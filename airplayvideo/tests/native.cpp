#include "native.hpp"
#include <future>
#include <iostream>
#include <sodium.h>
#include <sys/socket.h>

using namespace lab;
namespace {
int assertions = 0;
void check(bool value, const char *message) { ++assertions; require(value, message); }
template <class F> void rejects(F test, const char *message) {
  bool failed = false; try { test(); } catch (...) { failed = true; }
  check(failed, message);
}
const std::string media = "http://192.168.250.10:8124/0123456789abcdef0123456789abcdef/clip.mp4";
NativeIdentity identity() {
  return {"192.168.250.10", "11111111-1111-4111-8111-111111111111",
          "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333",
          "02:03:04:05:06:07", "44444444-4444-4444-8444-444444444444",
          "55555555-5555-4555-8555-555555555555", 1234, 4321, 98, 32768, 32769};
}
Json queue_body(const NativeRequest &request) {
  auto envelope = plist_decode(request.body);
  const auto &data = envelope.at("params").at("data").get_binary();
  return plist_decode(data);
}
Bytes txt_fields(std::initializer_list<std::string> fields) {
  Bytes result;
  for (const auto &field : fields) {
    require(field.size() <= 255, "TXT test field exceeds wire limit");
    result.push_back(uint8_t(field.size()));
    result.insert(result.end(), field.begin(), field.end());
  }
  return result;
}
class Fake final : public NativeTransport {
public:
  NativeIdentity id = ::identity();
  Json probe_info;
  std::vector<NativeRequest> calls;
  std::string fail, cancel_after;
  bool throw_error = false, missing_event = false, invalid_identity = false, event_health = true, missing_control = false;
  int closed = 0, verified = 0, opened = 0;
  std::atomic<bool> *stop = nullptr;
  Clock::time_point time{};
  void verify() override {
    ++verified;
    if (fail == "verify") throw std::runtime_error("private authentication material");
    if (cancel_after == "verify") *stop = true;
  }
  const NativeIdentity &identity() const override { return id; }
  Message exchange(const NativeRequest &value) override {
    calls.push_back(value);
    if (value.stage == fail && throw_error) throw std::runtime_error("private signed URL");
    Message result; result.status = value.stage == fail ? 500 : 200;
    if (value.stage == "probe") result.body = plist_encode(probe_info.is_null() ? Json{{"pi", invalid_identity ? "" : id.receiver_id}} : probe_info);
    else if (value.stage == "setup") result.body = plist_encode(missing_event ? Json::object() : Json{{"eventPort", 7001}});
    else if (value.stage == "control") result.body = plist_encode(missing_control ? Json::object() : Json{{"streams", Json::array({{{"type", 130}, {"streamID", 1}}})}});
    if (value.stage == cancel_after) *stop = true;
    return result;
  }
  void open_events(uint16_t port) override {
    check(port == 7001, "Validated event port passed to transport");
    ++opened;
    if (fail == "events") throw std::runtime_error("private event address");
  }
  bool healthy() const override { return event_health; }
  Clock::time_point now() const override { return time; }
  void wait(std::chrono::milliseconds value) override { time += value; }
  void close() noexcept override { ++closed; }
  size_t count(const std::string &stage) const {
    return std::count_if(calls.begin(), calls.end(), [&](const auto &call) { return call.stage == stage; });
  }
};
void receiver_ids() {
  const std::string psi = "abcdef12-1234-4567-89ab-0123456789ab";
  const std::string pi = "22222222-2222-4222-8222-222222222222";
  const std::string gid = "33333333-3333-4333-8333-333333333333";
  auto parsed = native_receiver_ids({{"psi", psi}, {"pi", pi}, {"gid", gid}});
  check(parsed.queue_id == psi && parsed.peer_id == gid, "Top-level UUID identities are preferred");
  auto info = Json{{"txtAirPlay", Json::binary(txt_fields({"model=AppleTV", "pi=02:03:04:05:06:07", "psi=" + psi, "gid=" + gid}))}};
  parsed = native_receiver_ids(plist_decode(plist_encode(info)));
  check(parsed.queue_id == psi && parsed.peer_id == gid, "Authenticated binary TXT UUID fallback survives plist decoding");
  info["pi"] = pi;
  parsed = native_receiver_ids(info);
  check(parsed.queue_id == pi && parsed.peer_id == gid, "Valid top-level pi precedes TXT psi and invalid TXT pi");
  info["psi"] = "invalid"; info["pi"] = 42;
  parsed = native_receiver_ids(info);
  check(parsed.queue_id == psi, "Invalid top-level values are never used as identities");
  parsed = native_receiver_ids({{"txtAirPlay", Json::binary(txt_fields({"PI=" + pi}))}});
  check(parsed.queue_id == pi && parsed.peer_id == pi, "TXT keys are ASCII case insensitive and pi can supply the queue UUID");
  parsed = native_receiver_ids({{"psi", psi}, {"txtAirPlay", Json::binary(txt_fields({"psi=ABCDEF12-1234-4567-89AB-0123456789AB", "pi=" + pi}))}});
  check(parsed.queue_id == psi, "UUID case differences are equivalent and distinct psi and pi are allowed");
  for (auto key : {"psi", "pi", "gid"}) {
    rejects([&] { native_receiver_ids({{"psi", psi}, {key, pi}, {"txtAirPlay", Json::binary(txt_fields({std::string(key) + "=" + gid}))}}); },
            "Conflicting same-key top-level and TXT UUIDs fail closed");
    rejects([&] { native_receiver_ids({{"psi", psi}, {"txtAirPlay", Json::binary(txt_fields({std::string(key) + "=", std::string(key) + "=" + pi}))}}); },
            "Duplicate known keys fail even when their first value is invalid");
  }
  rejects([&] { native_receiver_ids({{"psi", psi}, {"txtAirPlay", Json::binary(txt_fields({"PSI=" + psi, "psi=" + psi}))}}); },
          "Case-insensitive duplicates fail even when values match");
  rejects([&] { native_receiver_ids({{"psi", psi}, {"txtAirPlay", Json::binary(Bytes{3, 'x', '='})}}); },
          "Truncated TXT length fails despite valid top-level UUID");
  rejects([&] { native_receiver_ids({{"psi", psi}, {"txtAirPlay", Json::binary(Bytes(4097, 0))}}); },
          "TXT total encoded size is bounded");
  rejects([&] { native_receiver_ids({{"psi", psi}, {"txtAirPlay", Json::binary(Bytes(65, 0))}}); },
          "Empty TXT records still count toward the field limit");
  rejects([&] { native_receiver_ids({{"psi", psi}, {"txtAirPlay", "psi=" + psi}}); },
          "Only binary DNS TXT data is accepted");
  auto bounded = txt_fields({"psi=" + psi});
  for (int i = 0; i < 15; ++i) {
    auto field = txt_fields({std::string(255, 'x')}); bounded.insert(bounded.end(), field.begin(), field.end());
  }
  auto last = txt_fields({std::string(214, 'x')}); bounded.insert(bounded.end(), last.begin(), last.end());
  check(bounded.size() == 4096 && native_receiver_ids({{"txtAirPlay", Json::binary(bounded)}}).queue_id == psi,
        "Exactly 4096 TXT bytes are accepted with bounded unknown fields");
  check(native_receiver_ids({{"psi", psi}, {"txtAirPlay", Json::binary(Bytes(64, 0))}}).queue_id == psi,
        "Exactly 64 empty TXT records are accepted");
  rejects([&] { native_receiver_ids({{"deviceID", pi}, {"receiver_id", pi}, {"pi", "02:03:04:05:06:07"}}); },
          "Device IDs and pairing identity never substitute for queue UUIDs");
  rejects([&] { native_receiver_ids(Json::array()); }, "Receiver information must be an object");
}
void builders() {
  auto id = identity();
  auto setup = native_session_setup(id);
  check(setup["timingProtocol"] == "PTP" && setup["isScreenMirroringSession"] == false,
        "Native playback has PTP non-mirroring metadata");
  check(native_control_setup(id, id.receiver_id)["streams"][0]["type"] == 130, "RCS control type");
  auto peers = native_peer_setup(id, id.receiver_id);
  check(peers[0]["ClockPorts"][id.receiver_id] == id.clock_port, "Bound clock port is advertised");
  auto stream = native_media_setup(id)["streams"][0];
  check(stream["type"] == 96 && stream["streamConnections"]["streamConnectionTypeRTCP"]["streamConnectionKeyPort"] == id.rtcp_port,
        "Native media setup carries bound RTCP port");
  auto item = native_insert(id, media);
  check(item["type"] == "insertPlayQueueItem" && item["item"]["Content-Location"] == media,
        "Original media URL reaches native queue unchanged");
  check(!item["item"].contains("HLS-Content-Location"), "MP4 has no HLS selection field");
  auto hls = media.substr(0, media.size()-3) + "m3u8";
  check(native_insert(id, hls)["item"]["HLS-Content-Location"] == hls, "HLS selection is explicit");
  auto envelope = plist_decode(native_command(item));
  check(envelope["params"]["data"].is_binary() && plist_decode(envelope["params"]["data"].get_binary()) == item,
        "Queue command roundtrips nested binary plist");
  for (const auto &url : {"https://192.168.250.10:8124/0123456789abcdef/clip.mp4",
                          "http://127.0.0.1:8124/0123456789abcdef/clip.mp4",
                          "http://8.8.8.8:8124/0123456789abcdef/clip.mp4",
                          "http://user:pass@192.168.250.10:8124/0123456789abcdef/clip.mp4",
                          "http://192.168.250.10:0/0123456789abcdef/clip.mp4",
                          "http://192.168.250.10:65536/0123456789abcdef/clip.mp4",
                          "http://192.168.250.10:8124/short/clip.mp4",
                          "http://192.168.250.10:8124/0123456789abcdef/../clip.mp4",
                          "http://192.168.250.10:8124/0123456789abcdef/clip.mp4?secret=value",
                          "http://192.168.250.10:8124/0123456789abcdef/clip.mp4\r\nHeader: bad"})
    rejects([&] { validate_native_url(url); }, "Reject unscoped or injected media URL");
  rejects([&] { native_control_setup(id, ""); }, "Missing queue identity fails closed");
  id.clock_port = 0; rejects([&] { native_session_setup(id); }, "Missing clock parameters fail closed");
}
void lifecycle() {
  Fake transport; std::atomic<bool> stop{false};
  std::vector<Json> events;
  Note note = [&](const std::string &name, const Json &data) {
    check(name == "native_stage", "Only safe native telemetry emitted");
    check(data.size() == 3 && data["stage"].is_string() && data["status"].is_number_integer() && data["elapsed_ms"].is_number_integer(),
          "Telemetry contains only stage, status and elapsed time");
    events.push_back(data);
  };
  native_session(transport, media, stop, note, 3);
  check(transport.verified == 1 && transport.opened == 1 && transport.closed == 1, "Normal completion closes exactly once");
  std::vector<std::string> stages;
  for (const auto &call : transport.calls) stages.push_back(call.stage);
  check(stages == std::vector<std::string>({"probe", "setup", "record", "peers", "media", "control", "insert", "property", "rate", "feedback", "feedback", "rate", "teardown"}),
        "Native state machine ordering and serialized feedback");
  check(queue_body(transport.calls[11])["rate"] == 0.0, "Stop command precedes teardown");
  for (const auto &call : transport.calls) {
    check(call.path != "/play" && call.path != "/playback-info", "Modern path does not use legacy playback endpoints");
    if (call.path == "/command") {
      check(call.headers.at("X-Apple-StreamID") == "1" && !call.headers.contains("X-Apple-Stream-ID") && !call.options.session_headers,
            "RCS queue requests use selected stream header without session headers");
    }
  }
  check(Json(events).dump().find(media) == std::string::npos, "Diagnostics never include media URL");
  for (const auto &stage : {"verify", "probe", "setup", "events", "record", "peers", "media", "control", "insert", "property", "rate", "feedback", "teardown"}) {
    Fake fail; fail.fail = stage;
    bool caught = false;
    try { native_session(fail, media, stop, note, 1); }
    catch (const std::exception &error) {
      caught = true;
      check(std::string(error.what()).find("private") == std::string::npos, "Library details are not exposed");
      check(std::string(error.what()).find(stage) != std::string::npos, "Exact failed stage retained");
    }
    check(caught && fail.closed == 1, "Each failed stage closes exactly once");
    check(fail.count("teardown") == (fail.opened || (std::string(stage) == "events") ? 1 : 0), "Only accepted sessions receive one teardown");
  }
  Fake malformed; malformed.missing_event = true;
  rejects([&] { native_session(malformed, media, stop, note, 1); }, "Missing event port rejects session");
  check(malformed.count("teardown") == 1 && malformed.closed == 1, "Accepted malformed setup still tears down");
  Fake identity_failure; identity_failure.invalid_identity = true;
  rejects([&] { native_session(identity_failure, media, stop, note, 1); }, "Invalid authenticated receiver identity fails closed");
  check(identity_failure.count("setup") == 0, "Unknown receiver identity never starts a session");
  Fake txt_fallback;
  const std::string txt_id = "66666666-6666-4666-8666-666666666666";
  txt_fallback.probe_info = {{"txtAirPlay", Json::binary(txt_fields({"psi=" + txt_id}))}};
  native_session(txt_fallback, media, stop, note, 1);
  auto control = std::find_if(txt_fallback.calls.begin(), txt_fallback.calls.end(), [](const auto &value) { return value.stage == "control"; });
  check(control != txt_fallback.calls.end() && plist_decode(control->body)["streams"][0]["channelID"] == txt_id + "-RCS-1" && txt_fallback.closed == 1,
        "Authenticated TXT fallback is used by the real state machine and closes normally");
  Fake txt_conflict;
  txt_conflict.probe_info = {{"psi", txt_id}, {"txtAirPlay", Json::binary(txt_fields({"psi=" + txt_conflict.id.receiver_id}))}};
  rejects([&] { native_session(txt_conflict, media, stop, note, 1); }, "Conflicting TXT identity stops the state machine");
  check(txt_conflict.count("setup") == 0 && txt_conflict.closed == 1, "Conflicting identities close before setup");
  Fake event_failure; event_failure.event_health = false;
  rejects([&] { native_session(event_failure, media, stop, note, 1); }, "Event connection loss stops trial");
  check(event_failure.count("teardown") == 1 && event_failure.closed == 1, "Event connection failure cleans up");
  check(event_failure.count("record") == 0 && event_failure.count("insert") == 0, "Failed event channel prevents subsequent startup and playback");
  Fake control_failure; control_failure.missing_control = true;
  rejects([&] { native_session(control_failure, media, stop, note, 1); }, "Missing negotiated queue stream fails closed");
  check(control_failure.count("insert") == 0 && control_failure.count("teardown") == 1, "Invalid queue setup never inserts media");
  Fake cancelled; cancelled.stop = &stop; cancelled.cancel_after = "setup";
  native_session(cancelled, media, stop, note, 1);
  check(cancelled.opened == 0 && cancelled.count("teardown") == 1 && cancelled.closed == 1,
        "Cancellation after accepted setup tears down without starting playback");
  Fake before; native_session(before, media, stop, note, 1);
  check(before.verified == 0 && before.calls.empty() && before.closed == 1, "Pre-cancelled session does not contact receiver");
  stop = false;
  Fake callback; native_session(callback, media, stop, [](const std::string &, const Json &) { throw std::runtime_error("UI failed"); }, 1);
  check(callback.closed == 1, "Throwing telemetry cannot abandon resources");
}
void rtsp_options() {
  int sockets[2]; check(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0, "RTSP socket pair");
  Rtsp rtsp(Socket(sockets[0]), "test-session");
  Channel peer{Socket(sockets[1])};
  std::vector<Message> received;
  std::exception_ptr peer_error;
  std::jthread server([&] {
    try {
      for (int i = 1; i <= 2; ++i) {
        received.push_back(peer.read_message());
        peer.write(bytes("RTSP/1.0 200 OK\r\nCSeq: " + std::to_string(i) + "\r\nSession: receiver-session\r\nContent-Length: 0\r\n\r\n"));
      }
    } catch (...) { peer_error = std::current_exception(); }
  });
  rtsp.request("OPTIONS", "*");
  RtspOptions options; options.session_headers = false; options.user_agent = "AirPlay/960.10.1";
  rtsp.request("POST", "/command", {}, "", {{"X-Apple-StreamID", "1"}}, options);
  server.join(); if (peer_error) std::rethrow_exception(peer_error);
  check(received[0].first_line == "OPTIONS * RTSP/1.0" && received[0].headers.at("user-agent") == "AirPlay/409.16" &&
            received[0].headers.at("x-apple-session-id") == "test-session", "Existing RTSP wire defaults remain unchanged");
  check(!received[1].headers.contains("session") && !received[1].headers.contains("x-apple-session-id") &&
            received[1].headers.at("x-apple-streamid") == "1", "RCS options suppress both session header spellings");
  rejects([&] { rtsp.request("POST", "/bad\r\nInjected"); }, "Request target injection rejected before write");
  rejects([&] { rtsp.request("POST", "/command", {}, "", {{"CSeq", "99"}}); }, "Reserved duplicate headers rejected");

  int stalled[2]; check(socketpair(AF_UNIX, SOCK_STREAM, 0, stalled) == 0, "Cancellation socket pair");
  Rtsp pending(Socket(stalled[0]), "cancel-session"); Channel stalled_peer{Socket(stalled[1])};
  auto request = std::async(std::launch::async, [&] {
    try { pending.request("GET", "/info"); return false; } catch (...) { return true; }
  });
  stalled_peer.read_message(); stalled_peer.write(bytes("RTSP/1.0 200 OK\r\nContent-Length: 20\r\n\r\npartial"));
  pending.shutdown();
  check(request.wait_for(std::chrono::seconds(1)) == std::future_status::ready && request.get(),
        "Shutdown interrupts a partial response without waiting for ordinary timeout");
}
} // namespace
int main() {
  try {
    require(sodium_init() >= 0, "Sodium initialization failed");
    receiver_ids(); builders(); lifecycle(); rtsp_options();
    std::cout << assertions << " native assertions passed\n";
    return 0;
  } catch (const std::exception &error) { std::cerr << error.what() << '\n'; return 1; }
}
