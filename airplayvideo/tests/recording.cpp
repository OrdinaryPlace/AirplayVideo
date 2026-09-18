#include "recording.hpp"
#include <fstream>
#include <iostream>
extern "C" {
#include <libavdevice/avdevice.h>
#include <libavformat/avformat.h>
}
using namespace lab;
int main() {
  auto directory=std::filesystem::temp_directory_path()/("airplayvideo-recording-"+uuid());
  try {
    avdevice_register_all();av_log_set_level(AV_LOG_QUIET);
    std::atomic<bool> stop{false};
    Json options={{"source",{{"kind","synthetic"}}},{"width",1280},{"height",720},{"fps",30},
      {"bitrate",4000000},{"encoder","libopenh264"},{"audio",true},{"latency_ms",500}};
    Media media(options,stop,[](const auto &,const auto &){});
    auto peer=media.subscribe();media.start();
    std::string id(32,'a');Json report;std::atomic<bool> finished{false};
    Recording recording(media,directory,id,5,[&](const auto &,const auto &fields){report=fields;finished=true;});
    auto deadline=Clock::now()+std::chrono::seconds(12);int peers=0;
    while(!finished&&Clock::now()<deadline)if(peer->next(stop))++peers;
    require(finished&&report.at("status")=="complete","Bounded recording completes");
    require(peers>500&&!media.failed(),"Recording does not stop the other subscriber");
    AVFormatContext *file=nullptr;
    require(avformat_open_input(&file,(directory/(id+".mkv")).c_str(),nullptr,nullptr)>=0,"Recorded Matroska opens");
    require(avformat_find_stream_info(file,nullptr)>=0&&file->nb_streams==2,"Recording contains both tracks");
    int64_t previous[2]={-1,-1};int counts[2]={0,0};
    AVPacket *packet=av_packet_alloc();
    while(av_read_frame(file,packet)>=0) {
      int index=packet->stream_index;
      require(index<2&&packet->pts>=previous[index],"Recorded source PTS is monotonic");
      if(index==0&&counts[index]==0)require(packet->flags&AV_PKT_FLAG_KEY,"Recording begins with a keyframe");
      previous[index]=packet->pts;++counts[index];av_packet_unref(packet);
    }
    require(counts[0]>=140&&counts[1]>=600,"Recorded source retains video and audio cadence");
    for(int i=0;i<2;++i) {
      double end=previous[i]*av_q2d(file->streams[i]->time_base);
      require(end>4.9&&end<=5.0,"Both tracks retain the same five-second span");
    }
    av_packet_free(&packet);avformat_close_input(&file);
    bool refused=false;try{Recording duplicate(media,directory,id,5,[](const auto &,const auto &){});}catch(...){refused=true;}
    require(refused,"Existing recording cannot be overwritten");
    require((std::filesystem::status(directory/(id+".mkv")).permissions()&std::filesystem::perms::others_read)==std::filesystem::perms::none,"Capture is private");
    stop=true;media.close();std::filesystem::remove_all(directory);
    std::cout<<"Recorded packet timestamps, A/V tracks, bounded duration, subscriber isolation and privacy passed\n";
    return 0;
  } catch(const std::exception &e) {std::cerr<<e.what()<<'\n';std::filesystem::remove_all(directory);return 1;}
}
