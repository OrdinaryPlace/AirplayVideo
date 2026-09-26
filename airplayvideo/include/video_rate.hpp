#pragma once
#include "lab.hpp"
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/opt.h>
}

namespace lab {
struct VideoRate {
  std::string mode;
  int64_t target, maximum;

  static VideoRate parse(const Json &config) {
    require(config.at("bitrate").is_number_integer(), "Invalid video bitrate");
    const auto target = config.at("bitrate").get<int64_t>();
    require(target >= 2000000 && target <= 20000000, "Invalid video bitrate");
    const auto mode = config.value("rate_control", std::string("auto"));
    require(mode == "auto" || mode == "vbr", "Unsupported video rate control");
    int64_t maximum = 0;
    if (mode == "vbr") {
      require(config.value("encoder", std::string("libopenh264")) == "h264_vaapi",
              "Variable bitrate requires the VAAPI hardware encoder");
      require(config.contains("max_bitrate") && config.at("max_bitrate").is_number_integer(),
              "Variable bitrate needs a maximum bitrate");
      maximum = config.at("max_bitrate").get<int64_t>();
      require(maximum >= target && maximum <= 40000000,
              "Maximum bitrate must be at least the target and at most 40 Mbps");
    }
    return {mode, target, maximum};
  }

  void apply(AVCodecContext *codec) const {
    codec->bit_rate = target;
    if (mode == "vbr") {
      // Explicit selection fails if the driver cannot honor VBR; never silently
      // fall back to a different rate-control mode.
      require(av_opt_set(codec->priv_data, "rc_mode", "VBR", 0) >= 0,
              "The encoder does not support variable bitrate");
      codec->rc_max_rate = maximum;
      // A half-second coded-bit reservoir bounds bursts. This is an encoder
      // budget, not a queue or a change to the shared A/V presentation lead.
      codec->rc_buffer_size = int(maximum / 2);
    }
  }
};
}
