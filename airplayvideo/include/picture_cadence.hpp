#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

namespace lab::picture {
constexpr int width = 320, height = 180;
constexpr size_t max_frames = 2000;
constexpr int64_t max_duration_us = 31000000;
struct Threshold {
  int luma_delta = 0;
  double changed_fraction = 0;
  size_t updates = 0;
  double updates_per_second = 0, longest_hold_ms = 0;
};
struct Region { std::string name; std::vector<Threshold> thresholds; };
struct Summary {
  size_t video_frames = 0;
  double duration_seconds = 0, encoded_fps = 0;
  double median_ms = 0, p95_ms = 0, max_ms = 0;
  size_t gaps_over_50ms = 0;
  std::vector<Region> regions;
};

// This measures visible changes in fixed field regions, not source-frame identity.
class Cadence {
  struct State {
    Threshold result;
    std::vector<uint8_t> reference;
    int64_t accepted = 0, longest_hold = 0;
  };
  struct Window { const char *name; int x, y, w, h; std::vector<State> states; };
  std::array<Window, 3> windows_{{{"center",64,45,192,108,{}},
                                {"left",16,45,128,108,{}},
                                {"right",176,45,128,108,{}}}};
  size_t frames_ = 0;
  int64_t first_ = 0, last_ = 0;
  std::vector<int64_t> gaps_;
public:
  Cadence() {
    for(auto &window: windows_) for(int delta: {1,2,4}) for(double fraction: {0.005,0.02})
      window.states.push_back({Threshold{delta,fraction}, {}, 0, 0});
  }
  void add(std::span<const uint8_t> grayscale, int64_t pts_us) {
    if(grayscale.size()!=size_t(width*height)||frames_>=max_frames||pts_us<0)
      throw std::runtime_error("bounds");
    if(frames_ && (pts_us<=last_ || pts_us-first_>max_duration_us))
      throw std::runtime_error("timing");
    if(!frames_) first_=pts_us;
    else gaps_.push_back(pts_us-last_);
    last_=pts_us;
    // A small box blur suppresses pixel-level reconstruction noise. It cannot
    // make a static shot a reliable frame counter, hence the threshold sweep.
    std::vector<uint8_t> blurred(grayscale.size());
    for(int y=1;y<height-1;++y) for(int x=1;x<width-1;++x) {
      unsigned total=0;
      for(int dy=-1;dy<=1;++dy) for(int dx=-1;dx<=1;++dx)
        total+=grayscale[size_t(y+dy)*width+x+dx];
      blurred[size_t(y)*width+x]=uint8_t((total+4)/9);
    }
    for(auto &window: windows_) {
      std::vector<uint8_t> pixels;
      pixels.reserve(size_t(window.w)*window.h);
      for(int y=window.y;y<window.y+window.h;++y)
        pixels.insert(pixels.end(),blurred.begin()+size_t(y)*width+window.x,
                      blurred.begin()+size_t(y)*width+window.x+window.w);
      for(auto &state: window.states) {
        if(!frames_) {state.reference=pixels;state.accepted=pts_us;continue;}
        size_t changed=0;
        for(size_t i=0;i<pixels.size();++i)
          changed+=std::abs(int(pixels[i])-int(state.reference[i]))>=state.result.luma_delta;
        if(double(changed)/pixels.size()>=state.result.changed_fraction) {
          state.longest_hold=std::max(state.longest_hold,pts_us-state.accepted);
          ++state.result.updates;
          state.reference=pixels;
          state.accepted=pts_us;
        }
      }
    }
    ++frames_;
  }
  Summary summary() const {
    if(frames_<2||last_<=first_) throw std::runtime_error("timing");
    Summary result;
    result.video_frames=frames_;
    result.duration_seconds=double(last_-first_)/1000000;
    result.encoded_fps=double(frames_-1)/result.duration_seconds;
    auto sorted=gaps_; std::sort(sorted.begin(),sorted.end());
    result.median_ms=(double(sorted[(sorted.size()-1)/2])+sorted[sorted.size()/2])/2000;
    result.p95_ms=double(sorted[size_t(std::ceil(sorted.size()*0.95))-1])/1000;
    result.max_ms=double(sorted.back())/1000;
    result.gaps_over_50ms=std::count_if(sorted.begin(),sorted.end(),[](auto gap){return gap>50000;});
    for(const auto &window: windows_) {
      Region region{window.name,{}};
      for(const auto &state: window.states) {
        auto threshold=state.result;
        threshold.updates_per_second=double(threshold.updates)/result.duration_seconds;
        threshold.longest_hold_ms=double(std::max(state.longest_hold,last_-state.accepted))/1000;
        region.thresholds.push_back(threshold);
      }
      result.regions.push_back(std::move(region));
    }
    return result;
  }
};

Summary analyze_file(const std::string &path);
}
