#pragma once
#include "lab.hpp"
namespace lab {
// Native, plain-text rendering shared by preview and the encoded video source.
class GeneratedVideo {
  struct Impl;
  std::unique_ptr<Impl> impl_;
public:
  GeneratedVideo(const Json &settings, int width, int height);
  ~GeneratedVideo();
  const uint8_t *draw(double elapsed, int64_t unix_seconds);
  int stride() const;
  Bytes png(double elapsed, int64_t unix_seconds);
  std::string timer(double elapsed, int64_t unix_seconds) const;
  double duration() const;
};
Json generated_preview(const Json &settings);
}
