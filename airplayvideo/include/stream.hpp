#pragma once
#include "lab.hpp"
#include <condition_variable>
#include <deque>

namespace lab {
inline uint64_t ntp_delta(uint64_t us) {
  return (us / 1000000 << 32) + ((us % 1000000) << 32) / 1000000;
}
struct MediaPacket {
  bool audio = false;
  int64_t pts_us = 0;
  int64_t available_us = 0;
  EncodedFrame video;
  Bytes pcm;
};
struct Subscriber {
  std::mutex mutex;
  std::condition_variable ready;
  std::deque<std::shared_ptr<const MediaPacket>> packets;
  bool closed = false;
  bool waiting_keyframe = true;
  std::string error;
  size_t bytes = 0;
  std::shared_ptr<const MediaPacket> next(const std::atomic<bool> &stop);
  void close();
};
class Media {
  struct Impl;
public:
  const Json config;
  const uint64_t epoch_ntp;
  const Clock::time_point epoch;
private:
  std::unique_ptr<Impl> impl_;
public:
  Media(Json config, std::atomic<bool> &stop, Note note);
  ~Media();
  std::shared_ptr<Subscriber> subscribe();
  void start();
  void close();
  bool failed() const;
  bool completed() const;
};
void mirror_stream(const Credentials &, Media &, uint16_t timing_port,
                   uint16_t control_port, std::atomic<bool> &stop,
                   const Note &note);
Bytes alac_frame(std::span<const uint8_t> pcm);
Json audio_format_setup();
Bytes audio_packet(std::span<const uint8_t> pcm, std::span<const uint8_t> key,
                   uint64_t nonce, uint16_t sequence, uint32_t timestamp,
                   uint32_t ssrc, bool first);
Json audio_timing_setup(int lead_ms);
Json video_timing_setup(int lead_ms);
Bytes audio_sync_packet(uint64_t presentation_epoch, int64_t pts_us,
                        uint32_t timestamp, int lead_ms, bool first);
Json media_capabilities();
int run_stream(const Json &config, const std::filesystem::path &receivers,
               std::atomic<bool> &stop, const Note &note);
}
