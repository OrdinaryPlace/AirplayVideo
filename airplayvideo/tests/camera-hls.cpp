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
void decode(const std::filesystem::path &playlist,bool sound,double expected_seconds=0) {
  AVFormatContext *input=nullptr;
  require(avformat_open_input(&input,playlist.c_str(),nullptr,nullptr)>=0,"Camera HLS opens");
  try {
    require(avformat_find_stream_info(input,nullptr)>=0,"Camera HLS formats are readable");
    require(input->nb_streams==(sound?2U:1U),"Camera HLS contains exactly the requested tracks");
    std::vector<AVCodecContext*> decoders(input->nb_streams,nullptr);
    std::vector<int> frames(input->nb_streams,0);
    std::vector<int64_t> previous(input->nb_streams,AV_NOPTS_VALUE);
    int64_t first_decoded_video=AV_NOPTS_VALUE,last_decoded_video=AV_NOPTS_VALUE;
    AVPacket *packet=av_packet_alloc();AVFrame *frame=av_frame_alloc();
    require(packet&&frame,"Camera decode allocation");
    try {
      for(unsigned i=0;i<input->nb_streams;++i) {
        const auto *par=input->streams[i]->codecpar;
        require(par->codec_id==(i==0?AV_CODEC_ID_H264:AV_CODEC_ID_AAC),"Camera HLS codecs are H.264 and AAC");
        decoders[i]=avcodec_alloc_context3(avcodec_find_decoder(par->codec_id));
        require(decoders[i]&&avcodec_parameters_to_context(decoders[i],par)>=0,"Camera decoder format");
        decoders[i]->pkt_timebase=input->streams[i]->time_base;
        require(avcodec_open2(decoders[i],decoders[i]->codec,nullptr)>=0,"Camera decoder opens");
      }
      auto receive_frames=[&](unsigned i) {
        for(;;) {
          const int result=avcodec_receive_frame(decoders[i],frame);
          if(result==AVERROR(EAGAIN)||result==AVERROR_EOF)break;
          require(result>=0,"Camera output frame decodes without an error");
          if(i==0) {
            const auto pts=frame->best_effort_timestamp;
            require(pts!=AV_NOPTS_VALUE&&(last_decoded_video==AV_NOPTS_VALUE||pts>last_decoded_video),
                    "Decoded camera video has advancing presentation timestamps");
            if(first_decoded_video==AV_NOPTS_VALUE)first_decoded_video=pts;
            last_decoded_video=pts;
          }
          ++frames[i];av_frame_unref(frame);
        }
      };
      while(av_read_frame(input,packet)>=0) {
        unsigned i=packet->stream_index;require(i<decoders.size(),"Camera stream index");
        require(packet->pts!=AV_NOPTS_VALUE&&(previous[i]==AV_NOPTS_VALUE||packet->pts>previous[i]),"Camera track PTS are monotonic");
        if(previous[i]==AV_NOPTS_VALUE&&i==0) require(packet->flags&AV_PKT_FLAG_KEY,"Rolling camera playlist starts with a keyframe");
        previous[i]=packet->pts;
        require(avcodec_send_packet(decoders[i],packet)>=0,"Camera packet decodes");
        receive_frames(i);
        av_packet_unref(packet);
      }
      for(unsigned i=0;i<decoders.size();++i) {
        require(avcodec_send_packet(decoders[i],nullptr)>=0,"Camera decoder flushes its final frames");
        receive_frames(i);
      }
      if(expected_seconds>0) {
        require(frames[0]>=2,"Finite camera clip yields advancing decoded video");
        const double span=(last_decoded_video-first_decoded_video)*av_q2d(input->streams[0]->time_base);
        require(span>=expected_seconds-0.15,"Finite camera decodes the complete requested presentation span");
      } else require(frames[0]>=30,"Camera HLS yields decoded video frames");
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
  media.start();int peers=0;int64_t pts=0,last_peer_pts[2]={-1,-1};
  auto observe_peer=[&](const std::shared_ptr<const MediaPacket> &p) {
    if(!p)return;
    const int track=p->audio?1:0;
    require(p->pts_us>last_peer_pts[track],"Independent receiver timestamps remain monotonic");
    last_peer_pts[track]=p->pts_us;++peers;pts=p->pts_us;
  };
  auto deadline=Clock::now()+std::chrono::seconds(25);
  while(pts<18500000&&Clock::now()<deadline) {
    observe_peer(peer->next(stop));
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
  {
    // Decoding twelve seconds of stored media can take longer than this live
    // subscriber's bounded queue on a loaded CI runner. Keep the independent
    // receiver consuming while the synchronous decoder checks the camera.
    std::atomic<bool> drain_failed{false};
    std::jthread drain([&](std::stop_token ending) {
      try {
        while(!ending.stop_requested()&&!stop)observe_peer(peer->next(stop));
      } catch(...) {drain_failed=true;}
    });
    decode(directory/"index.m3u8",true);
    drain.request_stop();drain.join();
    require(!drain_failed&&!media.failed(),"Independent receiver continues while camera media is decoded");
    // On a decoder exception jthread's destructor also stops and joins here,
    // before either the failure-isolation test or Media teardown can run.
  }
  // A camera output failure must not cancel the independent source or receiver.
  auto failed_directory=directory.parent_path()/"failure";
  std::filesystem::create_directory(failed_directory);chmod(failed_directory.c_str(),0700);
  error=false;
  CameraHls failing(media,failed_directory,[&](const auto &event,const auto &){if(event=="camera_error")error=true;});
  {std::ofstream large(failed_directory/"over-limit");large.seekp(128*1024*1024);large.put('x');}
  deadline=Clock::now()+std::chrono::seconds(5);
  while(!error&&Clock::now()<deadline)observe_peer(peer->next(stop));
  require(error&&!media.failed(),"Camera disk-limit failure leaves the source running");
  auto p=peer->next(stop);require(bool(p),"Another receiver continues after camera failure");observe_peer(p);
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
void generated(const std::filesystem::path &directory,int seconds) {
  std::filesystem::create_directory(directory);chmod(directory.c_str(),0700);
  auto config=options(false);
  config["camera_only"]=true;
  config["source"]={{"kind","generated"},{"generated",{{"title","Camera"},{"tagline",""},
    {"duration_seconds",seconds},{"display","countdown"},{"timezone","UTC"}}}};
  std::atomic<bool> stop{false},ready{false},error{false};
  Media media(config,stop,[](const auto &,const auto &){});
  auto camera=std::make_unique<CameraHls>(media,directory,[&](const auto &event,const auto &){
    if(event=="camera_ready")ready=true;if(event=="camera_error")error=true;
  });
  media.start();auto deadline=Clock::now()+std::chrono::seconds(8);
  while(!media.completed()&&!media.failed()&&Clock::now()<deadline)std::this_thread::sleep_for(std::chrono::milliseconds(10));
  require(!media.failed(),"Camera-only generated source does not fail");
  require(media.completed(),"Camera-only generated source finishes its duration without a TV");
  while(!camera->done()&&Clock::now()<deadline)std::this_thread::sleep_for(std::chrono::milliseconds(10));
  require(camera->done(),"Finite camera drains its queued media and finalizes HLS");
  require(!error,"Finite camera finalizes without an output error");
  require(ready,"Finite camera announces ready, including clips shorter than the HLS segment target");
  camera.reset();stop=true;media.close();decode(directory/"index.m3u8",false,seconds);
}
}
int main() {
  auto root=std::filesystem::temp_directory_path()/("airplayvideo-camera-"+uuid());
  try {
    avdevice_register_all();av_log_set_level(AV_LOG_QUIET);
    std::filesystem::create_directory(root);chmod(root.c_str(),0700);
    rolling(root/"rolling");passive(root/"passive");generated(root/"generated",3);generated(root/"short",1);std::filesystem::remove_all(root);
    std::cout<<"Camera H.264/AAC decode, rolling retention, independent subscriber, isolated failure and camera-only generated duration passed\n";
    return 0;
  } catch(const std::exception &e) {std::cerr<<e.what()<<'\n';std::filesystem::remove_all(root);return 1;}
}
