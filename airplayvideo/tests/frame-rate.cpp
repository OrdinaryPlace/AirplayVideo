#include "frame_rate.hpp"
#include <iostream>
#include <stdexcept>

void require(bool value,const char *message) {if(!value)throw std::runtime_error(message);}
int main() {
  try {
    for(int fps:{30,60}) for(int phase=0;phase<8;++phase) {
      lab::FrameRateGate paced(fps,true);
      // The X11 clock can start anywhere relative to the stream epoch. Its
      // ordinary wake-up jitter must not drop neighboring source-rate frames.
      int64_t last=-1;
      for(int i=0;i<fps*10;++i) {
        int64_t timestamp=1000000+phase*1000000/(fps*8)+int64_t(i)*1000000/fps+(i%4<2?2000:-2000);
        require(paced.accept(timestamp),"Source-paced frame lost at a clock-grid boundary");
        require(!paced.accept(timestamp),"Duplicate timestamp accepted");
        require(!paced.accept(timestamp-100),"Regressing timestamp accepted");
        last=timestamp;
      }
      require(!paced.accept(-1),"Negative source time accepted");
      require(paced.accept(last+1000000/fps),"Rejected input damaged subsequent capture");
    }
    // The tuner still needs to reduce a higher source rate to the configured
    // output. The browser fix must not remove that independent requirement.
    lab::FrameRateGate tuner(30,false);
    int kept=0;
    for(int i=0;i<600;++i)kept+=tuner.accept(int64_t(i)*1000000/60);
    require(kept>=300&&kept<=301,"60 fps tuner was not limited to 30 fps");

    // Matroska rounds 30 fps to millisecond PTS (0,33,67,100,...). A startup
    // offset near half an output tick used to lose every third frame, including
    // the leading flash frame in the independent content-timing regression.
    for(int64_t phase:{0,1000,16000,16400,16600,16700,17000,33000,1000000}) {
      lab::FrameRateGate muxed(30,false);
      for(int i=0;i<210;++i) {
        int64_t pts=av_rescale_q(i,{1,30},{1,1000})*1000+phase;
        require(muxed.accept(pts),"Millisecond source frame lost because of startup phase");
      }
    }

    // Translation must not change the chosen frames, including fractional
    // 29.97/59.94 fps sources. Retain all lower-rate frames and limit the higher
    // source to 30 fps without modifying its actual presentation timestamps.
    for(int source_rate:{30000,60000}) for(int64_t phase:{0,16400,16700,987654321}) {
      lab::FrameRateGate baseline(30,false),shifted(30,false);
      int accepted=0;
      for(int i=0;i<600;++i) {
        int64_t pts=av_rescale_q(i,{1001,source_rate},{1,1000000});
        bool keep=baseline.accept(pts);
        require(shifted.accept(pts+phase)==keep,"Fractional-rate selection changed with startup phase");
        accepted+=keep;
      }
      require(source_rate==30000 ? accepted==600 : accepted>=300&&accepted<=301,
              "Fractional-rate source was not preserved or downsampled correctly");
    }

    lab::FrameRateGate gaps(30,false);
    require(!gaps.accept(-1)&&gaps.accept(16400),"Invalid PTS changed the initial frame origin");
    require(!gaps.accept(16400)&&!gaps.accept(16000),"Duplicate or regressing source PTS accepted");
    require(gaps.accept(5016400),"Forward gap was rejected");
    require(!gaps.accept(5000000)&&gaps.accept(5049733),"Gap or rejected PTS reset the source grid");
    lab::FrameRateGate restarted(30,false);
    require(restarted.accept(1000),"A new source did not start with a fresh frame grid");
    std::cout<<"Source pacing, muxed/fractional startup phases, downsampling and gaps preserved\n";
    return 0;
  } catch(const std::exception &e) {std::cerr<<e.what()<<'\n';return 1;}
}
