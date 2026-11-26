#include "camera_hls.hpp"
#include <fstream>
#include <iostream>
#include <sstream>
#include <sys/stat.h>
extern "C" {
#include <libavdevice/avdevice.h>
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
}
using namespace lab;
namespace {
void decode(const std::filesystem::path &playlist,bool sound) {
  AVFormatContext *input=nullptr;
  require(avformat_open_input(&input,playlist.c_str(),nullptr,nullptr)>=0,"Camera HLS opens");
  try {
    require(avformat_find_stream_info(input,nullptr)>=0,"Camera HLS formats are readable");
    require(input->nb_streams==(sound?2U:1U),"Camera HLS contains exactly the requested tracks");
    std::vector<AVCodecContext*> decoders(input->nb_streams,nullptr);
    std::vector<int> frames(input->nb_streams,0);
    std::vector<int64_t> previous(input->nb_streams,AV_NOPTS_VALUE);
    AVPacket *packet=av_packet_alloc();AVFrame *frame=av_frame_alloc();
    require(packet&&frame,"Camera decode allocation");
    try {
      for(unsigned i=0;i<input->nb_streams;++i) {
        const auto *par=input->streams[i]->codecpar;
        require(par->codec_id==(i==0?AV_CODEC_ID_H264:AV_CODEC_ID_AAC),"Camera HLS codecs are H.264 and AAC");
        decoders[i]=avcodec_alloc_context3(avcodec_find_decoder(par->codec_id));
        require(decoders[i]&&avcodec_parameters_to_context(decoders[i],par)>=0&&
                avcodec_open2(decoders[i],decoders[i]->codec,nullptr)>=0,"Camera decoder opens");
      }
      while(av_read_frame(input,packet)>=0) {
        unsigned i=packet->stream_index;require(i<decoders.size(),"Camera stream index");
        require(packet->pts!=AV_NOPTS_VALUE&&(previous[i]==AV_NOPTS_VALUE||packet->pts>previous[i]),"Camera track PTS are monotonic");
        if(previous[i]==AV_NOPTS_VALUE&&i==0) require(packet->flags&AV_PKT_FLAG_KEY,"Rolling camera playlist starts with a keyframe");
        previous[i]=packet->pts;
        require(avcodec_send_packet(decoders[i],packet)>=0,"Camera packet decodes");
        while(avcodec_receive_frame(decoders[i],frame)>=0) {++frames[i];av_frame_unref(frame);}
        av_packet_unref(packet);
      }
      require(frames[0]>=30,"Camera HLS yields decoded video frames");
      if(sound)require(frames[1]>=40,"Camera HLS yields decoded AAC audio");
    } catch(...) {
      for(auto *&decoder:decoders)avcodec_free_context(&decoder);
      av_packet_free(&packet);av_frame_free(&frame);throw;
    }
    for(auto *&decoder:decoders)avcodec_free_context(&decoder);
    av_packet_free(&packet);av_frame_free(&frame);
  } catch(...) {avformat_close_input(&input);throw;}
  avformat_close_input(&input);
}
Json options(bool sound) {
  return {{"source",{{"kind","synthetic"}}},{"width",1280},{"height",720},{"fps",30},
          {"bitrate",4000000},{"encoder","libopenh264"},{"audio",sound},{"latency_ms",500}};
}
void rolling(const std::filesystem::path &directory) {
  std::filesystem::create_directory(directory);chmod(directory.c_str(),0700);
  std::atomic<bool> stop{false},ready{false},error{false};
  Media media(options(true),stop,[](const auto &,const auto &){});
  auto peer=media.subscribe();
  auto camera=std::make_unique<CameraHls>(media,directory,[&](const auto &event,const auto &){
    if(event=="camera_ready")ready=true;if(event=="camera_error")error=true;
  });
  media.start();int peers=0;int64_t pts=0;
  auto deadline=Clock::now()+std::chrono::seconds(25);
  while(pts<18500000&&Clock::now()<deadline) {
    auto p=peer->next(stop);if(p) {++peers;pts=p->pts_us;}
    require(!media.failed()&&!error,"Camera and independent receiver stay healthy");
  }
  require(ready&&pts>=18500000&&peers>1800,"Camera is ready while another subscriber receives the source");
  camera.reset();
  std::ifstream file(directory/"index.m3u8");std::string line;int segments=0;
  while(std::getline(file,line)) if(line.starts_with("segment"))++segments;
  require(segments==6,"Camera playlist retains six rolling segments");
  size_t count=0;uintmax_t bytes=0;
  for(const auto &entry:std::filesystem::directory_iterator(directory)){++count;bytes+=entry.file_size();}
  require(count<=10&&bytes<128*1024*1024,"Old camera segments are deleted within bounded disk use");
  decode(directory/"index.m3u8",true);
  // A camera output failure must not cancel the independent source or receiver.
  auto failed_directory=directory.parent_path()/"failure";
  std::filesystem::create_directory(failed_directory);chmod(failed_directory.c_str(),0700);
  error=false;
  CameraHls failing(media,failed_directory,[&](const auto &event,const auto &){if(event=="camera_error")error=true;});
  {std::ofstream large(failed_directory/"over-limit");large.seekp(128*1024*1024);large.put('x');}
  deadline=Clock::now()+std::chrono::seconds(5);
  while(!error&&Clock::now()<deadline)peer->next(stop);
  require(error&&!media.failed(),"Camera disk-limit failure leaves the source running");
  auto p=peer->next(stop);require(bool(p),"Another receiver continues after camera failure");
  stop=true;media.close();
}
void passive(const std::filesystem::path &directory) {
  std::filesystem::create_directory(directory);chmod(directory.c_str(),0700);
  auto config=options(false);
  config["source"]={{"kind","generated"},{"generated",{{"title","Connecting"},{"tagline",""},
    {"duration_seconds",1},{"display","countdown"},{"timezone","UTC"}}}};
  std::atomic<bool> stop{false};
  Media media(config,stop,[](const auto &,const auto &){});
  CameraHls camera(media,directory,[](const auto &,const auto &){});
  media.start();std::this_thread::sleep_for(std::chrono::milliseconds(1700));
  require(!media.completed()&&!media.failed(),"A passive camera tap does not consume generated duration while TVs connect");
  auto peer=media.subscribe();auto joined=Clock::now(),deadline=joined+std::chrono::seconds(4);
  while(!media.completed()&&!media.failed()&&Clock::now()<deadline)peer->next(stop);
  require(media.completed()&&!media.failed()&&Clock::now()-joined>=std::chrono::milliseconds(1500),
          "Generated duration begins when an actual TV subscribes");
  stop=true;media.close();
}
void generated(const std::filesystem::path &directory) {
  std::filesystem::create_directory(directory);chmod(directory.c_str(),0700);
  auto config=options(false);
  config["camera_only"]=true;
  config["source"]={{"kind","generated"},{"generated",{{"title","Camera"},{"tagline",""},
    {"duration_seconds",3},{"display","countdown"},{"timezone","UTC"}}}};
  std::atomic<bool> stop{false},ready{false},error{false};
  Media media(config,stop,[](const auto &,const auto &){});
  auto camera=std::make_unique<CameraHls>(media,directory,[&](const auto &event,const auto &){
    if(event=="camera_ready")ready=true;if(event=="camera_error")error=true;
  });
  media.start();auto deadline=Clock::now()+std::chrono::seconds(8);
  while(!media.completed()&&!media.failed()&&Clock::now()<deadline)std::this_thread::sleep_for(std::chrono::milliseconds(10));
  require(ready&&!error&&!media.failed()&&media.completed(),"Camera-only generated playback starts its duration and completes without TVs or audio");
  camera.reset();stop=true;media.close();decode(directory/"index.m3u8",false);
}
}
int main() {
  auto root=std::filesystem::temp_directory_path()/("airplayvideo-camera-"+uuid());
  try {
    avdevice_register_all();av_log_set_level(AV_LOG_QUIET);
    std::filesystem::create_directory(root);chmod(root.c_str(),0700);
    rolling(root/"rolling");passive(root/"passive");generated(root/"generated");std::filesystem::remove_all(root);
    std::cout<<"Camera H.264/AAC decode, rolling retention, independent subscriber, isolated failure and camera-only generated duration passed\n";
    return 0;
  } catch(const std::exception &e) {std::cerr<<e.what()<<'\n';std::filesystem::remove_all(root);return 1;}
}
