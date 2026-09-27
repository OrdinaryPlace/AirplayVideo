#pragma once
#include <cstdint>
extern "C" {
#include <libavutil/mathematics.h>
}

namespace lab {
// Device capture already runs at the requested rate. Quantizing its wall-clock
// timestamps onto a second, unrelated grid can discard valid adjacent frames
// when normal capture jitter straddles a rounding boundary.
class FrameRateGate {
  int fps_;
  bool source_paced_;
  int64_t origin_ = -1;
  int64_t last_ = -1;
public:
  FrameRateGate(int fps, bool source_paced):fps_(fps),source_paced_(source_paced) {}
  bool accept(int64_t pts_us) {
    if(pts_us<0) return false;
    // A muxed source can use coarse timestamp units (for example milliseconds).
    // Its startup offset is unrelated to the output frame grid. Anchor that
    // grid to the first source PTS so the offset cannot discard valid frames.
    if(origin_<0) origin_=pts_us;
    const int64_t position=source_paced_ ? pts_us : av_rescale_q(pts_us-origin_,{1,1000000},{1,fps_});
    if(position<=last_) return false;
    last_=position;
    return true;
  }
};
}
