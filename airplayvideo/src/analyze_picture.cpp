#include "picture_cadence.hpp"
#include <iostream>
#include <nlohmann/json.hpp>
#include <sys/resource.h>
int main(int argc,char **argv) {
  if(argc!=2)return 2;
  const rlimit memory{512ULL*1024*1024,512ULL*1024*1024},cpu{30,31};
  if(setrlimit(RLIMIT_AS,&memory)!=0||setrlimit(RLIMIT_CPU,&cpu)!=0)return 3;
  (void)setpriority(PRIO_PROCESS,0,10);
  try {
    const auto summary=lab::picture::analyze_file(argv[1]);
    nlohmann::json regions=nlohmann::json::array();
    for(const auto &region:summary.regions) {
      nlohmann::json thresholds=nlohmann::json::array();
      for(const auto &value:region.thresholds)
        thresholds.push_back({{"luma_delta",value.luma_delta},{"changed_fraction",value.changed_fraction},
          {"updates",value.updates},{"updates_per_second",value.updates_per_second},{"longest_hold_ms",value.longest_hold_ms}});
      regions.push_back({{"name",region.name},{"thresholds",std::move(thresholds)}});
    }
    nlohmann::json result={{"version",1},{"video_frames",summary.video_frames},
      {"duration_seconds",summary.duration_seconds},{"encoded_fps",summary.encoded_fps},
      {"pts",{{"median_ms",summary.median_ms},{"p95_ms",summary.p95_ms},{"max_ms",summary.max_ms},
               {"gaps_over_50ms",summary.gaps_over_50ms}}},{"regions",std::move(regions)}};
    auto output=result.dump();if(output.size()>16384)return 4;
    std::cout<<output<<'\n';return 0;
  } catch(...) {return 5;}
}
