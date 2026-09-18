#include "lab.hpp"
#include <arpa/inet.h>
#include <cstring>
#include <fcntl.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sstream>
#include <sys/socket.h>
#include <unistd.h>
namespace lab {
Socket::~Socket() {
  if (fd_ >= 0)
    ::close(fd_);
}
Socket::Socket(Socket &&o) noexcept : fd_(o.fd_) { o.fd_ = -1; }
Socket &Socket::operator=(Socket &&o) noexcept {
  if (this != &o) {
    if (fd_ >= 0)
      ::close(fd_);
    fd_ = o.fd_;
    o.fd_ = -1;
  }
  return *this;
}
static bool poll_ready(int fd, short events, int ms) {
  pollfd p{fd, events, 0};
  int r;
  do {
    r = poll(&p, 1, ms);
  } while (r < 0 && errno == EINTR);
  require(r >= 0, "Network poll failed");
  require(!(p.revents & (POLLERR | POLLNVAL)), "Network socket error");
  return r > 0;
}
static void ready(int fd, short events, int ms) {
  require(poll_ready(fd, events, ms), "Network timeout");
}
Socket Socket::connect(const std::string &ip, uint16_t port, int timeout) {
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_port = htons(port);
  require(inet_pton(AF_INET, ip.c_str(), &a.sin_addr) == 1,
          "An IPv4 address is required");
  Socket s(::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0));
  require(s.fd() >= 0, "Socket allocation");
  fcntl(s.fd(), F_SETFL, O_NONBLOCK);
  int r = ::connect(s.fd(), reinterpret_cast<sockaddr *>(&a), sizeof(a));
  require(r == 0 || errno == EINPROGRESS, "Connection rejected");
  if (r != 0)
    ready(s.fd(), POLLOUT, timeout);
  int error = 0;
  socklen_t len = sizeof(error);
  getsockopt(s.fd(), SOL_SOCKET, SO_ERROR, &error, &len);
  require(error == 0, "Receiver connection failed");
  int yes = 1;
  setsockopt(s.fd(), IPPROTO_TCP, TCP_NODELAY, &yes, sizeof(yes));
  return s;
}
Bytes Socket::read_some(int timeout) {
  ready(fd_, POLLIN, timeout);
  Bytes b(16384);
  ssize_t n = recv(fd_, b.data(), b.size(), 0);
  require(n > 0, "Connection closed");
  b.resize(n);
  return b;
}
Bytes Socket::read_exact(size_t size, int timeout) {
  require(size <= 1024 * 1024, "Network read too large");
  Bytes b(size);
  size_t pos = 0;
  const auto end = Clock::now() + std::chrono::milliseconds(timeout);
  while (pos < size) {
    auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                    end - Clock::now())
                    .count();
    require(left > 0, "Network read timeout");
    ready(fd_, POLLIN, left);
    auto n = recv(fd_, b.data() + pos, size - pos, 0);
    if (n < 0 && (errno == EINTR || errno == EAGAIN))
      continue;
    require(n > 0, "Connection closed");
    pos += n;
  }
  return b;
}
void Socket::write(std::span<const uint8_t> data) {
  size_t pos = 0;
  auto end = Clock::now() + std::chrono::seconds(5);
  while (pos < data.size()) {
    auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                    end - Clock::now())
                    .count();
    require(left > 0, "Network write timeout");
    ready(fd_, POLLOUT, left);
    auto n = send(fd_, data.data() + pos, data.size() - pos, MSG_NOSIGNAL);
    if (n < 0 && (errno == EINTR || errno == EAGAIN))
      continue;
    require(n > 0, "Network write failed");
    pos += n;
  }
}
void Socket::shutdown() {
  if (fd_ >= 0)
    ::shutdown(fd_, SHUT_RDWR);
}
void Channel::encrypt(std::span<const uint8_t> secret, pair_channel kind,
                      const char *suffix) {
  require(!cipher_ && pending_.empty(),
          "Unexpected data during encryption switch");
  cipher_.reset(pair_cipher_new(PAIR_CLIENT_HOMEKIT_NORMAL, kind, secret.data(),
                                secret.size(), suffix));
  require(bool(cipher_), "Channel cipher initialization failed");
}
void Channel::write(std::span<const uint8_t> data) {
  if (!cipher_) {
    socket_.write(data);
    return;
  }
  uint8_t *out = nullptr;
  size_t len = 0;
  auto n = pair_encrypt(&out, &len, data.data(), data.size(), cipher_.get());
  std::unique_ptr<uint8_t, decltype(&free)> hold(out, free);
  require(n == ssize_t(data.size()), "Channel encryption failed");
  socket_.write(std::span(out, len));
}
Bytes Channel::read_block(int timeout) {
  if (!cipher_)
    return socket_.read_some(timeout);
  auto head = socket_.read_exact(2, timeout);
  size_t n = read_le(head);
  require(n > 0 && n <= 16384, "Invalid encrypted block length");
  auto tail = socket_.read_exact(n + 16, timeout);
  head.insert(head.end(), tail.begin(), tail.end());
  uint8_t *plain = nullptr;
  size_t len = 0;
  auto used =
      pair_decrypt(&plain, &len, head.data(), head.size(), cipher_.get());
  std::unique_ptr<uint8_t, decltype(&free)> hold(plain, free);
  require(used == ssize_t(head.size()) && len == n,
          "Channel authentication failed");
  return Bytes(plain, plain + len);
}
std::optional<Message> Channel::read_event(int idle_timeout,int message_timeout) {
  // Events are unsolicited: silence is normal. Once any bytes arrive, retain
  // the ordinary bounded framing/authentication checks, including partial data.
  if(pending_.empty()&&!poll_ready(socket_.fd(),POLLIN,idle_timeout)) return std::nullopt;
  return read_message(message_timeout);
}
Message Channel::read_message(int timeout) {
  const std::string delimiter = "\r\n\r\n";
  size_t header_end = 0;
  auto end = Clock::now() + std::chrono::milliseconds(timeout);
  auto append = [&] {
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                  end - Clock::now())
                  .count();
    require(ms > 0, "Message timeout");
    auto b = read_block(ms);
    pending_.insert(pending_.end(), b.begin(), b.end());
  };
  while (true) {
    auto it = std::search(pending_.begin(), pending_.end(), delimiter.begin(),
                          delimiter.end());
    if (it != pending_.end()) {
      header_end = it - pending_.begin() + 4;
      break;
    }
    require(pending_.size() < 32768, "Header too large");
    append();
  }
  Message m;
  std::istringstream s(
      std::string(pending_.begin(), pending_.begin() + header_end));
  std::getline(s, m.first_line);
  if (!m.first_line.empty() && m.first_line.back() == '\r')
    m.first_line.pop_back();
  if (m.first_line.starts_with("RTSP/") || m.first_line.starts_with("HTTP/")) {
    std::istringstream line(m.first_line);
    std::string proto;
    line >> proto >> m.status;
  }
  std::string line;
  while (std::getline(s, line) && line != "\r") {
    auto colon = line.find(':');
    require(colon != std::string::npos, "Malformed response header");
    auto key = line.substr(0, colon);
    std::transform(key.begin(), key.end(), key.begin(),
                   [](unsigned char c) { return std::tolower(c); });
    auto val = line.substr(colon + 1);
    while (!val.empty() &&
           std::isspace(static_cast<unsigned char>(val.front())))
      val.erase(val.begin());
    while (!val.empty() && std::isspace(static_cast<unsigned char>(val.back())))
      val.pop_back();
    require(!m.headers.contains(key), "Duplicate response header");
    m.headers[key] = val;
  }
  size_t body_size = 0;
  if (m.headers.contains("content-length")) {
    auto &v = m.headers["content-length"];
    require(!v.empty() &&
                std::all_of(v.begin(), v.end(),
                            [](unsigned char c) { return std::isdigit(c); }),
            "Invalid body length");
    body_size = std::stoull(v);
  }
  require(body_size <= 1024 * 1024 && !m.headers.contains("transfer-encoding"),
          "Unsupported response framing");
  while (pending_.size() < header_end + body_size)
    append();
  m.body = Bytes(pending_.begin() + header_end,
                 pending_.begin() + header_end + body_size);
  pending_.erase(pending_.begin(), pending_.begin() + header_end + body_size);
  return m;
}
Rtsp::Rtsp(const std::string &ip, uint16_t port, const std::string &id)
    : channel_(Socket::connect(ip, port)), id_(id), dacp_(hex(random_bytes(8))),
      active_(random_id() & 0xffffffff) {}
