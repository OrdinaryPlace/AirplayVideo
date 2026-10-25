#include "frame_probe.hpp"
#include <iostream>
using namespace lab;
void check(bool condition,const char *message) {if(!condition)throw std::runtime_error(message);}
template<class F>void rejects(F operation){bool rejected=false;try{operation();}catch(...){rejected=true;}check(rejected,"Expected bounded rejection");}
std::vector<uint8_t> image(int index,bool overlay=false) {
  std::vector<uint8_t> pixels(size_t(picture::width)*picture::height);
  for(int y=0;y<picture::height;++y)for(int x=0;x<picture::width;++x)
    pixels[size_t(y)*picture::width+x]=overlay&&y<30?(index%2?0:255):((x+index*7)%40<20?35:215);
  return pixels;
}
void sample(FrameProbe &probe,const std::vector<uint8_t> &pixels,int64_t at) {
  probe.capture(at,at);probe.gate(true,at);
  probe.timing(FrameProbe::Conversion,at,at+100);
  if(probe.converted(at+100)) {
    probe.observe_luma(pixels.data(),picture::width,picture::height,picture::width,at+100);
    probe.timing(FrameProbe::Observation,at+100,at+110);
  }
  probe.encoded(at+200);probe.timing(FrameProbe::Encode,at+150,at+200);
}
int main() {
  try {
    FrameProbe smooth,held,still;
    smooth.arm(0);held.arm(0);still.arm(0);
    for(int i=0;i<540;++i) {
      const int64_t at=int64_t(i)*1000000/30;
      sample(smooth,image(i),at);sample(held,image(i/3),at);
      auto static_image=image(0);for(int y=0;y<30;++y)for(int x=0;x<picture::width;++x)
        static_image[size_t(y)*picture::width+x]=i%2?0:255;
      sample(still,static_image,at);
    }
    const auto moving=smooth.summary(),repeated=held.summary(),static_field=still.summary();
    check(moving.capture_frames==450&&moving.accepted==450&&moving.converted_frames==450&&moving.encoded_frames==450,"All stages share fixed fifteen-second window after warmup");
    check(moving.picture_frames==450&&moving.rejected==0&&moving.nonmonotonic_pts==0,"Picture observation counts converted accepted frames");
    check(moving.capture_pts.max_ms<33.335&&moving.capture_pts.gaps_over_50ms==0,"Raw capture PTS are retained");
    for(size_t i=0;i<3;++i) {
      check(moving.regions[i].exact_updates==449,"Moving pictures update on every sample");
      check(repeated.regions[i].exact_updates==149,"Ten picture updates sampled at thirty fps remain distinct from sample rate");
      check(repeated.regions[i].longest_exact_hold_ms>=99.9&&repeated.regions[i].longest_exact_hold_ms<=100.1,"Exact holds use actual observation time");
      check(static_field.regions[i].exact_updates==0&&static_field.regions[i].longest_exact_hold_ms>14900,"Scoreboard-only updates cannot establish field motion");
      for(const auto &threshold:repeated.regions[i].thresholds)
        check(threshold.updates==149&&std::abs(threshold.updates_per_second-149.0/15)<.0001,"Threshold rate shares wall-clock denominator");
    }
    check(moving.timings[FrameProbe::Upload].count==0,"Unused upload stage is not claimed");
    check(moving.timings[FrameProbe::Observation].max==.01&&moving.timings[FrameProbe::Conversion].mean==.1,"Observation overhead and conversion call time are reported separately");
    FrameProbe pts;pts.arm(0);
    pts.capture(0,2000000);pts.gate(true,2000000);pts.capture(0,2033333);pts.gate(false,2033333);
    pts.capture(-1,2066666);pts.gate(false,2066666);pts.capture(100000,2100000);
    const auto irregular=pts.summary();check(irregular.nonmonotonic_pts==2&&irregular.rejected==2&&irregular.capture_pts.max_ms==100,"PTS regressions and gate rejection remain visible");
    FrameProbe capped;capped.arm(0);for(size_t i=0;i<FrameProbe::maximum_frames;++i)capped.capture(i,int64_t(2000000+i));
    rejects([&]{capped.capture(2001,2002001);});
    FrameProbe invalid;invalid.arm(0);auto pixels=image(0);
    rejects([&]{invalid.observe_luma(pixels.data(),1,1,1,2000000);});
    rejects([&]{invalid.timing(FrameProbe::Encode,2000010,2000000);});
    check(!invalid.finished(16999999)&&invalid.finished(17000000),"Window closes exactly after warmup plus fixed sample");
    invalid.arm(100000000);check(invalid.finished(17000000),"Repeated ready events cannot reset the measurement");
    std::cout<<"Stage window, repeated pictures, moving pictures, overlays, overhead and bounds passed\n";
    return 0;
  } catch(const std::exception &error) {std::cerr<<error.what()<<'\n';return 1;}
}
