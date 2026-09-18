#include "stream.hpp"
#include <bit>
#include <iostream>
#include <sodium.h>
extern "C" {
#include <libavdevice/avdevice.h>
#include <libavutil/log.h>
}
using namespace lab;
int main(int argc,char **argv) {
  try {
    require(sodium_init()>=0,"Crypto initialization");
    avdevice_register_all();av_log_set_level(AV_LOG_QUIET);
    require(ntp_delta(7200000000ULL)==(uint64_t(7200)<<32),"NTP conversion must remain correct beyond 71 minutes");
    auto header=mirror_header(10,1,ntp_now(),1280,720);
    require(std::bit_cast<float>(uint32_t(read_le(std::span(header).subspan(16,4))))==1280,"720p header width");
    require(std::bit_cast<float>(uint32_t(read_le(std::span(header).subspan(20,4))))==720,"720p header height");
    Bytes pcm(352*4,37),key=random_bytes(32);
    auto wire=audio_packet(pcm,key,5,65535,0xffffffe0,12345,true);
    require(wire.size()==1444&&wire[0]==0x80&&wire[1]==0xe0,"PCM packet framing and MTU");
    require(wire[2]==0xff&&wire[3]==0xff,"RTP sequence encoding");
    require(read_le(std::span(wire).last(8))==5,"Audio nonce trailer");
    require(open_sealed(key,5,std::span(wire).subspan(4,8),std::span(wire).subspan(12,wire.size()-20))==pcm,"Audio payload authentication");
    bool rejected=false;wire[8]^=1;
    try{open_sealed(key,5,std::span(wire).subspan(4,8),std::span(wire).subspan(12,wire.size()-20));}catch(...){rejected=true;}
    require(rejected,"Audio SSRC tamper rejection");
    std::atomic<bool> stop{false};
    Json options={{"source",{{"kind","synthetic"}}},{"width",1280},{"height",720},{"fps",30},{"bitrate",4000000},{"encoder","libopenh264"},{"audio",true},{"latency_ms",1500}};
    bool browser=argc==2&&std::string(argv[1])=="--browser";
    if(browser) {options["source"]={{"kind","browser"}}; options["width"]=1920;options["height"]=1080;}
    else if(argc==2) options["source"]={{"kind","fixture"},{"url",argv[1]}};
    std::atomic<bool> ready{false};
    Media media(options,stop,[&](const std::string &stage,const Json &details){if(stage=="media_ready")ready=true;else if(stage=="source_error")std::cerr<<details.at("message").get<std::string>()<<'\n';});
    auto first=media.subscribe(),second=media.subscribe();
    media.start();
    uint64_t video=0,audio=0;int peak=0;int64_t last_video=-1,last_audio=-1;
    auto until=Clock::now()+std::chrono::seconds(12);
    while(Clock::now()<until&&(video<60||audio<200)) {
      auto a=first->next(stop);if(!a){require(!media.failed(),"Media source failed");continue;}
      auto b=second->next(stop);require(a==b,"Receivers must share identical immutable encoded packets");
      if(a->audio) {
        require(a->pts_us>last_audio&&a->pcm.size()==1408,"Monotonic complete audio packets");last_audio=a->pts_us;++audio;
        for(size_t i=0;i<a->pcm.size();i+=2)peak=std::max(peak,std::abs(int(int16_t(uint16_t(a->pcm[i])<<8|a->pcm[i+1]))));
      } else {
        require(a->pts_us>last_video&&!a->video.avcc.empty()&&!a->video.payload.empty(),"Monotonic complete video frames");last_video=a->pts_us;++video;
      }
    }
    first->close();
    auto next=second->next(stop);require(bool(next),"Stopping one subscriber must retain the other");
    stop=true;media.close();
    require(ready&&video>=60&&audio>=200&&(browser||peak>1000),"A/V source completeness");
    require(std::abs(last_video-last_audio)<200000,"Shared video and audio timeline");
    std::cout<<"Media framing, two-hour clock, shared encoding, audio and independent subscriber checks passed\n";
    return 0;
  }catch(const std::exception &e){std::cerr<<e.what()<<'\n';return 1;}
}
