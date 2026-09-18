#pragma once
#include "stream.hpp"

namespace lab {
// A separate bounded subscriber: recording must never hold up live receivers.
class Recording {
  std::atomic<bool> stop_{false}, done_{false};
  std::shared_ptr<Subscriber> subscription_;
  std::jthread worker_;
public:
  Recording(Media &, const std::filesystem::path &, const std::string &id,
            int seconds, Note);
  ~Recording();
  bool done() const { return done_; }
};
}
