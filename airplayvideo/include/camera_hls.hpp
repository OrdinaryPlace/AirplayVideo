#pragma once
#include "stream.hpp"

namespace lab {
// A bounded subscriber: camera clients must never stall live AirPlay receivers.
class CameraHls {
  std::atomic<bool> stop_{false}, done_{false};
  std::shared_ptr<Subscriber> subscription_;
  std::jthread worker_;
public:
  CameraHls(Media &, const std::filesystem::path &, Note);
  ~CameraHls();
  bool done() const { return done_; }
};
}
