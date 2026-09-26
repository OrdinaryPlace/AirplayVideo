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
    // Independently recover playout time from the two RTP positions and NTP.
    // SETUP is checked separately: a self-imposed receiver clamp previously
    // allowed a wrong hard audio minimum and fixed video SETUP to pass.
    const uint64_t capture_epoch=uint64_t(12345)<<32;
    auto network_integer=[](std::span<const uint8_t> value) {
      uint64_t result=0; for(auto byte:value) result=(result<<8)|byte; return result;
    };
    // Receiver model: control and audio include the NTP epoch; screen headers
    // do not. Checking only equal raw A/V fields missed this wire distinction.
    const uint64_t ntp_epoch=2208988800ULL<<32;
    const uint64_t receiver_clock=capture_epoch+ntp_delta(7654321);
    int64_t receiver_offset=0;
    for(size_t size:{32U,48U}) {
      Bytes request(size);request[0]=size==32?0x80:0x23;
      if(size==32){request[1]=0xd2;request[2]=0x12;request[3]=0x34;}
      const size_t origin=size==32?8:24,received=size==32?16:32,transmitted=size==32?24:40;
      be(request,size-8,0xfedcba9876543210ULL,8);
      auto reply=timing_response(request,capture_epoch,capture_epoch+ntp_delta(1000));
      require(reply.size()==size,"Timing response framing");
      require(network_integer(std::span(reply).subspan(origin,8))==0xfedcba9876543210ULL,"Echo receiver timestamp unchanged");
      require(network_integer(std::span(reply).subspan(received,8))==ntp_epoch+capture_epoch,"Control receive time uses NTP epoch");
      require(network_integer(std::span(reply).subspan(transmitted,8))==ntp_epoch+capture_epoch+ntp_delta(1000),"Control transmit time uses NTP epoch");
      if(size==32)require(reply[1]==0xd3&&reply[2]==0x12&&reply[3]==0x34,"Timing response type and sequence");
      else require((reply[0]&7)==4,"NTP server response mode");
      require(timing_response(reply,capture_epoch,capture_epoch).empty(),"Never answer a timing response");
      require(timing_response(std::span(request).first(size-1),capture_epoch,capture_epoch).empty(),"Ignore truncated timing requests");
      receiver_offset=receiver_clock-(network_integer(std::span(reply).subspan(received,8))-ntp_epoch);
    }
    for(int lead:{500,750,1000,1500,2000}) {
      const auto setup=audio_timing_setup(lead);
      require(setup.at("usingScreen")==true,"Audio belongs to the screen session");
      require(setup.at("isMedia")==false,"Screen audio must not enter the receiver's main media path");
      require(setup.at("latencyMin")==0&&setup.at("latencyMax")==lead*44100/1000,"Screen audio latency window");
      require(video_timing_setup(lead).at("latencyMs")==lead,"Video SETUP must match its presentation timestamps");
      const uint64_t epoch=capture_epoch+ntp_delta(uint64_t(lead)*1000);
      for(int64_t pts:{0LL,2371234LL,7200123456LL,100000000000LL}) {
        uint32_t rtp=0xffffffe0U+uint32_t(uint64_t(pts)*44100/1000000);
        auto sync=audio_sync_packet(epoch,pts,rtp,lead,pts==0);
        require(sync.size()==20&&sync[0]==(pts==0?0x90:0x80)&&sync[1]==0xd4,"Sync framing");
        uint32_t playing=network_integer(std::span(sync).subspan(4,4));
        uint32_t sent=network_integer(std::span(sync).subspan(16,4));
        require(sent==rtp,"Sync reference identifies the transmitted PCM packet");
        const uint32_t advance=sent-playing;
        require(advance==uint32_t(lead*44100/1000),"Sync positions encode the requested lead");
        uint64_t audio_clock=network_integer(std::span(sync).subspan(8,8))-ntp_epoch;
        long double audio_time=(audio_clock+receiver_offset)/4294967296.0L+advance/44100.0L;
        auto frame=mirror_header(10,0,epoch+ntp_delta(pts),1280,720);
        uint64_t video_clock=read_le(std::span(frame).subspan(8,8));
        long double video_time=(video_clock+receiver_offset)/4294967296.0L;
        require(std::abs(audio_time-video_time)<0.000024L,"Receiver-mapped A/V presentation agrees within one sample");
        require(video_clock+receiver_offset==receiver_clock+ntp_delta(uint64_t(lead)*1000)+ntp_delta(pts),"Receiver presents video at the configured deadline");
      }
    }
    auto header=mirror_header(10,1,ntp_now(),1280,720);
    require(std::bit_cast<float>(uint32_t(read_le(std::span(header).subspan(16,4))))==1280,"720p header width");
    require(std::bit_cast<float>(uint32_t(read_le(std::span(header).subspan(20,4))))==720,"720p header height");
    Bytes pcm(352*4,37),key=random_bytes(32);
    auto wire=audio_packet(pcm,key,5,65535,0xffffffe0,12345,true);
    require(wire.size()==1448&&wire[0]==0x80&&wire[1]==0x60,"ALAC screen packet framing and MTU");
    require(wire[2]==0xff&&wire[3]==0xff,"RTP sequence encoding");
    require(read_le(std::span(wire).last(8))==5,"Audio nonce trailer");
    require(open_sealed(key,5,std::span(wire).subspan(4,8),std::span(wire).subspan(12,wire.size()-20))==alac_frame(pcm),"Audio payload authentication");
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
