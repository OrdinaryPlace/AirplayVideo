#include "lab.hpp"
#include <arpa/inet.h>
#include <cstring>
#include <poll.h>
#include <set>
#include <sys/socket.h>
#include <unistd.h>
namespace lab {
Json Receiver::json() const {
  return {{"name", name},           {"address", address}, {"port", port},
          {"device_id", device_id}, {"model", model},     {"version", version}};
}
static uint16_t u16(std::span<const uint8_t> b, size_t i) {
  require(i + 2 <= b.size(), "Truncated DNS integer");
  return uint16_t(b[i]) << 8 | b[i + 1];
}
static std::string dns_name(std::span<const uint8_t> b, size_t &pos) {
  size_t cur = pos;
  bool jumped = false;
  std::string out;
  std::set<size_t> seen;
  for (int labels = 0; labels < 128; ++labels) {
    require(cur < b.size() && seen.insert(cur).second, "Invalid DNS name");
    uint8_t n = b[cur++];
    if ((n & 192) == 192) {
      require(cur < b.size(), "Truncated DNS pointer");
      size_t target = ((n & 63) << 8) | b[cur++];
      if (!jumped)
        pos = cur;
      jumped = true;
      cur = target;
      continue;
    }
    require(n <= 63 && cur + n <= b.size(), "Invalid DNS label");
    if (!n) {
      if (!jumped)
        pos = cur;
      return out;
    }
    if (!out.empty())
      out += '.';
    out.append(reinterpret_cast<const char *>(b.data() + cur), n);
    cur += n;
    require(out.size() < 1024, "DNS name too long");
  }
  throw std::runtime_error("DNS name loop");
}
static void append_name(Bytes &b, std::string_view s) {
  size_t start = 0;
  while (start < s.size()) {
    auto end = s.find('.', start);
    if (end == std::string_view::npos)
      end = s.size();
    b.push_back(end - start);
    b.insert(b.end(), s.begin() + start, s.begin() + end);
    start = end + 1;
  }
  b.push_back(0);
}
std::vector<Receiver> discover(int ms) {
  Socket sock(::socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0));
  require(sock.fd() >= 0, "Discovery socket failed");
  sockaddr_in local{};
  local.sin_family = AF_INET;
  local.sin_addr.s_addr = INADDR_ANY;
  require(
      bind(sock.fd(), reinterpret_cast<sockaddr *>(&local), sizeof(local)) == 0,
      "Discovery bind failed");
  // An ephemeral source port requests legacy unicast replies (RFC 6762 6.7).
  auto query = [&](const std::string &name, uint16_t type) {
    Bytes b(12);
    be(b, 0, 1, 2);
    be(b, 4, 1, 2);
    append_name(b, name);
    size_t o = b.size();
    b.resize(o + 4);
    be(b, o, type, 2);
    be(b, o + 2, 1, 2);
    sockaddr_in group{};
    group.sin_family = AF_INET;
    group.sin_port = htons(5353);
    inet_pton(AF_INET, "224.0.0.251", &group.sin_addr);
    sendto(sock.fd(), b.data(), b.size(), 0,
           reinterpret_cast<sockaddr *>(&group), sizeof(group));
  };
  struct Entry {
    Receiver receiver;
    std::string target;
  };
  std::map<std::string, Entry> services;
  std::map<std::string, std::string> hosts;
  std::set<std::string> queried;
  query("_airplay._tcp.local", 12);
  auto deadline = Clock::now() + std::chrono::milliseconds(ms);
  while (Clock::now() < deadline) {
    pollfd p{sock.fd(), POLLIN, 0};
    if (poll(&p, 1, 100) <= 0)
      continue;
    Bytes packet(9000);
    auto n = recv(sock.fd(), packet.data(), packet.size(), 0);
    if (n < 12)
      continue;
    packet.resize(n);
    try {
      size_t pos = 12;
      auto questions = u16(packet, 4);
      uint32_t count = u16(packet, 6) + u16(packet, 8) + u16(packet, 10);
      require(questions < 128 && count < 512, "Too many DNS records");
      for (int i = 0; i < questions; ++i) {
        dns_name(packet, pos);
        pos += 4;
        require(pos <= packet.size(), "DNS question truncated");
      }
      for (uint32_t i = 0; i < count; ++i) {
        auto owner = dns_name(packet, pos);
        auto type = u16(packet, pos);
        auto len = u16(packet, pos + 8);
        pos += 10;
        require(pos + len <= packet.size(), "DNS record truncated");
        size_t end = pos + len;
        if (type == 12 && owner == "_airplay._tcp.local") {
          size_t offset = pos;
          auto instance = dns_name(packet, offset);
          auto &e = services[instance];
          auto suffix = instance.find("._airplay._tcp.local");
          if (suffix != std::string::npos)
            e.receiver.name = instance.substr(0, suffix);
          if (queried.insert(instance).second)
            query(instance, 255);
        } else if (type == 33 && owner.ends_with("._airplay._tcp.local") &&
                   len >= 7) {
          auto &e = services[owner];
          e.receiver.name = owner.substr(0, owner.size() - 20);
          e.receiver.port = u16(packet, pos + 4);
          size_t offset = pos + 6;
          e.target = dns_name(packet, offset);
          if (queried.insert(e.target).second)
            query(e.target, 1);
        } else if (type == 16 && owner.ends_with("._airplay._tcp.local")) {
          auto &e = services[owner];
          size_t q = pos;
          while (q < end) {
            size_t size = packet[q++];
            require(q + size <= end, "TXT truncated");
            std::string txt(packet.begin() + q, packet.begin() + q + size);
            q += size;
            auto eq = txt.find('=');
            if (eq == std::string::npos)
              continue;
            auto k = txt.substr(0, eq), v = txt.substr(eq + 1);
            if (k == "deviceid")
              e.receiver.device_id = v;
            else if (k == "model")
              e.receiver.model = v;
            else if (k == "srcvers")
              e.receiver.version = v;
          }
        } else if (type == 1 && len == 4) {
          char ip[INET_ADDRSTRLEN];
          inet_ntop(AF_INET, packet.data() + pos, ip, sizeof(ip));
          hosts[owner] = ip;
        }
        pos = end;
      }
    } catch (const std::exception &) { /* Ignore malformed advertisements; never
                                          log packet data. */
    }
  }
  std::vector<Receiver> out;
  for (auto &[name, e] : services) {
    if (hosts.contains(e.target) && !e.receiver.name.empty()) {
      e.receiver.address = hosts[e.target];
      out.push_back(e.receiver);
    }
  }
  std::sort(out.begin(), out.end(),
            [](const auto &a, const auto &b) { return a.name < b.name; });
  return out;
}
Json probe(Rtsp &rtsp, const std::string &expected) {
  auto m =
      rtsp.request("GET", "/info", {}, "", {{"X-Apple-ProtocolVersion", "1"}});
  require(m.status == 200,
          "Receiver info rejected (RTSP " + std::to_string(m.status) + ")");
  auto info = plist_decode(m.body);
  require(!info.value("name", "").empty() &&
              (expected.empty() || info.value("name", "") == expected),
          "Receiver name does not match selection");
  Json safe = Json::object();
  for (const auto *k : {"name", "model", "sourceVersion", "deviceID",
                        "features", "statusFlags", "displays"})
    if (info.contains(k))
      safe[k] = info[k];
  return safe;
}
} // namespace lab
