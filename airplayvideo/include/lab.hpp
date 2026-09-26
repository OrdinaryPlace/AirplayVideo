#pragma once
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <nlohmann/json.hpp>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <sys/types.h>
#include <thread>
#include <vector>
extern "C" {
#include "pair.h"
}
namespace lab {
using Bytes = std::vector<uint8_t>;
using Json = nlohmann::json;
using Clock = std::chrono::steady_clock;
using Note = std::function<void(const std::string &, const Json &)>;
void require(bool condition, const std::string &message);
Bytes bytes(std::string_view value);
std::string hex(std::span<const uint8_t> value);
Bytes unhex(std::string_view value);
Bytes random_bytes(size_t size);
std::string uuid();
uint64_t random_id();
void le(Bytes &b, size_t offset, uint64_t value, size_t width);
void be(Bytes &b, size_t offset, uint64_t value, size_t width);
uint64_t read_le(std::span<const uint8_t> b);
// Screen timestamps use monotonic seconds.32; audio/control NTP adds the
// 1900-to-1970 offset even though the underlying clock is still monotonic.
uint64_t ntp_now();
uint64_t screen_to_ntp(uint64_t screen_time);
Bytes timing_response(std::span<const uint8_t> request, uint64_t received,
                      uint64_t transmitted);
Bytes hkdf(std::span<const uint8_t> secret, std::string_view salt,
           std::string_view info, size_t size = 32);
Bytes seal(std::span<const uint8_t> key, uint64_t nonce,
           std::span<const uint8_t> aad, std::span<const uint8_t> plain);
Bytes open_sealed(std::span<const uint8_t> key, uint64_t nonce,
                  std::span<const uint8_t> aad,
                  std::span<const uint8_t> cipher);
Bytes plist_encode(const Json &value);
Json plist_decode(std::span<const uint8_t> value);
void private_write_new(const std::filesystem::path &path,
                       std::string_view data);
std::string private_read(const std::filesystem::path &path);

class Socket {
  int fd_ = -1;

public:
  Socket() = default;
  explicit Socket(int fd) : fd_(fd) {}
  ~Socket();
  Socket(Socket &&other) noexcept;
  Socket &operator=(Socket &&other) noexcept;
  Socket(const Socket &) = delete;
  int fd() const { return fd_; }
  static Socket connect(const std::string &ip, uint16_t port,
                        int timeout_ms = 5000);
  Bytes read_some(int timeout_ms = 5000);
  Bytes read_exact(size_t size, int timeout_ms = 5000);
  void write(std::span<const uint8_t> data, int timeout_ms = 5000);
  void shutdown();
  std::string local_address() const;
};
struct Message {
  std::string first_line;
  int status = 0;
  std::map<std::string, std::string> headers;
  Bytes body;
};
Bytes event_response(const Message &event);
class Channel {
  Socket socket_;
  std::unique_ptr<pair_cipher_context, decltype(&pair_cipher_free)> cipher_{
      nullptr, pair_cipher_free};
  Bytes pending_;
  Bytes read_block(int timeout_ms);

public:
  explicit Channel(Socket socket) : socket_(std::move(socket)) {}
  void encrypt(std::span<const uint8_t> secret, pair_channel kind,
               const char *suffix = nullptr);
  void write(std::span<const uint8_t> data, int timeout_ms = 5000);
  Message read_message(int timeout_ms = 5000);
  std::optional<Message> read_event(int idle_timeout_ms = 250,
                                  int message_timeout_ms = 5000);
  void shutdown() { socket_.shutdown(); }
  std::string local_address() const { return socket_.local_address(); }
};
struct RtspOptions {
  int timeout_ms = 5000;
  bool session_headers = true;
  std::string user_agent = "AirPlay/409.16";
  bool client_instance = false;
};
class Rtsp {
  Channel channel_;
  std::string id_, dacp_, session_;
  uint64_t active_;
  uint32_t seq_ = 0;

public:
  Rtsp(const std::string &ip, uint16_t port, const std::string &id,
       int connect_timeout_ms = 5000);
  // Socket injection supports local protocol tests without a receiver.
  Rtsp(Socket socket, const std::string &id);
  Message request(const std::string &method, const std::string &path,
                  const Bytes &body = {}, const std::string &type = "",
                  const std::map<std::string, std::string> &headers = {},
                  const RtspOptions &options = {});
  Json plist(const std::string &method, const std::string &path,
             const Json &body);
  void encrypt(std::span<const uint8_t> secret) {
    channel_.encrypt(secret, PAIR_CHANNEL_CONTROL);
  }
  void shutdown() { channel_.shutdown(); }
  std::string local_address() const { return channel_.local_address(); }
};
struct Receiver {
  std::string name, address, device_id, model, version;
  uint16_t port = 7000;
  Json json() const;
};
std::vector<Receiver> discover(int milliseconds = 3000);
Json probe(Rtsp &rtsp, const std::string &expected_name);
struct Credentials {
  std::string sender_id, keys, receiver_id;
  Receiver receiver;
  Json json() const;
  static Credentials from_json(const Json &json);
};
Credentials pair_with_pin(Rtsp &rtsp, const Receiver &receiver,
                          const std::string &sender_id, const std::string &pin,
                          const Note &note);
Bytes verify_pair(Rtsp &rtsp, const Credentials &credentials, const Note &note);

struct EncodedFrame {
  Bytes avcc, payload;
  bool keyframe = false;
};
class Video {
  struct Impl;
  std::unique_ptr<Impl> impl_;

public:
  Video();
  ~Video();
  EncodedFrame frame(int index);
};
Bytes mirror_header(uint32_t payload_size, uint8_t type,
                    uint64_t presentation_time, int width = 1920, int height = 1080);
Bytes mirror_packet(const EncodedFrame &frame, std::span<const uint8_t> key,
                    uint64_t nonce, uint64_t presentation_time, int width = 1920, int height = 1080);
void mirror(const Credentials &credentials, uint16_t timing_port, int seconds,
            std::atomic<bool> &stop, const Note &note);
void write_sample(const std::filesystem::path &path, int seconds);
} // namespace lab
