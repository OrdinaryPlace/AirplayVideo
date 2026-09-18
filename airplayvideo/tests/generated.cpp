#include "generated.hpp"
#include "stream.hpp"
#include <fstream>
#include <iostream>
extern "C" {
#include <libavutil/log.h>
}
using namespace lab;
int main(int argc,char **argv) {
  try {
    av_log_set_level(AV_LOG_QUIET);
    Json settings={{"title","Time to head out"},{"tagline","Shoes on. Bags ready. Have a great day!"},{"duration_seconds",61},{"display","countdown"},{"timezone","UTC"}};
    GeneratedVideo video(settings,1920,1080);
    require(video.timer(0,0)=="01:01"&&video.timer(.99,0)=="01:01"&&video.timer(1,0)=="01:00"&&video.timer(61,0)=="00:00"&&video.timer(62,0)=="00:00","Countdown boundaries");
    auto png=video.png(0,0);require(png.size()>10000&&png[0]==137&&png[1]=='P',"Native PNG preview");
    require(video.png(30,0)!=png,"Countdown and animation change pixels");
    if(argc==2) {std::ofstream file(argv[1],std::ios::binary);file.write(reinterpret_cast<const char*>(png.data()),png.size());}
    settings["display"]="time";settings["timezone"]="America/New_York";
    GeneratedVideo clock(settings,1280,720);require(clock.timer(0,0)=="19:00:00","Clock uses selected zone");
    require(clock.timer(0,1782864000)=="20:00:00","Clock observes daylight saving time");
    settings["display"]="neither";settings["title"]="Café <b>plain text</b> — Привет";settings["tagline"]=std::string(240,'W');
    GeneratedVideo plain(settings,1920,1080);require(plain.timer(0,0).empty()&&plain.png(0,0)!=plain.png(2,0),"No timer still animates; Unicode and long lines render");
    settings["duration_seconds"]=0;bool rejected=false;
    try{GeneratedVideo invalid(settings,1920,1080);}catch(...){rejected=true;}
    require(rejected,"Zero duration rejected");settings["duration_seconds"]=1;
    Json options={{"source",{{"kind","generated"},{"generated",settings}}},{"width",1280},{"height",720},{"fps",30},{"bitrate",4000000},{"encoder","libopenh264"},{"audio",false},{"latency_ms",500}};
    std::atomic<bool> stop{false},ready{false};
    Media media(options,stop,[&](const auto &event,const auto &){if(event=="media_ready")ready=true;});media.start();
    auto limit=Clock::now()+std::chrono::seconds(8);
    while(!ready&&Clock::now()<limit&&!media.failed()) std::this_thread::sleep_for(std::chrono::milliseconds(10));
    require(ready&&!media.failed(),"Generated encoder becomes ready without a browser or sound");
    std::this_thread::sleep_for(std::chrono::milliseconds(1200));
    require(!media.completed(),"Connection setup must not consume video duration");
    auto subscriber=media.subscribe();auto joined=Clock::now();int frames=0;int64_t last=-1;
    while(!media.completed()&&!media.failed()&&Clock::now()<limit) {
      auto p=subscriber->next(stop);if(!p)continue;
      require(!p->audio&&p->pts_us>last&&!p->video.avcc.empty(),"Monotonic encoded video without audio");
      if(frames==0)require(p->video.keyframe,"First connected frame is independently decodable");
      last=p->pts_us;++frames;
    }
    require(media.completed()&&!media.failed()&&frames>=10,"Generated stream completes naturally");
    require(Clock::now()-joined>=std::chrono::milliseconds(1500),"Duration plus receiver buffer is honored");
    stop=true;media.close();std::cout<<"Generated text, timers, time zones, animation, late connection, keyframe and natural completion passed\n";return 0;
  }catch(const std::exception &e){std::cerr<<e.what()<<'\n';return 1;}
}
