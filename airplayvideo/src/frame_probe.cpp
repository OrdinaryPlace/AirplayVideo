// Numeric-only observation of the production browser capture/encode pipeline.
#include "stream.hpp"
#include "frame_probe.hpp"
#include "video_rate.hpp"
#include <csignal>
#include <iostream>
#include <regex>
#include <set>
#include <sys/resource.h>
#include <unistd.h>
extern "C" {
#include <libavdevice/avdevice.h>
#include <libavutil/log.h>
}
using namespace lab;
namespace {
std::atomic<bool> stopping{false};
static_assert(std::atomic<bool>::is_always_lock_free);
void stop(int) {stopping.store(true,std::memory_order_relaxed);}
void validate(const Json &config) {
  const std::set<std::string> fields{"source","width","height","fps","encoder","bitrate", "rate_control","max_bitrate","deinterlace","audio","latency_ms"};
  require(config.is_object()&&config.size()==fields.size(),"Invalid probe configuration");
  for(const auto &[key,value]:config.items())require(fields.contains(key),"Invalid probe configuration");
  const auto &source=config.at("source");
  require(source.is_object()&&(source.size()==2||source.size()==3)&&source.at("kind")=="browser"&&
          source.at("display").is_string(),"Browser-only probe");
  for(const auto &[key,value]:source.items())require(key=="kind"||key=="display"||key=="pulse","Browser-only probe");
  require(std::regex_match(source.at("display").get<std::string>(),std::regex(":([0-9]{1,6})(\\.[0-9]{1,2})?")),"Local display required");
  if(source.contains("pulse"))require(source.at("pulse")=="airplayvideo.monitor","Local browser audio required");
  for(const char *key:{"width","height","fps","bitrate","max_bitrate","latency_ms"})
    require(config.at(key).is_number_integer(),"Invalid probe number");
  require((config.at("width")==1920&&config.at("height")==1080)||
          (config.at("width")==1280&&config.at("height")==720),"Invalid probe canvas");
  require(config.at("fps")==30||config.at("fps")==60,"Invalid probe rate");
  require(config.at("encoder")=="libopenh264"||config.at("encoder")=="h264_vaapi","Invalid probe encoder");
  require(config.at("audio").is_boolean()&&config.at("deinterlace").is_boolean(),"Invalid probe setting");
  require(config.at("max_bitrate").get<int64_t>()>=2000000&&config.at("max_bitrate").get<int64_t>()<=40000000,"Invalid probe bitrate");
  require(config.at("latency_ms").get<int64_t>()>=500&&config.at("latency_ms").get<int64_t>()<=2000,"Invalid probe lead");
  VideoRate::parse(config);
  if(config.at("audio").get<bool>()) {
    const char *pulse=getenv("PULSE_SERVER");
    require(pulse&&std::string_view(pulse).starts_with("unix:/"),"Local browser audio required");
  }
}
Json report(const FrameProbeSummary &value,int fps) {
  Json regions=Json::array();
  for(const auto &region:value.regions) {
    Json thresholds=Json::array();
    for(const auto &threshold:region.thresholds)
      thresholds.push_back({{"luma_delta",threshold.luma_delta},{"changed_fraction",threshold.changed_fraction},
        {"updates",threshold.updates},{"updates_per_second",threshold.updates_per_second},{"longest_hold_ms",threshold.longest_hold_ms}});
    regions.push_back({{"name",region.name},{"exact_updates",region.exact_updates},
      {"exact_updates_per_second",region.exact_updates_per_second},{"longest_exact_hold_ms",region.longest_exact_hold_ms},
      {"thresholds",std::move(thresholds)}});
  }
  Json timings=Json::object();
  const std::array<const char*,4> names{"conversion","upload","encode","observation"};
  for(size_t i=0;i<names.size();++i) {
    const auto &timing=value.timings[i];
    timings[names[i]]={{"count",timing.count},{"total",timing.total},{"mean",timing.mean},{"p95",timing.p95},{"max",timing.max}};
  }
  return {{"version",1},{"warmup_seconds",2},{"duration_seconds",15},{"requested_fps",fps},
    {"capture",{{"frames",value.capture_frames},{"fps",double(value.capture_frames)/15},
      {"nonmonotonic_pts",value.nonmonotonic_pts},{"pts",{{"median_ms",value.capture_pts.median_ms},
      {"p95_ms",value.capture_pts.p95_ms},{"max_ms",value.capture_pts.max_ms},{"gaps_over_50ms",value.capture_pts.gaps_over_50ms}}}}},
    {"gate",{{"accepted",value.accepted},{"rejected",value.rejected}}},
    {"converted",{{"frames",value.converted_frames},{"fps",double(value.converted_frames)/15}}},
    {"encoded",{{"frames",value.encoded_frames},{"fps",double(value.encoded_frames)/15}}},
    {"raw_picture",{{"frames",value.picture_frames},{"regions",std::move(regions)}}},{"timings_ms",std::move(timings)}};
}
}
int main(int argc,char**) {
  if(argc!=1)return 2;
  const rlimit memory{2ULL*1024*1024*1024,2ULL*1024*1024*1024},cpu{30,31},core{0,0};
  if(setrlimit(RLIMIT_AS,&memory)!=0||setrlimit(RLIMIT_CPU,&cpu)!=0||setrlimit(RLIMIT_CORE,&core)!=0)return 3;
  signal(SIGINT,stop);signal(SIGTERM,stop);alarm(40);
  avdevice_register_all();av_log_set_level(AV_LOG_QUIET);
  try {
    std::string input;char buffer[4096];
    while(std::cin) {std::cin.read(buffer,sizeof(buffer));input.append(buffer,size_t(std::cin.gcount()));require(input.size()<=16384,"Probe input too large");}
    auto config=Json::parse(input);validate(config);
    auto probe=std::make_shared<FrameProbe>();
    bool complete=false;
    {
      const auto deadline=Clock::now()+std::chrono::seconds(36);
      Media media(config,stopping,[&](const std::string &name,const Json &){
        if(name=="media_ready")probe->arm(frame_probe_now());
      },probe);
      media.start();
      while(!stopping&&!media.failed()&&Clock::now()<deadline&&!probe->finished(frame_probe_now()))
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
      complete=!stopping&&!media.failed()&&probe->finished(frame_probe_now());
      stopping=true;media.close();
    }
    require(complete,"Probe did not complete");
    auto summary=probe->summary();require(summary.capture_frames>=2&&summary.converted_frames>=2&&summary.encoded_frames>=2,"Probe produced too few frames");
    const auto output=report(summary,config.at("fps")).dump();require(output.size()<=32768,"Probe output too large");
    std::cout<<output<<'\n';return 0;
  } catch(...) {return 4;}
}
