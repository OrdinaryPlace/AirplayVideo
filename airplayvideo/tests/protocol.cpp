#include "lab.hpp"
#include <iostream>
#include <sodium.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>
using namespace lab;
static int assertions = 0;
static void check(bool value, const char *message) {
  ++assertions;
  require(value, message);
}
template <class F> static void rejected(F test, const char *message) {
  bool failed = false;
  try {
    test();
  } catch (...) {
    failed = true;
  }
  check(failed, message);
}
template <class T> using Owned = std::unique_ptr<T, void (*)(T *)>;
static void event_channel_test() {
  int sockets[2];check(socketpair(AF_UNIX,SOCK_STREAM,0,sockets)==0,"Event socket pair");
  Channel events{Socket(sockets[0])};Socket peer(sockets[1]);
  auto secret=random_bytes(32);events.encrypt(secret,PAIR_CHANNEL_EVENTS);
  Owned<pair_cipher_context> server(pair_cipher_new(PAIR_SERVER_HOMEKIT,PAIR_CHANNEL_EVENTS,secret.data(),secret.size(),nullptr),pair_cipher_free);
  check(bool(server),"Event peer cipher");
  for(int i=0;i<5;++i)check(!events.read_event(10,30),"Quiet event channel remains healthy across idle deadlines");
  const auto message=bytes("POST /event HTTP/1.1\r\nCSeq: 7\r\nContent-Length: 3\r\n\r\nonePOST /event HTTP/1.1\r\nCSeq: 8\r\nContent-Length: 3\r\n\r\ntwo");
  uint8_t *raw=nullptr;size_t size=0;
  check(pair_encrypt(&raw,&size,message.data(),message.size(),server.get())==ssize_t(message.size()),"Event peer encryption");
  Owned<uint8_t> wire(raw,[](uint8_t *p){free(p);});
  std::jthread writer([&]{peer.write(std::span(raw,size).first(1));std::this_thread::sleep_for(std::chrono::milliseconds(10));peer.write(std::span(raw,size).subspan(1));});
  auto first=events.read_event(100,100);writer.join();
  check(first&&first->body==bytes("one")&&first->headers.at("cseq")=="7","Fragmented encrypted event after idle");
  auto second=events.read_event(0,100);
  check(second&&second->body==bytes("two")&&second->headers.at("cseq")=="8","Buffered event does not wait for another socket read");
  check(!events.read_event(10,30),"Channel can return to idle after an event");
  peer.shutdown();rejected([&]{events.read_event(100,100);},"Actual event peer closure still fails");
  int partial[2];check(socketpair(AF_UNIX,SOCK_STREAM,0,partial)==0,"Partial event socket pair");
  Channel incomplete{Socket(partial[0])};Socket incomplete_peer(partial[1]);
  incomplete_peer.write(bytes("POST /event HTTP/1.1\r\n"));
  rejected([&]{incomplete.read_event(10,20);},"Started but stalled event remains bounded");
}
struct Peer {
  std::array<uint8_t, 32> key{};
  std::string id;
};
static int get_peer(uint8_t *key, const char *id, void *ctx) {
  auto &p = *static_cast<Peer *>(ctx);
  if (p.id != id)
    return -1;
  std::copy(p.key.begin(), p.key.end(), key);
  return 0;
}
static void pairing_test() {
  const char *client_id = "87c0dd5f-d807-4375-abd7-9e78fc67bb43";
  const char *server_id = "receiver-test";
  Owned<pair_setup_context> client(pair_setup_new(PAIR_CLIENT_HOMEKIT_NORMAL,
                                                  "1234", nullptr, nullptr,
                                                  client_id),
                                   pair_setup_free);
  Owned<pair_setup_context> server(
      pair_setup_new(PAIR_SERVER_HOMEKIT, "1234", nullptr, nullptr, server_id),
      pair_setup_free);
  check(bool(client) && bool(server), "Pair contexts");
  using Request = uint8_t *(*)(size_t *, pair_setup_context *);
  using Response = int (*)(pair_setup_context *, const uint8_t *, size_t);
  std::array<Request, 3> req = {pair_setup_request1, pair_setup_request2,
                                pair_setup_request3};
  std::array<Response, 3> rsp = {pair_setup_response1, pair_setup_response2,
                                 pair_setup_response3};
  for (int i = 0; i < 3; ++i) {
    size_t n = 0, m = 0;
    Owned<uint8_t> q(req[i](&n, client.get()), [](uint8_t *p) { free(p); });
    uint8_t *raw = nullptr;
    check(pair_setup(&raw, &m, server.get(), q.get(), n) == 0,
          "Server pair step");
    Owned<uint8_t> a(raw, [](uint8_t *p) { free(p); });
    check(rsp[i](client.get(), a.get(), m) == 0, "Client pair step");
  }
  const char *keys = nullptr;
  pair_result *cr = nullptr, *sr = nullptr;
  check(pair_setup_result(&keys, &cr, client.get()) == 0, "Client pair result");
  check(pair_setup_result(nullptr, &sr, server.get()) == 0,
        "Server pair result");
  check(keys && std::strlen(keys) == 192, "Receiver key is pinned");
  Peer peer;
  peer.id = sr->device_id;
  std::copy(std::begin(sr->client_public_key), std::end(sr->client_public_key),
            peer.key.begin());
  auto verify = [&](const std::string &stored, bool should_work) {
    Owned<pair_verify_context> c(pair_verify_new(PAIR_CLIENT_HOMEKIT_NORMAL,
                                                 stored.c_str(), nullptr,
                                                 nullptr, client_id),
                                 pair_verify_free);
    Owned<pair_verify_context> s(pair_verify_new(PAIR_SERVER_HOMEKIT, nullptr,
                                                 get_peer, &peer, server_id),
                                 pair_verify_free);
    check(bool(c) && bool(s), "Verify contexts");
    size_t qn = 0, an = 0;
    Owned<uint8_t> q(pair_verify_request1(&qn, c.get()),
                     [](uint8_t *p) { free(p); });
    uint8_t *raw = nullptr;
    check(pair_verify(&raw, &an, s.get(), q.get(), qn) == 0, "Server verify 1");
    Owned<uint8_t> a(raw, [](uint8_t *p) { free(p); });
    int ok = pair_verify_response1(c.get(), a.get(), an);
    if (!should_work) {
      check(ok < 0, "Wrong receiver identity rejected");
      return;
    }
    check(ok == 0, "Server signature verified");
    q.reset(pair_verify_request2(&qn, c.get()));
    raw = nullptr;
    check(pair_verify(&raw, &an, s.get(), q.get(), qn) == 0, "Server verify 2");
    a.reset(raw);
    check(pair_verify_response2(c.get(), a.get(), an) == 0, "Client verify 2");
    pair_result *left = nullptr, *right = nullptr;
    check(pair_verify_result(&left, c.get()) == 0 &&
              pair_verify_result(&right, s.get()) == 0,
          "Verify results");
    check(left->shared_secret_len == 32 && right->shared_secret_len == 32 &&
              sodium_memcmp(left->shared_secret, right->shared_secret, 32) == 0,
          "Shared secrets agree");
    Owned<pair_cipher_context> tx(
        pair_cipher_new(PAIR_CLIENT_HOMEKIT_NORMAL, PAIR_CHANNEL_CONTROL,
                        left->shared_secret, 32, nullptr),
        pair_cipher_free);
    Owned<pair_cipher_context> rx(
        pair_cipher_new(PAIR_SERVER_HOMEKIT, PAIR_CHANNEL_CONTROL,
                        right->shared_secret, 32, nullptr),
        pair_cipher_free);
    Bytes plain(4097, 42);
    uint8_t *wire = nullptr, *back = nullptr;
    size_t wn = 0, bn = 0;
    check(pair_encrypt(&wire, &wn, plain.data(), plain.size(), tx.get()) ==
              ssize_t(plain.size()),
          "Multi-block encrypt");
    Owned<uint8_t> w(wire, [](uint8_t *p) { free(p); });
    check(pair_decrypt(&back, &bn, wire, wn, rx.get()) == ssize_t(wn),
          "Multi-block decrypt");
    Owned<uint8_t> b(back, [](uint8_t *p) { free(p); });
    check(bn == plain.size() && std::equal(plain.begin(), plain.end(), back),
          "Control payload preserved");
    check(pair_decrypt(&back, &bn, wire, wn, rx.get()) < 0,
          "Replayed counter rejected");
  };
  std::string saved = keys;
  verify(saved, true);
  verify(saved, true);
  saved.back() = saved.back() == '0' ? '1' : '0';
  verify(saved, false);
  Owned<pair_setup_context> bad(pair_setup_new(PAIR_CLIENT_HOMEKIT_NORMAL,
                                               "4321", nullptr, nullptr,
                                               client_id),
                                pair_setup_free);
  Owned<pair_setup_context> fresh(
      pair_setup_new(PAIR_SERVER_HOMEKIT, "1234", nullptr, nullptr, server_id),
      pair_setup_free);
  for (int i = 0; i < 2; ++i) {
    size_t qn = 0, an = 0;
    Owned<uint8_t> q(req[i](&qn, bad.get()), [](uint8_t *p) { free(p); });
    uint8_t *raw = nullptr;
    int r = pair_setup(&raw, &an, fresh.get(), q.get(), qn);
    Owned<uint8_t> a(raw, [](uint8_t *p) { free(p); });
    if (i == 0)
      check(r == 0 && rsp[i](bad.get(), a.get(), an) == 0,
            "Wrong PIN challenge exchange");
    else
      check(r < 0 || rsp[i](bad.get(), a.get(), an) < 0, "Wrong PIN rejected");
  }
}
int main() {
  try {
    check(sodium_init() >= 0, "Sodium initialization");
    auto value = Json{{"binary", Json::binary(Bytes{0, 1, 255})},
                      {"array", Json::array({true, 42, "hello"})},
                      {"id", uint64_t(0xfedcba9876543210)}};
    check(plist_decode(plist_encode(value)) == value,
          "Binary plist mixed types");
    rejected([] { plist_decode(Bytes{1, 2, 3}); }, "Malformed plist rejected");
    auto key = random_bytes(32), plain = bytes("Hello World"),
         aad = bytes("authenticated header");
    auto encrypted = seal(key, 19, aad, plain);
    check(open_sealed(key, 19, aad, encrypted) == plain, "AEAD roundtrip");
    rejected([&] { open_sealed(key, 20, aad, encrypted); },
             "Wrong nonce rejected");
    aad[0] ^= 1;
    rejected([&] { open_sealed(key, 19, aad, encrypted); },
             "Header tamper rejected");
    aad[0] ^= 1;
    encrypted.back() ^= 1;
    rejected([&] { open_sealed(key, 19, aad, encrypted); },
             "Tag tamper rejected");
    auto dir =
        std::filesystem::temp_directory_path() / ("airplay-lab-test-" + uuid());
    auto state = dir / "state";
    private_write_new(state, "original");
    check(private_read(state) == "original", "Saved state readback");
    rejected([&] { private_write_new(state, "replacement"); },
             "Existing state protected");
    check(private_read(state) == "original", "Original state retained");
    chmod(state.c_str(), 0644);
    rejected([&] { private_read(state); },
             "Broad credential permissions rejected");
    chmod(state.c_str(), 0600);
    std::filesystem::create_symlink(state, dir / "link");
    rejected([&] { private_read(dir / "link"); },
             "Credential symlink rejected");
    std::filesystem::remove_all(dir);
    int fds[2];
    check(socketpair(AF_UNIX, SOCK_STREAM, 0, fds) == 0, "Socketpair");
    Channel channel{Socket(fds[0])};
    Socket peer(fds[1]);
    auto wire =
        bytes("RTSP/1.0 200 OK\r\nCSeq: 7\r\nContent-Length: "
              "5\r\n\r\nhelloRTSP/1.0 204 OK\r\nContent-Length: 0\r\n\r\n");
    std::jthread writer([&] {
      for (size_t i = 0; i < wire.size(); i += 3)
        peer.write(
            std::span(wire).subspan(i, std::min(size_t(3), wire.size() - i)));
    });
    auto message = channel.read_message();
    check(message.status == 200 && message.body == bytes("hello"),
          "Fragmented RTSP body");
    check(channel.read_message().status == 204, "Pipelined RTSP response");
    writer.join();
    Video video;
    auto frame = video.frame(0);
    check(frame.keyframe && frame.avcc[0] == 1 && !frame.payload.empty(),
          "Generated initial AVC keyframe");
    auto packet = mirror_packet(frame, key, 0, 0x0102030405060708);
    check(packet.size() == 128 + frame.payload.size() + 16 &&
              read_le(std::span(packet).first(4)) == frame.payload.size() + 16,
          "Mirror size includes authentication tag");
    check(read_le(std::span(packet).subspan(8, 8)) == 0x0102030405060708,
          "Mirror timestamp byte order");
    check(open_sealed(key, 0, std::span(packet).first(128),
                      std::span(packet).subspan(128)) == frame.payload,
          "Mirror frame authenticates full header");
    auto second = video.frame(1);
    check(second.payload != frame.payload, "Generated animation changes");
    pairing_test();
    event_channel_test();
    std::cout << assertions << " assertions passed\n";
    return 0;
  } catch (const std::exception &e) {
    std::cerr << "Test failed: " << e.what() << '\n';
    return 1;
  }
}
