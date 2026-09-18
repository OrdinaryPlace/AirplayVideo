// A real MPEG-TS mid-GOP join: 1080i MPEG-2, B pictures and 48 kHz AC-3.
#include "stream.hpp"
#include <fstream>
#include <iostream>
extern "C" {
#include <libavdevice/avdevice.h>
}
using namespace lab;
int main(int argc,char **argv) {
  std::filesystem::path damaged;
  try {
    require(argc==2,"Broadcast fixture required");
    avdevice_register_all();av_log_set_level(AV_LOG_QUIET);
    Json options={{"source",{{"kind","fixture"},{"url",argv[1]}}},{"width",1920},{"height",1080},
      {"fps",30},{"bitrate",4000000},{"encoder","libopenh264"},{"audio",true},{"deinterlace",true},{"latency_ms",500}};
    std::atomic<bool> stop{false};std::atomic<unsigned> recovered{0};
    auto note=[&](const std::string &event,const Json &detail) {
      if(event=="source_recovered")recovered+=detail.at("invalid_packets").get<unsigned>();
      if(event=="source_error")std::cerr<<detail.at("message").get<std::string>()<<'\n';
    };
    {
      Media media(options,stop,note);auto sink=media.subscribe();media.start();
      unsigned videos=0,audios=0;int peak=0;int64_t first=-1,last_video=-1,last_audio=-1;
      std::vector<int64_t> audio_age;
      auto until=Clock::now()+std::chrono::seconds(12);
      while(Clock::now()<until&&(videos<90||audios<300)) {
        auto p=sink->next(stop);require(!media.failed(),"Broadcast must recover from its incomplete first GOP");
        if(!p)continue;
        if(first<0){require(!p->audio&&p->video.keyframe,"Subscriber starts on a decodable picture");first=p->pts_us;}
        if(p->audio) {
          require(p->pts_us>last_audio,"Broadcast audio PTS advances");last_audio=p->pts_us;++audios;
          if(p->pts_us>first+500000)audio_age.push_back(p->available_us-p->pts_us);
          for(size_t i=0;i<p->pcm.size();i+=2)peak=std::max(peak,std::abs(int(int16_t(uint16_t(p->pcm[i])<<8|p->pcm[i+1]))));
        } else {
          require(p->pts_us>last_video&&!p->video.avcc.empty(),"Broadcast video PTS advances");last_video=p->pts_us;++videos;
        }
      }
      require(recovered>0&&videos>=90&&audios>=300&&peak>1000,"Recovered broadcast supplies picture and non-silent audio");
      require(std::abs(last_audio-last_video)<200000,"Tracks retain their shared clock");
      require(audio_age.size()>100,"Enough steady-state audio samples");std::sort(audio_age.begin(),audio_age.end());
      require(audio_age[audio_age.size()/2]<150000,"Paced video must not block demuxing audio behind it");
      std::cout<<"Recovered "<<recovered<<" invalid packets; median audio age "<<audio_age[audio_age.size()/2]<<" us\n";
      stop=true;media.close();
    }
    // Stop while startup queues are full or workers wait for future PTS.
    stop=false;auto start=Clock::now();
    {Media media(options,stop,note);media.start();std::this_thread::sleep_for(std::chrono::milliseconds(100));stop=true;media.close();}
    require(Clock::now()-start<std::chrono::seconds(2),"Cancelling broadcast startup joins every worker promptly");
    // Never recover forever if there is no usable sequence header at all.
    std::ifstream file(argv[1],std::ios::binary);Bytes data((std::istreambuf_iterator<char>(file)),{});
    unsigned removed=0;
    for(size_t i=0;i+3<data.size();++i)if(data[i]==0&&data[i+1]==0&&data[i+2]==1&&data[i+3]==0xb3){data[i+3]=0xb2;++removed;}
    require(removed>0,"Fixture includes later recovery headers");
    damaged=std::filesystem::temp_directory_path()/("airplayvideo-damaged-"+uuid()+".ts");
    {std::ofstream out(damaged,std::ios::binary);out.write(reinterpret_cast<const char*>(data.data()),data.size());}
    options["source"]["url"]=damaged.string();stop=false;start=Clock::now();
    {
      Media media(options,stop,note);media.start();
      while(!media.failed()&&Clock::now()-start<std::chrono::seconds(11))std::this_thread::sleep_for(std::chrono::milliseconds(20));
      bool failed=media.failed();stop=true;media.close();require(failed,"Unrecoverable broadcast must fail within a finite deadline");
    }
    std::filesystem::remove(damaged);
    std::cout<<"1080i/AC-3 recovery, independent pacing, cancellation and damaged-source deadline passed\n";
    return 0;
  }catch(const std::exception &e){std::cerr<<e.what()<<'\n';if(!damaged.empty())std::filesystem::remove(damaged);return 1;}
}
