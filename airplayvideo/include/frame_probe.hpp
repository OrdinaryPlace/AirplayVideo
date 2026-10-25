#pragma once
#include "picture_cadence.hpp"
#include <chrono>
#include <mutex>

namespace lab {
inline int64_t frame_probe_now() {
  return std::chrono::duration_cast<std::chrono::microseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
}
struct ProbeTiming {
  size_t count=0;
  double total=0,mean=0,p95=0,max=0;
};
struct ProbeGaps { double median_ms=0,p95_ms=0,max_ms=0; size_t gaps_over_50ms=0; };
struct ProbeRegion {
  std::string name;
  size_t exact_updates=0;
  double exact_updates_per_second=0,longest_exact_hold_ms=0;
  std::vector<picture::Threshold> thresholds;
};
struct FrameProbeSummary {
  size_t capture_frames=0,nonmonotonic_pts=0,accepted=0,rejected=0;
  size_t converted_frames=0,encoded_frames=0,picture_frames=0;
  ProbeGaps capture_pts;
  std::array<ProbeTiming,4> timings;
  std::vector<ProbeRegion> regions;
};

// Opt-in only. Production playback never constructs this object. All stages
// share one fixed steady-clock window; samples and pixels never leave it.
class FrameProbe {
public:
  static constexpr int64_t warmup_us=2000000,duration_us=15000000;
  static constexpr size_t maximum_frames=2000;
  enum Timing { Conversion,Upload,Encode,Observation };
private:
  struct Region {
    const char *name;
    int x,y,w,h;
    std::vector<uint8_t> previous;
    size_t updates=0;
    int64_t accepted=0,longest=0;
  };
  mutable std::mutex mutex_;
  bool armed_=false;
  int64_t start_=0,end_=0,last_pts_=-1,last_picture_=0;
  size_t captured_=0,nonmonotonic_=0,accepted_=0,rejected_=0,converted_=0,encoded_=0,pictures_=0;
  std::vector<int64_t> gaps_;
  std::array<std::vector<int64_t>,4> timings_;
  std::array<Region,3> regions_{{{"center",64,45,192,108,{}},
                                {"left",16,45,128,108,{}},
                                {"right",176,45,128,108,{}}}};
  picture::Cadence changes_;
  bool inside(int64_t now) const {return armed_&&now>=start_&&now<end_;}
  static void bound(bool condition) {if(!condition)throw std::runtime_error("Probe bounds");}
  static void increment(size_t &value) {bound(value<maximum_frames);++value;}
public:
  void arm(int64_t now) {
    std::lock_guard lock(mutex_);
    if(!armed_) {start_=now+warmup_us;end_=start_+duration_us;armed_=true;}
  }
  bool armed() const {std::lock_guard lock(mutex_);return armed_;}
  bool finished(int64_t now) const {std::lock_guard lock(mutex_);return armed_&&now>=end_;}
  void capture(int64_t pts,int64_t now) {
    std::lock_guard lock(mutex_);if(!inside(now))return;
    increment(captured_);
    if(pts<0||(last_pts_>=0&&pts<=last_pts_)) {++nonmonotonic_;return;}
    if(last_pts_>=0) {bound(pts-last_pts_<=60000000);gaps_.push_back(pts-last_pts_);}
    last_pts_=pts;
  }
  void gate(bool accepted,int64_t now) {
    std::lock_guard lock(mutex_);if(!inside(now))return;
    increment(accepted?accepted_:rejected_);
  }
  bool converted(int64_t now) {
    std::lock_guard lock(mutex_);if(!inside(now))return false;
    increment(converted_);return true;
  }
  void encoded(int64_t now) {
    std::lock_guard lock(mutex_);if(inside(now))increment(encoded_);
  }
  void timing(Timing stage,int64_t before,int64_t after) {
    std::lock_guard lock(mutex_);if(!inside(after))return;
    bound(after>=before&&after-before<=60000000);
    auto &values=timings_[stage];bound(values.size()<maximum_frames);
    values.push_back(after-before);
  }
  // The caller already checked converted(now); keep the same observation time
  // even when computing these aggregates crosses the end of the sample window.
  void observe_luma(const uint8_t *luma,int width,int height,int stride,int64_t now) {
    std::lock_guard lock(mutex_);
    bound(inside(now)&&luma&&width>=picture::width&&height>=picture::height&&
          width<=1920&&height<=1080&&stride>=width&&stride<=16384);
    increment(pictures_);
    std::vector<uint8_t> gray(size_t(picture::width)*picture::height);
    for(int y=0;y<picture::height;++y)for(int x=0;x<picture::width;++x)
      gray[size_t(y)*picture::width+x]=luma[size_t(y*height/picture::height)*stride+x*width/picture::width];
    for(auto &region:regions_) {
      std::vector<uint8_t> pixels;pixels.reserve(size_t(region.w)*region.h);
      for(int y=region.y;y<region.y+region.h;++y)
        pixels.insert(pixels.end(),gray.begin()+size_t(y)*picture::width+region.x,
                      gray.begin()+size_t(y)*picture::width+region.x+region.w);
      if(region.previous.empty())region.accepted=now;
      else if(pixels!=region.previous) {
        ++region.updates;region.longest=std::max(region.longest,now-region.accepted);region.accepted=now;
      }
      region.previous=std::move(pixels);
    }
    last_picture_=now;
    changes_.add(gray,now-start_);
  }
  FrameProbeSummary summary() const {
    std::lock_guard lock(mutex_);
    FrameProbeSummary result;
    result.capture_frames=captured_;result.nonmonotonic_pts=nonmonotonic_;
    result.accepted=accepted_;result.rejected=rejected_;
    result.converted_frames=converted_;result.encoded_frames=encoded_;result.picture_frames=pictures_;
    if(!gaps_.empty()) {
      auto values=gaps_;std::sort(values.begin(),values.end());
      result.capture_pts={double(values[(values.size()-1)/2]+values[values.size()/2])/2000,
        double(values[size_t(std::ceil(values.size()*.95))-1])/1000,double(values.back())/1000,
        size_t(std::count_if(values.begin(),values.end(),[](auto value){return value>50000;}))};
    }
    for(size_t i=0;i<timings_.size();++i)if(!timings_[i].empty()) {
      auto values=timings_[i];std::sort(values.begin(),values.end());
      int64_t total=0;for(auto value:values) {bound(total<=60000000-value);total+=value;}
      result.timings[i]={values.size(),double(total)/1000,double(total)/values.size()/1000,
        double(values[size_t(std::ceil(values.size()*.95))-1])/1000,double(values.back())/1000};
    }
    std::vector<picture::Region> thresholds;
    if(pictures_>=2)thresholds=changes_.summary().regions;
    for(size_t i=0;i<regions_.size();++i) {
      const auto &region=regions_[i];
      ProbeRegion output{region.name,region.updates,double(region.updates)/15,
        pictures_?double(std::max(region.longest,last_picture_-region.accepted))/1000:0,{}};
      if(!thresholds.empty())output.thresholds=thresholds[i].thresholds;
      else for(int delta:{1,2,4})for(double fraction:{.005,.02})
        output.thresholds.push_back({delta,fraction,0,0,0});
      for(auto &threshold:output.thresholds)threshold.updates_per_second=double(threshold.updates)/15;
      result.regions.push_back(std::move(output));
    }
    return result;
  }
};
}
