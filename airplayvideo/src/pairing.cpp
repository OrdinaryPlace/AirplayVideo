#include "lab.hpp"
#include <sodium.h>
namespace lab {
Json Credentials::json() const {
  return {{"schema", 1},
          {"sender_id", sender_id},
          {"keys", keys},
          {"receiver_id", receiver_id},
          {"receiver", receiver.json()}};
}
Credentials Credentials::from_json(const Json &j) {
  require(j.value("schema", 0) == 1, "Unsupported saved state schema");
  Credentials c;
  c.sender_id = j.at("sender_id");
  c.keys = j.at("keys");
  c.receiver_id = j.at("receiver_id");
  auto &r = j.at("receiver");
  c.receiver = {
      r.at("name"),         r.at("address"),        r.value("device_id", ""),
      r.value("model", ""), r.value("version", ""), r.at("port")};
  require(c.sender_id.size() == 36 && c.keys.size() == 192 &&
              !c.receiver_id.empty() && c.receiver_id.size() < 64,
          "Invalid saved pairing; preserved existing file");
  unhex(c.keys);
  require(!c.receiver.name.empty() && c.receiver.name.size() < 128 &&
              c.receiver.port > 0,
          "Invalid receiver state");
  return c;
}
static void check_response(const Message &m, int expected) {
  require(m.status == 200,
          "Pairing request rejected (RTSP " + std::to_string(m.status) + ")");
  const char *error = nullptr;
  int state = pair_state_get(PAIR_CLIENT_HOMEKIT_NORMAL, &error, m.body.data(),
                             m.body.size());
  require(state == expected, "Pairing response rejected or unexpected stage");
}
Credentials pair_with_pin(Rtsp &rtsp, const Receiver &receiver,
                          const std::string &id, const std::string &pin,
                          const Note &note) {
  require(pin.size() >= 4 && pin.size() <= 8 &&
              std::all_of(pin.begin(), pin.end(),
                          [](unsigned char c) { return std::isdigit(c); }),
          "Enter the displayed numeric PIN");
  std::unique_ptr<pair_setup_context, decltype(&pair_setup_free)> ctx(
      pair_setup_new(PAIR_CLIENT_HOMEKIT_NORMAL, pin.c_str(), nullptr, nullptr,
                     id.c_str()),
      pair_setup_free);
  require(bool(ctx), "Could not initialize PIN pairing");
  using Request = uint8_t *(*)(size_t *, pair_setup_context *);
  using Response = int (*)(pair_setup_context *, const uint8_t *, size_t);
  std::array<Request, 3> requests = {pair_setup_request1, pair_setup_request2,
                                     pair_setup_request3};
  std::array<Response, 3> responses = {
      pair_setup_response1, pair_setup_response2, pair_setup_response3};
  for (int step = 0; step < 3; ++step) {
    size_t size = 0;
    uint8_t *raw = requests[step](&size, ctx.get());
    std::unique_ptr<uint8_t, decltype(&free)> hold(raw, free);
    require(raw && size <= 4096, "Pairing message creation failed");
    auto m = rtsp.request("POST", "/pair-setup", Bytes(raw, raw + size),
                          "application/octet-stream", {{"X-Apple-HKP", "3"}});
    check_response(m, (step + 1) * 2);
    require(responses[step](ctx.get(), m.body.data(), m.body.size()) == 0,
            step == 1 ? "PIN proof rejected; no automatic retry"
                      : "Pairing proof validation failed");
    note("pair_setup_" + std::to_string((step + 1) * 2), {{"accepted", true}});
  }
  const char *keys = nullptr;
  pair_result *result = nullptr;
  require(pair_setup_result(&keys, &result, ctx.get()) == 0 && keys && result,
          "Pairing did not produce credentials");
  Credentials c{id, keys, result->device_id, receiver};
  require(c.keys.size() == 192, "Pairing did not pin the receiver public key");
  return c;
}
Bytes verify_pair(Rtsp &rtsp, const Credentials &c, const Note &note) {
  require(c.keys.size() == 192,
          "Receiver public key is required for verification");
  std::unique_ptr<pair_verify_context, decltype(&pair_verify_free)> ctx(
      pair_verify_new(PAIR_CLIENT_HOMEKIT_NORMAL, c.keys.c_str(), nullptr,
                      nullptr, c.sender_id.c_str()),
      pair_verify_free);
  require(bool(ctx), "Could not initialize saved-pairing verification");
  for (int step = 0; step < 2; ++step) {
    size_t size = 0;
    uint8_t *raw = step == 0 ? pair_verify_request1(&size, ctx.get())
                             : pair_verify_request2(&size, ctx.get());
    std::unique_ptr<uint8_t, decltype(&free)> hold(raw, free);
    require(raw && size <= 4096, "Verification message creation failed");
    auto m = rtsp.request("POST", "/pair-verify", Bytes(raw, raw + size),
                          "application/octet-stream", {{"X-Apple-HKP", "3"}});
    check_response(m, (step + 1) * 2);
    int ok =
        step == 0
            ? pair_verify_response1(ctx.get(), m.body.data(), m.body.size())
            : pair_verify_response2(ctx.get(), m.body.data(), m.body.size());
    require(ok == 0,
            "Pairing identity verification failed; credentials preserved");
    note("pair_verify_" + std::to_string((step + 1) * 2), {{"accepted", true}});
  }
  pair_result *result = nullptr;
  require(pair_verify_result(&result, ctx.get()) == 0 && result &&
              result->shared_secret_len == 32,
          "Session key agreement failed");
  Bytes secret(result->shared_secret,
               result->shared_secret + result->shared_secret_len);
  rtsp.encrypt(secret);
  return secret;
}
} // namespace lab
