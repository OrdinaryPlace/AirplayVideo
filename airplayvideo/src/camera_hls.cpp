#include "camera_hls.hpp"
#include <fstream>
#include <sys/stat.h>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/audio_fifo.h>
#include <libavutil/mem.h>
}
namespace lab {
namespace {
void checked(int n) { require(n>=0,"Could not produce the Home Assistant camera stream"); }
struct Mux {
  AVFormatContext *ctx=nullptr;
  AVCodecContext *aac=nullptr;
  AVAudioFifo *fifo=nullptr;
  AVStream *video=nullptr, *audio=nullptr;
  int64_t audio_pts=AV_NOPTS_VALUE;
  bool header=false;
  ~Mux() {
    if(ctx) {
      if(header) av_write_trailer(ctx);
      if(!(ctx->oformat->flags&AVFMT_NOFILE)) avio_closep(&ctx->pb);
      avformat_free_context(ctx);
    }
    av_audio_fifo_free(fifo);avcodec_free_context(&aac);
  }
  void open(const std::filesystem::path &directory,const Json &config,const MediaPacket &first) {
    auto path=directory/"index.m3u8";
    checked(avformat_alloc_output_context2(&ctx,nullptr,"hls",path.c_str()));
    require(ctx,"Camera stream allocation failed");
    ctx->max_interleave_delta=1000000;
    video=avformat_new_stream(ctx,nullptr);require(video,"Camera video stream allocation failed");
    video->time_base={1,1000000};video->avg_frame_rate={config.at("fps"),1};
    auto *v=video->codecpar;v->codec_type=AVMEDIA_TYPE_VIDEO;v->codec_id=AV_CODEC_ID_H264;
    v->width=config.at("width");v->height=config.at("height");
    v->extradata_size=first.video.avcc.size();
    v->extradata=static_cast<uint8_t*>(av_mallocz(v->extradata_size+AV_INPUT_BUFFER_PADDING_SIZE));
    require(v->extradata,"Camera codec allocation failed");
    std::copy(first.video.avcc.begin(),first.video.avcc.end(),v->extradata);
    if(config.value("audio",true)) {
      const AVCodec *codec=avcodec_find_encoder(AV_CODEC_ID_AAC);require(codec,"Camera AAC encoder unavailable");
      aac=avcodec_alloc_context3(codec);require(aac,"Camera audio allocation failed");
      aac->sample_rate=44100;aac->sample_fmt=AV_SAMPLE_FMT_FLTP;aac->time_base={1,44100};
      aac->bit_rate=128000;av_channel_layout_default(&aac->ch_layout,2);
      aac->flags|=AV_CODEC_FLAG_GLOBAL_HEADER;
      checked(avcodec_open2(aac,codec,nullptr));
      audio=avformat_new_stream(ctx,nullptr);require(audio,"Camera audio stream allocation failed");
      audio->time_base=aac->time_base;checked(avcodec_parameters_from_context(audio->codecpar,aac));
      fifo=av_audio_fifo_alloc(aac->sample_fmt,2,aac->frame_size*2);require(fifo,"Camera audio buffer allocation failed");
    }
    AVDictionary *options=nullptr;
    av_dict_set(&options,"hls_time","2",0);av_dict_set(&options,"hls_list_size","6",0);
    av_dict_set(&options,"hls_delete_threshold","2",0);
    av_dict_set(&options,"hls_flags","delete_segments+independent_segments+temp_file",0);
    av_dict_set(&options,"hls_segment_type","mpegts",0);
    av_dict_set(&options,"hls_segment_filename",(directory/"segment%09d.ts").c_str(),0);
    if(!(ctx->oformat->flags&AVFMT_NOFILE)) checked(avio_open(&ctx->pb,path.c_str(),AVIO_FLAG_WRITE));
    const int result=avformat_write_header(ctx,&options);av_dict_free(&options);checked(result);header=true;
  }
  void encode_audio() {
    while(av_audio_fifo_size(fifo)>=aac->frame_size) {
      AVFrame *f=av_frame_alloc();require(f,"Camera audio frame allocation failed");
      try {
        f->format=aac->sample_fmt;f->sample_rate=aac->sample_rate;f->nb_samples=aac->frame_size;
        checked(av_channel_layout_copy(&f->ch_layout,&aac->ch_layout));checked(av_frame_get_buffer(f,0));
        require(av_audio_fifo_read(fifo,reinterpret_cast<void**>(f->data),f->nb_samples)==f->nb_samples,"Camera audio buffer read failed");
        f->pts=audio_pts;audio_pts+=f->nb_samples;checked(avcodec_send_frame(aac,f));
      } catch(...) {av_frame_free(&f);throw;}
      av_frame_free(&f);
      AVPacket *packet=av_packet_alloc();require(packet,"Camera audio packet allocation failed");
      try {
        for(;;) {
          int result=avcodec_receive_packet(aac,packet);
          if(result==AVERROR(EAGAIN)||result==AVERROR_EOF)break;
          checked(result);packet->stream_index=audio->index;
          av_packet_rescale_ts(packet,aac->time_base,audio->time_base);
          checked(av_interleaved_write_frame(ctx,packet));av_packet_unref(packet);
        }
      } catch(...) {av_packet_free(&packet);throw;}
      av_packet_free(&packet);
    }
  }
  void write(const MediaPacket &in,int64_t origin,int fps) {
    if(in.audio) {
      require(aac&&in.pcm.size()%4==0,"Unexpected camera audio format");
      const int samples=in.pcm.size()/4;
      std::vector<float> left(samples),right(samples);
      for(int i=0;i<samples;++i) {
        left[i]=int16_t(uint16_t(in.pcm[i*4])<<8|in.pcm[i*4+1])/32768.0f;
        right[i]=int16_t(uint16_t(in.pcm[i*4+2])<<8|in.pcm[i*4+3])/32768.0f;
      }
      if(audio_pts==AV_NOPTS_VALUE)audio_pts=av_rescale_q(in.pts_us-origin,{1,1000000},aac->time_base);
      require(av_audio_fifo_size(fifo)+samples<44100,"Camera audio buffer overrun");
      void *planes[]={left.data(),right.data()};
      require(av_audio_fifo_write(fifo,planes,samples)==samples,"Camera audio buffer write failed");
      encode_audio();return;
    }
    AVPacket *packet=av_packet_alloc();require(packet,"Camera video packet allocation failed");
    try {
      checked(av_new_packet(packet,in.video.payload.size()));
      std::copy(in.video.payload.begin(),in.video.payload.end(),packet->data);
      packet->stream_index=video->index;packet->pts=packet->dts=in.pts_us-origin;
      packet->duration=1000000/fps;if(in.video.keyframe)packet->flags|=AV_PKT_FLAG_KEY;
      av_packet_rescale_ts(packet,{1,1000000},video->time_base);
      checked(av_interleaved_write_frame(ctx,packet));
    } catch(...) {av_packet_free(&packet);throw;}
    av_packet_free(&packet);
  }
};
void check_size(const std::filesystem::path &directory) {
  uintmax_t total=0;
  for(const auto &entry:std::filesystem::directory_iterator(directory)) {
    require(entry.is_regular_file()&&!entry.is_symlink(),"Unexpected camera stream file");
    total+=entry.file_size();
    require(total<=128*1024*1024,"The Home Assistant camera reached its disk limit");
  }
}
}
CameraHls::CameraHls(Media &media,const std::filesystem::path &directory,Note note) {
  require(directory.is_absolute()&&!std::filesystem::is_symlink(directory)&&
          std::filesystem::is_directory(directory)&&std::filesystem::is_empty(directory),
          "Camera output requires a private empty directory");
  struct stat info{};
  require(stat(directory.c_str(),&info)==0&&(info.st_mode&0077)==0,"Camera output directory must be private");
  // A passive tap must not consume a generated announcement while TVs connect.
  // Camera-only playback explicitly makes this subscriber its presentation owner.
  subscription_=media.subscribe(media.config.value("camera_only",false));
  worker_=std::jthread([this,&media,directory,note] {
    int64_t origin=-1,last_check=-1;bool ready=false;
    try {
      Mux mux;
      while(!stop_) {
        auto packet=subscription_->next(stop_);
        if(!packet) {
          require(!media.failed(),"The camera source ended");
          if(media.completed())break;
          continue;
        }
        if(origin<0) {
          if(packet->audio||!packet->video.keyframe)continue;
          origin=packet->pts_us;mux.open(directory,media.config,*packet);
        }
        if(packet->pts_us<origin)continue;
        mux.write(*packet,origin,media.config.at("fps"));
        if(packet->pts_us-last_check>=500000) {
          check_size(directory);last_check=packet->pts_us;
          if(!ready&&std::filesystem::exists(directory/"index.m3u8")) {
            ready=true;note("camera_ready",{{"video_codec","h264"},{"audio_codec",media.config.value("audio",true)?"aac":"none"}});
          }
        }
      }
    } catch(const std::exception &) {
      // Never forward muxer details, paths, input URLs, or credentials.
      std::error_code ignored;std::filesystem::remove(directory/"index.m3u8",ignored);
      note("camera_error",{{"message","The Home Assistant camera stream stopped; TV playback can continue"}});
    }
    subscription_->close();done_=true;
  });
}
CameraHls::~CameraHls() {stop_=true;subscription_->close();if(worker_.joinable())worker_.join();}
}
