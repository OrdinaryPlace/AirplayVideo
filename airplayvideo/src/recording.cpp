#include "recording.hpp"
#include <fstream>
#include <regex>
#include <sys/stat.h>
extern "C" {
#include <libavformat/avformat.h>
#include <libavutil/mem.h>
}
namespace lab {
namespace {
void checked(int n) { require(n>=0,"Could not write diagnostic recording"); }
struct Mux {
  AVFormatContext *ctx=nullptr;
  AVStream *video=nullptr, *audio=nullptr;
  bool header=false;
  ~Mux() { if(ctx) {if(header) av_write_trailer(ctx);avio_closep(&ctx->pb);avformat_free_context(ctx);} }
  void open(const std::filesystem::path &path,const Json &config,const MediaPacket &first) {
    checked(avformat_alloc_output_context2(&ctx,nullptr,"matroska",path.c_str()));
    require(ctx,"Recording allocation failed");
    video=avformat_new_stream(ctx,nullptr);require(video,"Recording video stream");
    video->time_base={1,1000000};video->avg_frame_rate={config.at("fps"),1};
    auto *v=video->codecpar;v->codec_type=AVMEDIA_TYPE_VIDEO;v->codec_id=AV_CODEC_ID_H264;
    v->width=config.at("width");v->height=config.at("height");
    v->extradata_size=first.video.avcc.size();
    v->extradata=static_cast<uint8_t*>(av_mallocz(v->extradata_size+AV_INPUT_BUFFER_PADDING_SIZE));
    require(v->extradata,"Recording codec data");std::copy(first.video.avcc.begin(),first.video.avcc.end(),v->extradata);
    if(config.value("audio",true)) {
      audio=avformat_new_stream(ctx,nullptr);require(audio,"Recording audio stream");
      audio->time_base={1,1000000};auto *a=audio->codecpar;
      a->codec_type=AVMEDIA_TYPE_AUDIO;a->codec_id=AV_CODEC_ID_PCM_S16LE;
      a->sample_rate=44100;a->bits_per_coded_sample=16;a->block_align=4;
      av_channel_layout_default(&a->ch_layout,2);
    }
    checked(avio_open(&ctx->pb,path.c_str(),AVIO_FLAG_WRITE));chmod(path.c_str(),0600);
    checked(avformat_write_header(ctx,nullptr));header=true;
  }
  void write(const MediaPacket &in,int64_t origin,int fps) {
    AVPacket *p=av_packet_alloc();require(p,"Recording packet allocation");
    try {
      const Bytes &data=in.audio?in.pcm:in.video.payload;
      checked(av_new_packet(p,data.size()));std::copy(data.begin(),data.end(),p->data);
      if(in.audio) for(size_t i=0;i<data.size();i+=2) std::swap(p->data[i],p->data[i+1]);
      auto *stream=in.audio?audio:video;require(stream,"Unexpected recording audio");
      p->stream_index=stream->index;p->pts=p->dts=in.pts_us-origin;
      p->duration=in.audio?av_rescale(352,1000000,44100):1000000/fps;
      if(in.audio||in.video.keyframe)p->flags|=AV_PKT_FLAG_KEY;
      av_packet_rescale_ts(p,{1,1000000},stream->time_base);
      checked(av_interleaved_write_frame(ctx,p));
      require(avio_tell(ctx->pb)<128*1024*1024,"Recording reached its size limit");
    } catch(...) {av_packet_free(&p);throw;}
    av_packet_free(&p);
  }
};
}
Recording::Recording(Media &media,const std::filesystem::path &directory,
                     const std::string &id,int seconds,Note note) {
  require(std::regex_match(id,std::regex("[0-9a-f]{32}")),"Invalid recording identifier");
  require(seconds>=5&&seconds<=30,"Recording length must be 5 to 30 seconds");
  std::filesystem::create_directories(directory);chmod(directory.c_str(),0700);
  auto path=directory/(id+".mkv"),report=directory/(id+".json"),csv=directory/(id+".csv");
  require(!std::filesystem::exists(path)&&!std::filesystem::exists(report)&&!std::filesystem::exists(csv),"Recording already exists");
  subscription_=media.subscribe();
  worker_=std::jthread([this,&media,path,report,csv,id,seconds,note] {
    Json result={{"id",id},{"seconds_requested",seconds},{"stage","encoded_before_airplay"},
      {"video_codec","h264"},{"audio_codec",media.config.value("audio",true)?"pcm_s16le":"none"},
      {"playback_buffer_ms",media.config.at("latency_ms")},{"width",media.config.at("width")},
      {"height",media.config.at("height")},{"fps",media.config.at("fps")},
      {"source_kind",media.config.at("source").at("kind")}};
    int64_t first=-1,last_video=-1,last_audio=-1;uint64_t video=0,audio=0;
    int64_t age_min[2]={INT64_MAX,INT64_MAX},age_max[2]={INT64_MIN,INT64_MIN};
    bool complete=false;
    try {
      // Construct after paths are checked; the data directory is owner-only.
      std::ofstream timings(csv);chmod(csv.c_str(),0600);require(bool(timings),"Recording timing file");
      timings<<"track,pts_us,available_us,observed_us,age_us,queue_us,bytes,keyframe\n";
      Mux mux;
      auto deadline=Clock::now()+std::chrono::seconds(seconds+8);
      while(!stop_&&Clock::now()<deadline) {
        auto p=subscription_->next(stop_);
        if(!p) {require(!media.failed(),"Capture source failed");continue;}
        if(first<0) {
          if(p->audio||!p->video.keyframe)continue;
          first=p->pts_us;mux.open(path,media.config,*p);
        }
        if(p->pts_us<first)continue;
        const int track=p->audio?1:0;
        int64_t observed=std::chrono::duration_cast<std::chrono::microseconds>(Clock::now()-media.epoch).count();
        int64_t age=observed-p->pts_us;
        age_min[track]=std::min(age_min[track],age);age_max[track]=std::max(age_max[track],age);
        timings<<(p->audio?"audio":"video")<<','<<p->pts_us<<','<<p->available_us<<','<<observed<<','<<age<<','<<(observed-p->available_us)<<','
          <<(p->audio?p->pcm.size():p->video.payload.size())<<','<<(!p->audio&&p->video.keyframe)<<'\n';
        if(p->audio)last_audio=p->pts_us;else last_video=p->pts_us;
        if(p->pts_us-first<int64_t(seconds)*1000000) {
          mux.write(*p,first,media.config.at("fps"));if(p->audio)++audio;else ++video;
        }
        if(last_video-first>=int64_t(seconds)*1000000&&
           (!media.config.value("audio",true)||last_audio-first>=int64_t(seconds)*1000000)) {complete=true;break;}
      }
      require(complete,"Recording ended before the requested duration");
      result["status"]="complete";
    } catch(const std::exception &e) {result["status"]="error";result["error"]=e.what();}
    subscription_->close();result["video_frames"]=video;result["audio_packets"]=audio;
    result["origin_pts_us"]=first;result["video_last_pts_us"]=last_video;result["audio_last_pts_us"]=last_audio;
    for(int i=0;i<2;++i)if(age_min[i]!=INT64_MAX)result[i?"audio_age_us":"video_age_us"]={{"min",age_min[i]},{"max",age_max[i]}};
    try {private_write_new(report,result.dump(2));}catch(...) {result["status"]="error";result["error"]="Could not save recording report";}
    done_=true;note("recording_finished",result);
  });
}
Recording::~Recording() {stop_=true;subscription_->close();if(worker_.joinable())worker_.join();}
}
