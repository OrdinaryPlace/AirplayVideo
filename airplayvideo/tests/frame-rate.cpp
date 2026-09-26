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
    std::cout<<"Source-clock phase/jitter preserved at 30/60 fps; tuner downsampling retained\n";
    return 0;
  } catch(const std::exception &e) {std::cerr<<e.what()<<'\n';return 1;}
}
