#include "native.hpp"
#include <arpa/inet.h>
#include <regex>
#include <set>
#include <sodium.h>
#include <sys/socket.h>
#include <unistd.h>

namespace lab {
namespace {
constexpr auto plist_type = "application/x-apple-binary-plist";
constexpr auto rcs_type = "A6B27562-B43A-4F2D-B75F-82391E250194";
bool private_ip(const std::string &text) {
  in_addr ip{};
  if (inet_pton(AF_INET, text.c_str(), &ip) != 1) return false;
  auto value = ntohl(ip.s_addr);
  return (value >> 24) == 10 || (value >> 20) == 0xac1 || (value >> 16) == 0xc0a8;
}
bool identifier(const std::string &value) {
  static const std::regex pattern("[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}");
  return value.size() == 36 && std::regex_match(value, pattern);
}
std::string ascii_lower(std::string value) {
  for (auto &ch : value) if (ch >= 'A' && ch <= 'Z') ch += 'a' - 'A';
  return value;
}
void validate_identity(const NativeIdentity &id) {
  require(private_ip(id.local_ip) && identifier(id.session_id) &&
              identifier(id.correlation_id) && identifier(id.item_id) && identifier(id.client_id),
          "Invalid native session identity");
  static const std::regex mac("([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}");
  require(std::regex_match(id.sender_mac, mac) && id.clock_id && id.stream_id &&
              id.clock_port && id.rtcp_port, "Incomplete native session parameters");
}
void emit(const Note &note, const std::string &stage, int status, int64_t elapsed) noexcept {
  if (note) {
    try { note("native_stage", {{"stage", stage}, {"status", status}, {"elapsed_ms", elapsed}}); }
    catch (...) { /* Diagnostics cannot own transport lifetime. */ }
  }
}
NativeRequest request(const std::string &stage, const std::string &method,
                      const std::string &path, const Json *body = nullptr) {
  NativeRequest out{stage, method, path, {}, {}, {}, {}};
  out.options.timeout_ms = 2500;
  out.options.user_agent = "AirPlay/960.10.1";
  out.options.session_headers = false;
  out.options.client_instance = true;
  if (body) { out.body = plist_encode(*body); out.content_type = plist_type; }
  return out;
}
NativeRequest command(const std::string &stage, const Json &body) {
  auto out = request(stage, "POST", "/command");
  out.content_type = plist_type;
  out.body = native_command(body);
  out.headers["X-Apple-StreamID"] = "1";
  // Experimental Apple receiver RCS profile; keep old RTSP defaults unchanged.
  out.options.session_headers = false;
  return out;
}
class Stopped final {};

class EventPump {
  Channel channel_;
  std::atomic<bool> healthy_{true};
  std::jthread thread_;
public:
  EventPump(const std::string &address, uint16_t port, std::span<const uint8_t> secret)
      : channel_(Socket::connect(address, port, 2000)) {
    channel_.encrypt(secret, PAIR_CHANNEL_EVENTS);
    thread_ = std::jthread([this](std::stop_token stop) {
      while (!stop.stop_requested()) {
        try {
          auto event = channel_.read_event(100, 1000);
          if (!event || event->status) continue;
          // Receiver-assisted web loading is a separate protocol. Do not
          // acknowledge it as completed when this transport cannot serve it.
          const auto unsupported = bytes("unhandledURLRequest");
          require(std::search(event->body.begin(), event->body.end(),
                              unsupported.begin(), unsupported.end()) == event->body.end(),
                  "Receiver requires unsupported URL assistance");
          channel_.write(event_response(*event));
        } catch (...) {
          if (!stop.stop_requested()) healthy_ = false;
          break;
        }
      }
    });
  }
  ~EventPump() { thread_.request_stop(); channel_.shutdown(); if (thread_.joinable()) thread_.join(); }
  bool healthy() const { return healthy_; }
};
class BoundPort {
  Socket socket_;
public:
  uint16_t port = 0;
  explicit BoundPort(const std::string &local) : socket_(::socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0)) {
    require(socket_.fd() >= 0, "Native UDP socket unavailable");
    sockaddr_in address{}; address.sin_family = AF_INET;
    require(inet_pton(AF_INET, local.c_str(), &address.sin_addr) == 1 &&
                bind(socket_.fd(), reinterpret_cast<sockaddr *>(&address), sizeof(address)) == 0,
            "Native UDP binding failed");
    socklen_t size = sizeof(address);
    require(getsockname(socket_.fd(), reinterpret_cast<sockaddr *>(&address), &size) == 0,
            "Native UDP port unavailable");
    port = ntohs(address.sin_port);
  }
};
class WireTransport final : public NativeTransport {
  const Credentials &credentials_;
  Rtsp rtsp_;
  NativeIdentity identity_;
  std::unique_ptr<BoundPort> clock_, rtcp_;
  Bytes secret_;
  std::unique_ptr<EventPump> events_;
  std::atomic<bool> closed_{false};
  std::jthread watchdog_;
public:
  WireTransport(const Credentials &credentials, std::atomic<bool> &stop, int seconds)
      : credentials_(credentials), rtsp_(credentials.receiver.address, credentials.receiver.port,
                                         credentials.sender_id, 2000) {
    identity_.local_ip = rtsp_.local_address();
    identity_.receiver_id = credentials.receiver_id;
    identity_.session_id = uuid(); identity_.correlation_id = uuid();
    identity_.item_id = uuid(); identity_.client_id = uuid();
    auto mac = random_bytes(6); mac[0] = (mac[0] | 2) & 0xfe;
    for (size_t i = 0; i < mac.size(); ++i) {
      if (i) identity_.sender_mac += ':';
      identity_.sender_mac += hex(std::span(mac).subspan(i, 1));
    }
    identity_.clock_id = random_id() | 1; identity_.stream_id = random_id() | 1;
    identity_.encryption_seed = random_id();
    clock_ = std::make_unique<BoundPort>(identity_.local_ip);
    rtcp_ = std::make_unique<BoundPort>(identity_.local_ip);
    identity_.clock_port = clock_->port; identity_.rtcp_port = rtcp_->port;
    auto deadline = Clock::now() + std::chrono::seconds(seconds + 6);
    watchdog_ = std::jthread([this, &stop, deadline](std::stop_token end) {
      std::optional<Clock::time_point> stopping;
      while (!end.stop_requested()) {
        if (stop && !stopping) stopping = Clock::now();
        if (Clock::now() >= deadline || (stopping && Clock::now() - *stopping >= std::chrono::milliseconds(500))) {
          rtsp_.shutdown(); break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
      }
    });
  }
  ~WireTransport() override { close(); }
  void verify() override {
    secret_ = verify_pair(rtsp_, credentials_, [](const std::string &, const Json &) {});
  }
  const NativeIdentity &identity() const override { return identity_; }
  Message exchange(const NativeRequest &value) override {
    return rtsp_.request(value.method, value.path, value.body, value.content_type,
                         value.headers, value.options);
  }
  void open_events(uint16_t port) override {
    events_ = std::make_unique<EventPump>(credentials_.receiver.address, port, secret_);
    sodium_memzero(secret_.data(), secret_.size()); secret_.clear();
  }
  bool healthy() const override { return !events_ || events_->healthy(); }
  Clock::time_point now() const override { return Clock::now(); }
  void wait(std::chrono::milliseconds value) override { std::this_thread::sleep_for(value); }
  void close() noexcept override {
    if (closed_.exchange(true)) return;
    watchdog_.request_stop(); if (watchdog_.joinable()) watchdog_.join();
    rtsp_.shutdown(); events_.reset(); clock_.reset(); rtcp_.reset();
    sodium_memzero(secret_.data(), secret_.size()); secret_.clear();
  }
};
} // namespace

NativeReceiverIds native_receiver_ids(const Json &info) {
  require(info.is_object(), "Receiver information is invalid");
  std::map<std::string, std::string> top, txt;
  for (auto key : {"psi", "pi", "gid"}) {
    auto value = info.find(key);
    if (value != info.end() && value->is_string()) {
      const auto &text = value->get_ref<const std::string &>();
      if (identifier(text)) top[key] = text;
    }
  }
  auto encoded = info.find("txtAirPlay");
  if (encoded != info.end()) {
    require(encoded->is_binary(), "Receiver TXT information is invalid");
    const auto &data = encoded->get_binary();
    require(data.size() <= 4096, "Receiver TXT information exceeds limit");
    size_t cursor = 0, fields = 0;
    std::set<std::string> seen;
    while (cursor < data.size()) {
      require(++fields <= 64, "Receiver TXT field count exceeds limit");
      size_t length = data[cursor++];
      require(length <= data.size() - cursor, "Receiver TXT field is truncated");
      const auto begin = data.begin() + cursor;
      const auto end = begin + length;
      const auto equal = std::find(begin, end, uint8_t('='));
      auto key = ascii_lower(std::string(begin, equal));
      if (key == "psi" || key == "pi" || key == "gid") {
        require(seen.insert(key).second, "Receiver TXT identity is duplicated");
        if (equal != end) {
          std::string value(equal + 1, end);
          if (identifier(value)) txt[key] = value;
        }
      }
      cursor += length;
    }
  }
  for (auto key : {"psi", "pi", "gid"}) {
    require(!top.contains(key) || !txt.contains(key) ||
                ascii_lower(top.at(key)) == ascii_lower(txt.at(key)),
            "Receiver identity information conflicts");
  }
  NativeReceiverIds ids;
  // psi and pi are distinct advertised identities and may legitimately differ.
  // Preserve source priority before falling back to the authenticated TXT data.
  for (const auto *values : {&top, &txt}) {
    for (auto key : {"psi", "pi"}) {
      if (values->contains(key)) { ids.queue_id = values->at(key); break; }
    }
    if (!ids.queue_id.empty()) break;
  }
  require(!ids.queue_id.empty(), "Receiver queue identity is unavailable");
  ids.peer_id = top.contains("gid") ? top.at("gid") :
                txt.contains("gid") ? txt.at("gid") : ids.queue_id;
  return ids;
}

void validate_native_url(const std::string &url) {
  static const std::regex pattern("http://([0-9.]+):([0-9]{1,5})/([A-Za-z0-9_-]{16,128})/([A-Za-z0-9_.-]{1,128})");
  std::smatch match;
  require(url.size() <= 2048 && std::regex_match(url, match, pattern) &&
              private_ip(match[1].str()), "Use the assigned private native media URL");
  auto port = std::stoul(match[2].str());
  require(port > 0 && port <= 65535 && match[4].str().find("..") == std::string::npos &&
              (url.ends_with(".mp4") || url.ends_with(".m3u8")),
          "Use the assigned private native media URL");
}
Json native_session_setup(const NativeIdentity &id) {
  validate_identity(id);
  Json peer = {{"ID", id.correlation_id}, {"Addresses", Json::array({id.local_ip})},
               {"DeviceType", 0}, {"SupportsClockPortMatchingOverride", true}};
  // Wire compatibility profile for this experimental protocol, not host identity.
  return {{"name", "AirplayVideo"}, {"deviceID", id.sender_mac}, {"macAddress", id.sender_mac},
          {"sessionUUID", id.session_id}, {"sessionCorrelationUUID", id.correlation_id},
          {"timingProtocol", "PTP"}, {"timingPeerInfo", peer}, {"timingPeerList", Json::array({peer})},
          {"isScreenMirroringSession", false}, {"isMultiSelectAirPlay", false},
          {"sourceVersion", "960.10.1"}, {"model", "iPhone18,1"}, {"osName", "iPhone OS"},
          {"osVersion", "26.0"}, {"osBuildVersion", "23A341"},
          {"statsCollectionEnabled", false}, {"diagnosticsAndUsage", false}};
}
Json native_peer_setup(const NativeIdentity &id, const std::string &peer_id) {
  validate_identity(id); require(identifier(peer_id), "Receiver clock identity is unavailable");
  return Json::array({{{"ID", id.correlation_id}, {"Addresses", Json::array({id.local_ip})},
                      {"ClockID", id.clock_id}, {"DeviceType", 0},
                      {"ClockPorts", {{peer_id, id.clock_port}}},
                      {"SupportsClockPortMatchingOverride", true}}});
}
Json native_media_setup(const NativeIdentity &id) {
  validate_identity(id);
  return {{"streams", Json::array({{
      {"type", 96}, {"ct", 2}, {"sr", 44100}, {"spf", 352}, {"audioFormat", 262144},
      {"audioMode", "default"}, {"isMedia", true}, {"latencyMin", 11025}, {"latencyMax", 88200},
      {"supportsDynamicStreamID", true}, {"streamConnectionID", id.stream_id},
      {"streamConnections", {
          {"streamConnectionTypeRTP", {{"streamConnectionKeyUseStreamEncryptionKey", true}}},
          {"streamConnectionTypeMediaDataControl", {{"streamConnectionKeyEncryptionSeed", id.encryption_seed}}},
          {"streamConnectionTypeRTCP", {{"streamConnectionKeyPort", id.rtcp_port}}}}}
  }})}};
}
Json native_control_setup(const NativeIdentity &id, const std::string &receiver_id) {
  validate_identity(id); require(identifier(receiver_id), "Receiver queue identity is unavailable");
  return {{"streams", Json::array({{{"type", 130}, {"controlType", 1},
          {"clientUUID", id.client_id}, {"clientTypeUUID", rcs_type},
          {"channelID", receiver_id + "-RCS-1"}}})}};
}
Json native_insert(const NativeIdentity &id, const std::string &url) {
  validate_identity(id); validate_native_url(url);
  Json item = {{"uuid", id.item_id}, {"Content-Location", url}, {"mediaType", "file"},
      {"streamType", 1}, {"rate", 1.0}, {"volume", 1.0}, {"isAudioOnly", false}, {"audioOnly", false},
      {"SenderMACAddress", id.sender_mac}, {"clientBundleID", "local.airplayvideo"},
      {"clientProcName", "AirplayVideo"}, {"playbackRestrictions", 0}, {"referenceRestrictions", 3},
      {"Start-Position", {{"flags", 1}, {"value", 0}, {"epoch", 0}, {"timescale", 1000}}}};
  if (url.ends_with(".m3u8")) item["HLS-Content-Location"] = url;
  return {{"type", "insertPlayQueueItem"}, {"item", item}};
}
Bytes native_command(const Json &body) {
  require(body.is_object() && body.contains("type") && body["type"].is_string(),
          "Invalid native queue command");
  return plist_encode({{"params", {{"data", Json::binary(plist_encode(body))}}}});
}

void native_session(NativeTransport &transport, const std::string &url,
                    std::atomic<bool> &stop, const Note &note, int seconds) {
  const auto started = transport.now();
  auto elapsed = [&] { return std::chrono::duration_cast<std::chrono::milliseconds>(transport.now() - started).count(); };
  bool setup = false, inserted = false, cleaned = false, events_open = false;
  int cleanup_status = 200;
  std::string stage = "setup";
  int status = 0;
  auto cleanup = [&] {
    if (cleaned) return cleanup_status;
    cleaned = true;
    if (inserted) {
      try { auto value = command("rate", {{"type", "setRate"}, {"rate", 0.0}});
        value.options.timeout_ms = 500; transport.exchange(value); } catch (...) {}
    }
    if (setup) {
      int result = 0;
      try { auto value = request("teardown", "TEARDOWN", "rtsp://" + transport.identity().local_ip + "/" + std::to_string(transport.identity().stream_id));
        value.headers["Session"] = std::to_string(transport.identity().stream_id);
        value.options.timeout_ms = 500; result = transport.exchange(value).status; } catch (...) {}
      emit(note, "teardown", result, elapsed());
      cleanup_status = result;
    }
    transport.close();
    return cleanup_status;
  };
  auto check = [&] {
    if (stop) throw Stopped{};
    require(elapsed() < int64_t(seconds) * 1000, "Native trial deadline reached");
  };
  auto send = [&](NativeRequest value) {
    check(); stage = value.stage; status = 0;
    if (events_open && !transport.healthy()) {
      stage = "events";
      throw std::runtime_error("Native event channel failed");
    }
    auto response = transport.exchange(value); status = response.status;
    if (stage == "setup" && status == 200) setup = true;
    emit(note, stage, status, elapsed());
    require(status == 200, "Native receiver request rejected");
    return response;
  };
  try {
    validate_native_url(url); require(seconds >= 1 && seconds <= 600, "Native duration must be 1..600 seconds");
    const auto &id = transport.identity(); validate_identity(id);
    check(); stage = "verify"; transport.verify(); emit(note, stage, 200, elapsed());
    auto info_message = send(request("probe", "GET", "/info"));
    const auto receiver = native_receiver_ids(plist_decode(info_message.body));
    const std::string uri = "rtsp://" + id.local_ip + "/" + std::to_string(id.stream_id);
    auto root = native_session_setup(id);
    auto response = send(request("setup", "SETUP", uri, &root));
    auto data = plist_decode(response.body);
    require(data.is_object() && data.contains("eventPort") && data["eventPort"].is_number_integer(),
            "Receiver event channel is unavailable");
    auto port = data["eventPort"].get<int64_t>();
    require(port > 0 && port <= 65535, "Receiver event channel is invalid");
    check(); stage = "events"; status = 0; transport.open_events(uint16_t(port)); events_open = true; emit(note, stage, 200, elapsed());
    send(request("record", "RECORD", uri));
    auto peers = native_peer_setup(id, receiver.peer_id);
    auto peer_request = request("peers", "SETPEERSX", uri, &peers);
    peer_request.content_type = "/peer-list-changed-x"; send(peer_request);
    auto media = native_media_setup(id); send(request("media", "SETUP", uri, &media));
    auto control = native_control_setup(id, receiver.queue_id);
    auto control_response = send(request("control", "SETUP", uri, &control));
    auto control_data = plist_decode(control_response.body);
    require(control_data.is_object() && control_data.contains("streams") && control_data["streams"].is_array(),
            "Receiver control stream is unavailable");
    bool queue_ready = false;
    for (const auto &stream : control_data["streams"]) {
      if (stream.is_object() && stream.value("type", 0) == 130 && stream.value("streamID", uint64_t(0)) == 1)
        queue_ready = true;
    }
    require(queue_ready, "Receiver did not establish the requested queue stream");
    send(command("insert", native_insert(id, url))); inserted = true;
    send(command("property", {{"type", "setProperty"}, {"property", "actionAtItemEnd"}, {"value", 1}}));
    send(command("rate", {{"type", "setRate"}, {"rate", 1.0}}));
    auto next_feedback = transport.now();
    while (!stop && elapsed() < int64_t(seconds) * 1000) {
      stage = "events"; status = 0;
      require(transport.healthy(), "Native event channel failed");
      if (transport.now() >= next_feedback) {
        send(request("feedback", "POST", "/feedback"));
        next_feedback = transport.now() + std::chrono::seconds(2);
      }
      transport.wait(std::chrono::milliseconds(50));
    }
    auto closed = cleanup();
    stage = "teardown"; status = closed;
    require(closed == 200 || stop, "Native teardown failed");
    emit(note, "finished", stop ? 0 : 200, elapsed());
  } catch (const Stopped &) {
    cleanup(); emit(note, "finished", 0, elapsed());
  } catch (...) {
    if (stop) { cleanup(); emit(note, "finished", 0, elapsed()); return; }
    emit(note, stage, status, elapsed()); cleanup();
    throw std::runtime_error("Native playback failed at " + stage + " (status " + std::to_string(status) + ")");
  }
}

void native_play(const Credentials &credentials, const std::string &url,
                 std::atomic<bool> &stop, const Note &note, int seconds) {
  validate_native_url(url);
  require(seconds >= 1 && seconds <= 600, "Native duration must be 1..600 seconds");
  require(private_ip(credentials.receiver.address) && credentials.receiver.port,
          "Choose a private native receiver");
  require(identifier(credentials.sender_id) && credentials.keys.size() == 192 &&
              std::all_of(credentials.keys.begin(), credentials.keys.end(), [](unsigned char c) { return std::isxdigit(c); }),
          "Invalid native pairing; existing credentials were preserved");
  if (stop) { emit(note, "finished", 0, 0); return; }
  try {
    WireTransport transport(credentials, stop, seconds);
    emit(note, "connect", 200, 0);
    native_session(transport, url, stop, note, seconds);
  } catch (const std::exception &error) {
    // native_session failures already contain only fixed stage/status values.
    const std::string message = error.what();
    if (message.starts_with("Native playback failed at ")) throw;
    emit(note, "connect", 0, 0);
    throw std::runtime_error("Native playback connection failed");
  }
}
} // namespace lab
