// AirplayVideo's shared, timestamped capture and encoding pipeline.
#include "stream.hpp"
#include "generated.hpp"
#include <cstring>
#include <bit>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavdevice/avdevice.h>
#include <libavformat/avformat.h>
#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavutil/audio_fifo.h>
#include <libavutil/hwcontext.h>
#include <libavutil/imgutils.h>
#include <libavutil/opt.h>
#include <libavutil/time.h>
#include <libswresample/swresample.h>
#include <libswscale/swscale.h>
}
namespace lab {
namespace {
constexpr AVRational micros{1, 1000000};
void check(int result, const char *message) {
  // Do not include libav network errors: some contain private source URLs.
  require(result >= 0, std::string(message) + " (media error " + std::to_string(result) + ")");
}
struct FrameDelete { void operator()(AVFrame *p) const { av_frame_free(&p); } };
using Frame = std::unique_ptr<AVFrame, FrameDelete>;
Frame frame() { Frame p(av_frame_alloc()); require(bool(p), "Frame allocation"); return p; }
struct CodecDelete { void operator()(AVCodecContext *p) const { avcodec_free_context(&p); } };
using Codec = std::unique_ptr<AVCodecContext, CodecDelete>;
struct PacketDelete { void operator()(AVPacket *p) const { av_packet_free(&p); } };
using Packet = std::unique_ptr<AVPacket, PacketDelete>;

class VideoEncoder {
  Codec codec_;
  SwsContext *scale_ = nullptr;
  AVBufferRef *device_ = nullptr;
  AVFilterGraph *graph_ = nullptr;
  AVFilterContext *filter_in_ = nullptr, *filter_out_ = nullptr;
  Bytes sps_, pps_, avcc_;
  int fps_, width_, height_;
  int64_t last_tick_ = -1;
  bool deinterlace_;
  std::function<void(std::shared_ptr<MediaPacket>)> output_;