Message Rtsp::request(const std::string &method, const std::string &path,
                      const Bytes &body, const std::string &type,
                      const std::map<std::string, std::string> &headers) {
  std::ostringstream s;
  s << method << " " << path << " RTSP/1.0\r\nCSeq: " << ++seq_
    << "\r\nUser-Agent: AirPlay/409.16\r\nDACP-ID: " << dacp_
    << "\r\nActive-Remote: " << active_ << "\r\nX-Apple-Session-ID: " << id_
    << "\r\nContent-Length: " << body.size() << "\r\n";
  if (!type.empty())
    s << "Content-Type: " << type << "\r\n";
  if (!session_.empty())
    s << "Session: " << session_ << "\r\n";
  for (auto &[k, v] : headers)
    s << k << ": " << v << "\r\n";
  s << "\r\n";
  auto wire = bytes(s.str());
  wire.insert(wire.end(), body.begin(), body.end());
  channel_.write(wire);
  auto m = channel_.read_message();
  if (m.headers.contains("cseq"))
    require(m.headers.at("cseq") == std::to_string(seq_),
            "Response sequence mismatch");
  if (m.headers.contains("session"))
    session_ = m.headers.at("session");
  return m;
}
Json Rtsp::plist(const std::string &method, const std::string &path,
                 const Json &body) {
  auto m = request(method, path, plist_encode(body),
                   "application/x-apple-binary-plist");
  require(m.status == 200,
          method + " rejected (RTSP " + std::to_string(m.status) + ")");
  return m.body.empty() ? Json::object() : plist_decode(m.body);
}
} // namespace lab
