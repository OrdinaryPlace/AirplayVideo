#include "picture_cadence.hpp"
#include <iostream>
using namespace lab::picture;
void require(bool value,const char *message){if(!value)throw std::runtime_error(message);}
template<class F> void rejects(F operation){bool rejected=false;try{operation();}catch(...){rejected=true;}require(rejected,"Invalid analysis input must be rejected");}
std::vector<uint8_t> image(int movement,int noise=0,bool overlay=false) {
  std::vector<uint8_t> value(size_t(width)*height);
  for(int y=0;y<height;++y)for(int x=0;x<width;++x) {
    int level=((x+movement*7)%40<20)?45:205;
    level+=noise*((x+y)%2?1:-1);
    if(overlay&&y<30)level=movement%2?0:255;
    value[size_t(y)*width+x]=uint8_t(std::clamp(level,0,255));
  }
  return value;
}
int main() {
  try {
    Cadence smooth,held3,held4,noise;
    for(int i=0;i<120;++i) {
      const int64_t pts=int64_t(i)*1000000/30;
      smooth.add(image(i),pts);held3.add(image(i/3),pts);held4.add(image(i/4),pts);
      noise.add(image(0,i%2?1:-1),pts);
    }
    const auto a=smooth.summary(),b=held3.summary(),c=held4.summary(),d=noise.summary();
    require(a.video_frames==120&&std::abs(a.encoded_fps-30)<0.001,"Preserve original 30fps timestamps");
    require(a.regions.size()==3,"Fixed three-region contract");
    for(size_t r=0;r<a.regions.size();++r)for(size_t t=0;t<6;++t) {
      require(a.regions[r].thresholds[t].updates==119,"Moving field advances every sample");
      require(b.regions[r].thresholds[t].updates==39,"Three-sample holds preserve visible change cadence");
      require(c.regions[r].thresholds[t].updates==29,"Four-sample holds preserve visible change cadence");
      require(b.regions[r].thresholds[t].longest_hold_ms>99&&b.regions[r].thresholds[t].longest_hold_ms<101,"Hold durations use presentation time");
      if(d.regions[r].thresholds[t].luma_delta>=2)
        require(d.regions[r].thresholds[t].updates==0,"Blurred pixel noise is excluded at conservative thresholds");
    }
    Cadence static_field,scoreboard;
    for(int i=0;i<60;++i) {
      auto field=image(0),with_overlay=field;
      for(int y=0;y<30;++y)for(int x=0;x<width;++x)with_overlay[size_t(y)*width+x]=i%2?0:255;
      static_field.add(field,int64_t(i)*1000000/30);
      scoreboard.add(with_overlay,int64_t(i)*1000000/30);
    }
    for(const auto &r:scoreboard.summary().regions)for(const auto &t:r.thresholds)
      require(t.updates==0&&t.longest_hold_ms>1966,"Scoreboard-only changes are not moving-field updates");
    for(const auto &r:static_field.summary().regions)for(const auto &t:r.thresholds)
      require(t.updates==0,"Static content is reported as low change, never inferred source FPS");
    Cadence gaps;gaps.add(image(0),0);gaps.add(image(1),33000);gaps.add(image(2),67000);gaps.add(image(3),167000);
    auto timing=gaps.summary();
    require(timing.gaps_over_50ms==1&&timing.max_ms==100&&timing.median_ms==34,"Report real jitter and gaps");
    Cadence even;even.add(image(0),0);even.add(image(1),33000);even.add(image(2),67000);
    require(even.summary().median_ms==33.5,"Even-length PTS samples use the conventional median");
    rejects([&]{gaps.add(image(4),167000);});
    rejects([&]{gaps.add(image(4),166000);});
    rejects([&]{gaps.add(image(4),max_duration_us+1);});
    rejects([&]{Cadence empty;empty.summary();});
    rejects([&]{Cadence one;one.add(image(0),0);one.summary();});
    rejects([&]{Cadence invalid;invalid.add(std::vector<uint8_t>(1),0);});
    rejects([&]{Cadence invalid;invalid.add(image(0),-1);});
    Cadence limited;auto still=image(0);
    for(size_t i=0;i<max_frames;++i)limited.add(still,int64_t(i)*10000);
    require(limited.summary().video_frames==max_frames,"Maximum bounded frame set is usable");
    rejects([&]{limited.add(still,int64_t(max_frames)*10000);});
    std::cout<<"Picture cadence synthetic motion, holds, noise, overlays, timestamps and bounds passed\n";return 0;
  } catch(const std::exception &e){std::cerr<<e.what()<<'\n';return 1;}
}