  void parameter_sets(std::span<const uint8_t> data, EncodedFrame &out) {
    std::vector<std::pair<size_t,size_t>> starts;
    for (size_t i = 0; i + 3 < data.size();) {
      size_t prefix = 0;
      if (data[i] == 0 && data[i+1] == 0 && data[i+2] == 1) prefix = 3;
      else if (i+4 <= data.size() && data[i] == 0 && data[i+1] == 0 && data[i+2] == 0 && data[i+3] == 1) prefix = 4;
      if (prefix) { starts.emplace_back(i,i+prefix); i += prefix; } else ++i;
    }
    require(!starts.empty(), "H.264 encoder did not produce Annex B");
    for (size_t i = 0; i < starts.size(); ++i) {
      size_t a=starts[i].second, b=i+1 < starts.size() ? starts[i+1].first : data.size();
      if (a == b) continue;
      Bytes nal(data.begin()+a,data.begin()+b);
      switch(nal[0]&31) {
      case 7: sps_=std::move(nal); break;
      case 8: pps_=std::move(nal); break;
      default:
        size_t pos=out.payload.size(); out.payload.resize(pos+4);
        be(out.payload,pos,nal.size(),4);
        out.payload.insert(out.payload.end(),nal.begin(),nal.end());
      }
    }
    if (!sps_.empty() && !pps_.empty()) {
      require(sps_.size()>=4 && sps_.size()<65536 && pps_.size()<65536,"H.264 parameter size");
      avcc_={1,sps_[1],sps_[2],sps_[3],255,225};
      size_t p=avcc_.size(); avcc_.resize(p+2); be(avcc_,p,sps_.size(),2);
      avcc_.insert(avcc_.end(),sps_.begin(),sps_.end()); avcc_.push_back(1);
      p=avcc_.size(); avcc_.resize(p+2); be(avcc_,p,pps_.size(),2);
      avcc_.insert(avcc_.end(),pps_.begin(),pps_.end());
    }
    out.avcc=avcc_;
  }
  void encode(AVFrame *input, int64_t us) {
    if(us<0) return;
    int64_t tick=av_rescale_q(us,micros,{1,fps_});
    if(tick<=last_tick_) return;
    last_tick_=tick;
    auto converted=frame();
    auto *v=codec_.get();
    converted->format=device_ ? AV_PIX_FMT_NV12 : AV_PIX_FMT_YUV420P;
    converted->width=width_; converted->height=height_;
    check(av_frame_get_buffer(converted.get(),32),"Video buffer");
    ptrdiff_t lines[4]={converted->linesize[0],converted->linesize[1],converted->linesize[2],converted->linesize[3]};
    check(av_image_fill_black(converted->data,lines,AVPixelFormat(converted->format),AVCOL_RANGE_MPEG,width_,height_),"Letterbox");
    double sar=input->sample_aspect_ratio.num>0 ? av_q2d(input->sample_aspect_ratio) : 1.0;
    double aspect=input->width*sar/input->height;
    int w=width_,h=int(width_/aspect)/2*2;
    if(h>height_) { h=height_; w=int(height_*aspect)/2*2; }
    w=std::clamp(w,2,width_); h=std::clamp(h,2,height_);
    int x=(width_-w)/4*2,y=(height_-h)/4*2;
    scale_=sws_getCachedContext(scale_,input->width,input->height,AVPixelFormat(input->format),w,h,AVPixelFormat(converted->format),SWS_BILINEAR,nullptr,nullptr,nullptr);
    require(scale_,"Video converter");
    int source_matrix=input->colorspace==AVCOL_SPC_BT709 || input->height>=720 ? SWS_CS_ITU709 : SWS_CS_ITU601;
    check(sws_setColorspaceDetails(scale_,sws_getCoefficients(source_matrix),input->color_range==AVCOL_RANGE_JPEG,sws_getCoefficients(SWS_CS_ITU709),0,0,1<<16,1<<16),"Video color conversion");
    uint8_t *dest[4]={converted->data[0]+y*converted->linesize[0]+x,nullptr,nullptr,nullptr};
    if(device_) dest[1]=converted->data[1]+y/2*converted->linesize[1]+x;
    else {
      dest[1]=converted->data[1]+y/2*converted->linesize[1]+x/2;
      dest[2]=converted->data[2]+y/2*converted->linesize[2]+x/2;
    }
    require(sws_scale(scale_,input->data,input->linesize,0,input->height,dest,converted->linesize)==h,"Incomplete video conversion");
    converted->pts=av_rescale_q(us,micros,v->time_base);
    converted->pict_type=input->pict_type;
    converted->color_range=AVCOL_RANGE_MPEG; converted->colorspace=AVCOL_SPC_BT709;
    converted->color_primaries=AVCOL_PRI_BT709; converted->color_trc=AVCOL_TRC_BT709;
    if(device_) {
      auto gpu=frame(); check(av_hwframe_get_buffer(v->hw_frames_ctx,gpu.get(),0),"GPU frame");
      check(av_hwframe_transfer_data(gpu.get(),converted.get(),0),"GPU upload");
      check(av_frame_copy_props(gpu.get(),converted.get()),"GPU frame properties");
      check(avcodec_send_frame(v,gpu.get()),"Encode video");
    } else check(avcodec_send_frame(v,converted.get()),"Encode video");
    Packet packet(av_packet_alloc());
    for(;;) {
      int result=avcodec_receive_packet(v,packet.get());
      if(result==AVERROR(EAGAIN)||result==AVERROR_EOF) break;
      check(result,"Encoded video packet");
      auto out=std::make_shared<MediaPacket>();
      out->pts_us=av_rescale_q(packet->pts,v->time_base,micros);
      out->video.keyframe=packet->flags&AV_PKT_FLAG_KEY;
      parameter_sets({packet->data,size_t(packet->size)},out->video);
      require(!out->video.avcc.empty(),"H.264 codec configuration missing");
      if(!out->video.payload.empty()) output_(std::move(out));
      av_packet_unref(packet.get());
    }
  }
public:
  VideoEncoder(const Json &o,std::function<void(std::shared_ptr<MediaPacket>)> output)
      : fps_(o.at("fps")),width_(o.at("width")),height_(o.at("height")),deinterlace_(o.value("deinterlace",true)),output_(std::move(output)) {
    std::string encoder=o.value("encoder","libopenh264");
    require(encoder=="libopenh264" || encoder=="h264_vaapi","Unsupported encoder");
    const auto *c=avcodec_find_encoder_by_name(encoder.c_str()); require(c,"Encoder unavailable");
    codec_.reset(avcodec_alloc_context3(c)); require(bool(codec_),"Video codec allocation");
    auto *v=codec_.get(); v->width=width_; v->height=height_;
    v->time_base={1,90000}; v->framerate={fps_,1}; v->bit_rate=o.at("bitrate").get<int64_t>();
    v->gop_size=fps_*2; v->max_b_frames=0; v->thread_count=2;
    v->profile=AV_PROFILE_H264_MAIN; v->color_range=AVCOL_RANGE_MPEG;
    v->colorspace=AVCOL_SPC_BT709; v->color_primaries=AVCOL_PRI_BT709; v->color_trc=AVCOL_TRC_BT709;
    if(encoder=="h264_vaapi") {
      check(av_hwdevice_ctx_create(&device_,AV_HWDEVICE_TYPE_VAAPI,"/dev/dri/renderD128",nullptr,0),"VAAPI device");
      v->pix_fmt=AV_PIX_FMT_VAAPI; v->hw_frames_ctx=av_hwframe_ctx_alloc(device_); require(v->hw_frames_ctx,"VAAPI frame context");
      auto *h=reinterpret_cast<AVHWFramesContext*>(v->hw_frames_ctx->data);
      h->format=AV_PIX_FMT_VAAPI; h->sw_format=AV_PIX_FMT_NV12; h->width=width_; h->height=height_; h->initial_pool_size=8;
      check(av_hwframe_ctx_init(v->hw_frames_ctx),"VAAPI frame pool");
    } else v->pix_fmt=AV_PIX_FMT_YUV420P;
    check(avcodec_open2(v,c,nullptr),"Open H.264 encoder");
  }
  ~VideoEncoder() { avfilter_graph_free(&graph_); sws_freeContext(scale_); codec_.reset(); av_buffer_unref(&device_); }
  void consume(AVFrame *f,int64_t us) {
    if(!deinterlace_ || (!graph_ && !(f->flags&AV_FRAME_FLAG_INTERLACED))) { encode(f,us); return; }
    if(!graph_) {
      graph_=avfilter_graph_alloc(); require(graph_,"Deinterlace graph"); graph_->nb_threads=2;
      std::string args="video_size="+std::to_string(f->width)+"x"+std::to_string(f->height)+":pix_fmt="+std::to_string(f->format)+":time_base=1/1000000:pixel_aspect="+std::to_string(f->sample_aspect_ratio.num>0?f->sample_aspect_ratio.num:1)+"/"+std::to_string(f->sample_aspect_ratio.den>0?f->sample_aspect_ratio.den:1);
      AVFilterContext *deinterlacer=nullptr;
      check(avfilter_graph_create_filter(&filter_in_,avfilter_get_by_name("buffer"),"input",args.c_str(),nullptr,graph_),"Deinterlace input");
      check(avfilter_graph_create_filter(&deinterlacer,avfilter_get_by_name("bwdif"),"deinterlace","mode=send_field:parity=auto:deint=interlaced",nullptr,graph_),"Deinterlace filter");
      check(avfilter_graph_create_filter(&filter_out_,avfilter_get_by_name("buffersink"),"output",nullptr,nullptr,graph_),"Deinterlace output");
      check(avfilter_link(filter_in_,0,deinterlacer,0),"Deinterlace link"); check(avfilter_link(deinterlacer,0,filter_out_,0),"Deinterlace link");
      check(avfilter_graph_config(graph_,nullptr),"Deinterlace graph configuration");
    }
    f->pts=us; check(av_buffersrc_add_frame_flags(filter_in_,f,AV_BUFFERSRC_FLAG_KEEP_REF),"Deinterlace frame");
    auto out=frame();
    for(;;) {
      int result=av_buffersink_get_frame(filter_out_,out.get());
      if(result==AVERROR(EAGAIN)||result==AVERROR_EOF) break;
      check(result,"Deinterlaced output");
      encode(out.get(),av_rescale_q(out->pts,av_buffersink_get_time_base(filter_out_),micros));
      av_frame_unref(out.get());
    }
  }
};

class AudioConverter {
  SwrContext *resample_=nullptr;
  AVAudioFifo *fifo_=nullptr;
  int64_t origin_us_=0, samples_in_=0, samples_out_=0;
  int rate_=0;
  std::function<void(std::shared_ptr<MediaPacket>)> output_;
public:
  explicit AudioConverter(std::function<void(std::shared_ptr<MediaPacket>)> out):output_(std::move(out)) {
    fifo_=av_audio_fifo_alloc(AV_SAMPLE_FMT_S16,2,8192); require(fifo_,"Audio buffer");
  }
  ~AudioConverter() { av_audio_fifo_free(fifo_); swr_free(&resample_); }
  void consume(AVFrame *in,int64_t us) {
    if(us<0) return;
    if(!resample_) {
      AVChannelLayout stereo=AV_CHANNEL_LAYOUT_STEREO;
      check(swr_alloc_set_opts2(&resample_,&stereo,AV_SAMPLE_FMT_S16,44100,&in->ch_layout,AVSampleFormat(in->format),in->sample_rate,0,nullptr),"Audio resampler");
      check(swr_init(resample_),"Audio resampler initialization");
      origin_us_=us; rate_=in->sample_rate;
    }
    require(in->sample_rate==rate_,"Audio format changed; restart the source");
    int64_t error=us-(origin_us_+av_rescale(samples_in_,1000000,rate_));
    require(std::abs(error)<2000000,"Audio clock discontinuity ("+std::to_string(error/1000)+" ms); playback stopped");
    // PulseAudio can omit silence holes. Preserve their time on the common
    // video/audio timeline instead of compressing the audio timeline. Ignore
    // ordinary device-clock jitter; trim overlapping source samples on a jump
    // backwards. No stale timestamp is replaced with the current wall clock.
    int skip=0;
    if(error>50000) {
      int gap=int(av_rescale(error,rate_,1000000));
      check(swr_inject_silence(resample_,gap),"Fill audio timestamp gap");
      samples_in_+=gap;
    } else if(error < -50000) {
      skip=std::min(in->nb_samples,int(av_rescale(-error,rate_,1000000)));
      if(skip==in->nb_samples) return;
    }
    int count_in=in->nb_samples-skip;
    samples_in_+=count_in;
    bool planar=av_sample_fmt_is_planar(AVSampleFormat(in->format));
    int planes=planar?in->ch_layout.nb_channels:1;
    int stride=av_get_bytes_per_sample(AVSampleFormat(in->format))*(planar?1:in->ch_layout.nb_channels);
    std::vector<const uint8_t*> input(size_t(planes),nullptr);
    for(int i=0;i<planes;++i) input[i]=in->extended_data[i]+size_t(skip)*stride;
    int capacity=swr_get_out_samples(resample_,count_in);
    Bytes converted(size_t(capacity)*4); uint8_t *data=converted.data();
    int count=swr_convert(resample_,&data,capacity,input.data(),count_in);
    check(count,"Audio conversion");
    require(av_audio_fifo_size(fifo_)+count<44100*3,"Audio buffer overrun");
    require(av_audio_fifo_write(fifo_,reinterpret_cast<void**>(&data),count)==count,"Audio buffer write");
    while(av_audio_fifo_size(fifo_)>=352) {
      auto out=std::make_shared<MediaPacket>(); out->audio=true;
      out->pts_us=origin_us_+av_rescale(samples_out_,1000000,44100); samples_out_+=352;
      out->pcm.resize(352*4); void *ptr=out->pcm.data();
      require(av_audio_fifo_read(fifo_,&ptr,352)==352,"Audio buffer read");
      if constexpr(std::endian::native==std::endian::little)
        for(size_t i=0;i<out->pcm.size();i+=2) std::swap(out->pcm[i],out->pcm[i+1]);
      output_(std::move(out));
    }
  }
};
}

std::shared_ptr<const MediaPacket> Subscriber::next(const std::atomic<bool> &stop) {
  std::unique_lock lock(mutex);
  ready.wait_for(lock,std::chrono::milliseconds(100),[&]{return stop||closed||!packets.empty();});
  if(!error.empty()) throw std::runtime_error(error);
  if(stop||closed||packets.empty()) return {};
  auto packet=packets.front(); packets.pop_front();
  bytes-=packet->audio ? packet->pcm.size() : packet->video.payload.size();
  return packet;
}
void Subscriber::close() { std::lock_guard lock(mutex); closed=true; ready.notify_all(); }

struct Media::Impl {
  Media &owner;
  std::atomic<bool> &stop;
  Note note;
  std::atomic<bool> closed{false},failure{false},completed{false},joined{false};
  std::mutex mutex;
  std::vector<std::weak_ptr<Subscriber>> subscribers;
  std::vector<std::jthread> workers;
  uint64_t video_count=0,audio_count=0;
  bool ready=false;
  int64_t wall_epoch=av_gettime();
  explicit Impl(Media &o,std::atomic<bool> &s,Note n):owner(o),stop(s),note(std::move(n)) {}
  static int interrupt(void *opaque) { auto *self=static_cast<Impl*>(opaque); return self->stop||self->closed; }
  void publish(std::shared_ptr<MediaPacket> p) {
    p->available_us=std::chrono::duration_cast<std::chrono::microseconds>(Clock::now()-owner.epoch).count();
    std::lock_guard lock(mutex);
    if(p->audio) ++audio_count; else ++video_count;
    if(!ready && video_count>0 && (!owner.config.value("audio",true)||audio_count>0)) {
      ready=true; note("media_ready",{{"width",owner.config.at("width")},{"height",owner.config.at("height")},{"fps",owner.config.at("fps")},{"audio",owner.config.value("audio",true)},{"encoder",owner.config.at("encoder")}});
    }
    if(!p->audio && video_count%owner.config.at("fps").get<int>()==0)
      note("media",{{"video_frames",video_count},{"audio_packets",audio_count}});
    std::erase_if(subscribers,[](auto &s){return s.expired();});
    for(auto &weak:subscribers) if(auto s=weak.lock()) {
      std::lock_guard qlock(s->mutex);
      if(s->closed) continue;
      // A subscription may arrive while a pre-join frame is encoding. Do not
      // expose that undecodable frame (or its audio) before the next IDR.
      if(s->waiting_keyframe) {
        if(p->audio||!p->video.keyframe)continue;
        s->waiting_keyframe=false;
      }
      size_t size=p->audio?p->pcm.size():p->video.payload.size();
      if(s->packets.size()>=600 || s->bytes+size>24*1024*1024) {
        s->error="Receiver cannot keep up; its connection was stopped"; s->closed=true;
      } else { s->packets.push_back(p); s->bytes+=size; }
      s->ready.notify_one();
    }
  }
  void generated() {
    try {
      const auto &o=owner.config;
      require(!o.value("audio",true),"Generated videos are silent");
      GeneratedVideo renderer(o.at("source").at("generated"),o.at("width"),o.at("height"));
      VideoEncoder encoder(o,[this](auto p){publish(std::move(p));});
      auto f=frame(); f->format=AV_PIX_FMT_BGRA; f->width=o.at("width"); f->height=o.at("height");
      f->sample_aspect_ratio={1,1};f->color_range=AVCOL_RANGE_JPEG;f->colorspace=AVCOL_SPC_BT709;
      bool started=false; Clock::time_point start; int64_t index=0;
      const int fps=o.at("fps"),lead=o.at("latency_ms");
      while(!interrupt(this)) {
        auto due=owner.epoch+std::chrono::microseconds(index*1000000/fps);
        while(!interrupt(this)&&Clock::now()<due) std::this_thread::sleep_for(std::chrono::milliseconds(2));
        if(interrupt(this)) break;
        auto now=Clock::now(); bool first=joined&&!started;
        if(first) {started=true;start=now;}
        double elapsed=started?std::chrono::duration<double>(now-start).count():0;
        f->data[0]=const_cast<uint8_t*>(renderer.draw(elapsed,(av_gettime()+int64_t(lead)*1000)/1000000));
        f->linesize[0]=renderer.stride(); f->pict_type=first?AV_PICTURE_TYPE_I:AV_PICTURE_TYPE_NONE;
        auto us=std::chrono::duration_cast<std::chrono::microseconds>(now-owner.epoch).count();
        encoder.consume(f.get(),us);
        if(started&&elapsed>=renderer.duration()) {
          // Let the final 00:00 picture reach its presentation deadline before TEARDOWN.
          auto drain=now+std::chrono::milliseconds(lead+100);
          while(!interrupt(this)&&Clock::now()<drain) std::this_thread::sleep_for(std::chrono::milliseconds(10));
          if(!interrupt(this)) completed=true;
          break;
        }
        // Skip missed ticks rather than rendering a burst of stale frames.
        index=std::max(index+1,us*fps/1000000+1);
      }
    } catch(const std::exception &e) {
      if(!interrupt(this)) {failure=true;note("source_error",{{"message",e.what()}});closed=true;}
    }
  }
  void capture(int device_stream) {
    AVFormatContext *input=avformat_alloc_context();
    try {
      require(input,"Capture allocation"); input->interrupt_callback={interrupt,this};
      const auto &o=owner.config;
      const auto kind=o.at("source").at("kind").get<std::string>();
      bool network=kind=="hdhomerun"||kind=="fixture";
      std::string format,source;
      AVDictionary *opts=nullptr;
      if(kind=="synthetic") {
        format="lavfi";
        source=device_stream==0 ? "testsrc2=size="+std::to_string(o.at("width").get<int>())+"x"+std::to_string(o.at("height").get<int>())+":rate="+std::to_string(o.at("fps").get<int>()) : "sine=frequency=440:sample_rate=48000";
      } else if(kind=="browser") {
        format=device_stream==0?"x11grab":"pulse";
        source=device_stream==0?o.at("source").value("display",":99.0"):o.at("source").value("pulse","airplayvideo.monitor");
        if(device_stream==0) {
          av_dict_set(&opts,"video_size",(std::to_string(o.at("width").get<int>())+"x"+std::to_string(o.at("height").get<int>())).c_str(),0);
          av_dict_set(&opts,"framerate",std::to_string(o.at("fps").get<int>()).c_str(),0);
          av_dict_set(&opts,"draw_mouse","0",0);
        } else {
          av_dict_set(&opts,"sample_rate","48000",0); av_dict_set(&opts,"channels","2",0); av_dict_set(&opts,"fragment_size","4096",0);
        }
      } else {
        require(network,"Unsupported source"); source=o.at("source").at("url");
        if(kind=="hdhomerun") require(source.starts_with("http://"),"HDHomeRun requires local HTTP");
        av_dict_set(&opts,"protocol_whitelist",kind=="fixture"?"file":"http,tcp",0);
        av_dict_set(&opts,"rw_timeout","5000000",0);
        av_dict_set(&opts,"probesize","2000000",0); av_dict_set(&opts,"analyzeduration","2000000",0);
      }
      const AVInputFormat *demuxer=format.empty()?nullptr:av_find_input_format(format.c_str());
      require(format.empty()||demuxer,"Required capture device missing");
      int result=avformat_open_input(&input,source.c_str(),demuxer,&opts); av_dict_free(&opts); check(result,"Open media source");
      if(network) check(avformat_find_stream_info(input,nullptr),"Read channel formats");
      std::map<int,Codec> decoders;
      int video_index=-1,audio_index=-1;
      for(unsigned i=0;i<input->nb_streams;++i) {
        auto *p=input->streams[i]->codecpar;
        if(p->codec_type==AVMEDIA_TYPE_VIDEO && video_index<0) {
          if(kind=="hdhomerun") require(p->codec_id==AV_CODEC_ID_H264||p->codec_id==AV_CODEC_ID_MPEG2VIDEO,"Channel video format is not supported");
          video_index=int(i);
        } else if(p->codec_type==AVMEDIA_TYPE_AUDIO && audio_index<0 && o.value("audio",true)) {
          if(kind=="hdhomerun") require(p->codec_id==AV_CODEC_ID_AC3||p->codec_id==AV_CODEC_ID_AAC||p->codec_id==AV_CODEC_ID_MP2||p->codec_id==AV_CODEC_ID_MP3,"Channel audio format is not supported");
          audio_index=int(i);
        } else continue;
        const auto *codec=avcodec_find_decoder(p->codec_id); require(codec,"Source decoder unavailable");
        Codec ctx(avcodec_alloc_context3(codec)); require(bool(ctx),"Decoder allocation");
        check(avcodec_parameters_to_context(ctx.get(),p),"Decoder format"); ctx->thread_count=2;
        check(avcodec_open2(ctx.get(),codec,nullptr),"Open source decoder"); decoders.emplace(i,std::move(ctx));
      }
      require(!network||video_index>=0,"Channel has no video");
      require(!network||!o.value("audio",true)||audio_index>=0,"Channel has no supported audio");
      auto deliver=[this](auto p){publish(std::move(p));};
      std::unique_ptr<VideoEncoder> video; std::unique_ptr<AudioConverter> audio;
      if(video_index>=0) video=std::make_unique<VideoEncoder>(o,deliver);
      if(audio_index>=0) audio=std::make_unique<AudioConverter>(deliver);
      int64_t origin=network ? input->start_time : 0;
      int64_t offset=network ? std::chrono::duration_cast<std::chrono::microseconds>(Clock::now()-owner.epoch).count() : 0;
      Packet packet(av_packet_alloc()); require(bool(packet),"Capture packet allocation");
      while(!interrupt(this)) {
        result=av_read_frame(input,packet.get());
        if(result<0) { if(interrupt(this)) break; throw std::runtime_error("Media source ended or timed out; press Play to reconnect"); }
        auto it=decoders.find(packet->stream_index);
        if(it==decoders.end()) { av_packet_unref(packet.get()); continue; }
        int index=packet->stream_index;
        check(avcodec_send_packet(it->second.get(),packet.get()),"Decode source packet"); av_packet_unref(packet.get());
        for(;;) {
          auto f=frame(); result=avcodec_receive_frame(it->second.get(),f.get());
          if(result==AVERROR(EAGAIN)||result==AVERROR_EOF) break;
          check(result,"Decode source frame");
          int64_t pts=f->best_effort_timestamp!=AV_NOPTS_VALUE?f->best_effort_timestamp:f->pts;
          require(pts!=AV_NOPTS_VALUE,"Source has no presentation timestamps");
          int64_t us=av_rescale_q(pts,input->streams[index]->time_base,micros);
          if(network) {
            if(origin==AV_NOPTS_VALUE) origin=us;
            us=us-origin+offset;
          } else if(kind=="browser") us-=wall_epoch;
          if(kind=="synthetic"||network) {
            auto due=owner.epoch+std::chrono::microseconds(us);
            while(!interrupt(this)&&Clock::now()<due) std::this_thread::sleep_for(std::chrono::milliseconds(1));
          }
          if(interrupt(this)) break;
          if(index==video_index) video->consume(f.get(),us);
          else audio->consume(f.get(),us);
        }
      }
    } catch(const std::exception &e) {
      if(!interrupt(this)) { failure=true; note("source_error",{{"message",e.what()}}); closed=true; }
    }
    avformat_close_input(&input);
    if(closed) close();
  }
  void close() {
    closed=true; std::lock_guard lock(mutex);
    for(auto &weak:subscribers) if(auto s=weak.lock()) s->close();
  }
};
Media::Media(Json c,std::atomic<bool> &s,Note n):config(std::move(c)),epoch_ntp(ntp_now()+(uint64_t(config.value("latency_ms",1500))*(uint64_t(1)<<32))/1000),epoch(Clock::now()),impl_(nullptr) {
  require(config.at("width")==1920||config.at("width")==1280,"Unsupported canvas");
  require((config.at("width")==1920&&config.at("height")==1080)||(config.at("width")==1280&&config.at("height")==720),"Unsupported canvas");
  require(config.at("fps")==30||config.at("fps")==60,"Unsupported frame rate");
  require(config.at("bitrate").get<int>()>=2000000&&config.at("bitrate").get<int>()<=20000000,"Invalid bitrate");
  require(config.value("latency_ms",1500)>=500&&config.value("latency_ms",1500)<=2000,"Invalid presentation lead");
  impl_=std::make_unique<Impl>(*this,s,std::move(n));
}
Media::~Media() { close(); impl_->workers.clear(); }
void Media::start() {
  auto kind=config.at("source").at("kind");
  if(kind=="generated") {impl_->workers.emplace_back([this]{impl_->generated();});return;}
  int count=(kind=="browser"||kind=="synthetic")&&config.value("audio",true)?2:1;
  for(int i=0;i<count;++i) impl_->workers.emplace_back([this,i]{impl_->capture(i);});
}
std::shared_ptr<Subscriber> Media::subscribe() { auto s=std::make_shared<Subscriber>(); std::lock_guard lock(impl_->mutex); impl_->subscribers.push_back(s); impl_->joined=true; return s; }
void Media::close() { if(impl_) impl_->close(); }
bool Media::failed() const { return impl_->failure; }
bool Media::completed() const { return impl_->completed; }
Json media_capabilities() {
  AVBufferRef *device=nullptr;
  bool hardware=avcodec_find_encoder_by_name("h264_vaapi")&&av_hwdevice_ctx_create(&device,AV_HWDEVICE_TYPE_VAAPI,"/dev/dri/renderD128",nullptr,0)>=0;
  av_buffer_unref(&device);
  return {{"codecs",Json::array({"h264"})},{"encoders",hardware?Json::array({"libopenh264","h264_vaapi"}):Json::array({"libopenh264"})},{"resolutions",Json::array({"1080p","720p"})},{"frame_rates",Json::array({30,60})},{"audio","PCM stereo 44.1 kHz"}};
}
}
