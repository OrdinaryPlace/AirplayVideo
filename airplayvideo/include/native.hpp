#pragma once
#include "lab.hpp"

namespace lab {
// Experimental receiver-owned URL playback. The caller owns the scoped HTTP
// origin and authorizes this exact receiver. Credentials stay in memory. This
// does not discover, pair, fetch YouTube, transcode, or confirm picture/audio.
// Maximum trial duration is 1..600 seconds; connect and cleanup add bounded
// overhead. The owning process must retain an outer termination/reaping bound.
// PTP metadata is negotiated; this is not a general IEEE-1588 clock sender.
void native_play(const Credentials &, const std::string &media_url,
                 std::atomic<bool> &stop, const Note &, int max_seconds = 120);

struct NativeIdentity {
  std::string local_ip, receiver_id, session_id, correlation_id, sender_mac,
      item_id, client_id;
  uint64_t clock_id = 0, stream_id = 0, encryption_seed = 0;
  uint16_t clock_port = 0, rtcp_port = 0;
};
struct NativeRequest {
  std::string stage, method, path, content_type;
  Bytes body;
  std::map<std::string, std::string> headers;
  RtspOptions options;
};
// No networking or randomness in these builders. Inputs are validated before
// any connection; generated runtime identity values are never credentials.
void validate_native_url(const std::string &);
struct NativeReceiverIds {
  std::string queue_id, peer_id;
};
// Call only on /info obtained after pair verification. Accept UUID identifiers
// from the top-level plist or its DNS TXT binary txtAirPlay field (<=4096 bytes,
// <=64 records). Reject malformed TXT, duplicate identity keys and conflicting
// same-key UUIDs; prefer valid top-level values. No credential/deviceID fallback.
NativeReceiverIds native_receiver_ids(const Json &authenticated_info);
Json native_session_setup(const NativeIdentity &);
Json native_peer_setup(const NativeIdentity &, const std::string &peer_id);
Json native_media_setup(const NativeIdentity &);
Json native_control_setup(const NativeIdentity &, const std::string &receiver_id);
Json native_insert(const NativeIdentity &, const std::string &media_url);
Bytes native_command(const Json &);

// Deterministic state-machine seam. A test transport supplies a virtual clock,
// fake replies and event failures. One owner serializes all control requests.
// Production close() must release event resources and shut down control IO.
class NativeTransport {
public:
  virtual ~NativeTransport() = default;
  virtual void verify() = 0;
  virtual const NativeIdentity &identity() const = 0;
  virtual Message exchange(const NativeRequest &) = 0;
  virtual void open_events(uint16_t port) = 0;
  virtual bool healthy() const = 0;
  virtual Clock::time_point now() const = 0;
  virtual void wait(std::chrono::milliseconds) = 0;
  virtual void close() noexcept = 0;
};
void native_session(NativeTransport &, const std::string &media_url,
                    std::atomic<bool> &stop, const Note &, int max_seconds);
} // namespace lab
