#include "lab.hpp"
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <openssl/evp.h>
#include <openssl/kdf.h>
#include <plist/plist.h>
#include <sodium.h>
#include <sys/stat.h>
#include <unistd.h>
namespace lab {
void require(bool c, const std::string &m) {
  if (!c)
    throw std::runtime_error(m);
}
Bytes bytes(std::string_view s) { return Bytes(s.begin(), s.end()); }
std::string hex(std::span<const uint8_t> v) {
  std::string out(v.size() * 2, '0');
  const char *h = "0123456789abcdef";
  for (size_t i = 0; i < v.size(); ++i) {
    out[2 * i] = h[v[i] >> 4];
    out[2 * i + 1] = h[v[i] & 15];
  }
  return out;
}
Bytes unhex(std::string_view s) {
  require(s.size() % 2 == 0, "Invalid hex length");
  Bytes b(s.size() / 2);
  auto digit = [](char c) -> int {
    if (c >= '0' && c <= '9')
      return c - '0';
    if (c >= 'a' && c <= 'f')
      return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
      return c - 'A' + 10;
    throw std::runtime_error("Invalid hex");
  };
  for (size_t i = 0; i < b.size(); ++i)
    b[i] = digit(s[2 * i]) * 16 + digit(s[2 * i + 1]);
  return b;
}
Bytes random_bytes(size_t n) {
  Bytes b(n);
  randombytes_buf(b.data(), b.size());
  return b;
}
std::string uuid() {
  auto b = random_bytes(16);
  b[6] = (b[6] & 15) | 64;
  b[8] = (b[8] & 63) | 128;
  auto s = hex(b);
  return s.substr(0, 8) + "-" + s.substr(8, 4) + "-" + s.substr(12, 4) + "-" +
         s.substr(16, 4) + "-" + s.substr(20);
}
uint64_t random_id() {
  return read_le(random_bytes(8)) & 0x7fffffffffffffffULL;
}
void le(Bytes &b, size_t o, uint64_t v, size_t w) {
  require(w <= 8 && o + w <= b.size(), "LE overflow");
  for (size_t i = 0; i < w; ++i)
    b[o + i] = v >> (8 * i);
}
void be(Bytes &b, size_t o, uint64_t v, size_t w) {
  require(w <= 8 && o + w <= b.size(), "BE overflow");
  for (size_t i = 0; i < w; ++i)
    b[o + w - 1 - i] = v >> (8 * i);
}
uint64_t read_le(std::span<const uint8_t> b) {
  require(b.size() <= 8, "LE width");
  uint64_t r = 0;
  for (size_t i = 0; i < b.size(); ++i)
    r |= uint64_t(b[i]) << (8 * i);
  return r;
}
uint64_t ntp_now() {
  auto n = std::chrono::duration_cast<std::chrono::nanoseconds>(
               Clock::now().time_since_epoch())
               .count();
  return (uint64_t(n / 1000000000) << 32) |
         ((uint64_t(n % 1000000000) << 32) / 1000000000);
}
uint64_t screen_to_ntp(uint64_t screen_time) {
  return screen_time + (uint64_t(2208988800) << 32);
}
Bytes hkdf(std::span<const uint8_t> secret, std::string_view salt,
           std::string_view info, size_t n) {
  std::unique_ptr<EVP_PKEY_CTX, decltype(&EVP_PKEY_CTX_free)> c(
      EVP_PKEY_CTX_new_id(EVP_PKEY_HKDF, nullptr), EVP_PKEY_CTX_free);
  require(bool(c), "HKDF allocation");
  require(EVP_PKEY_derive_init(c.get()) > 0 &&
              EVP_PKEY_CTX_set_hkdf_md(c.get(), EVP_sha512()) > 0 &&
              EVP_PKEY_CTX_set1_hkdf_salt(
                  c.get(), reinterpret_cast<const unsigned char *>(salt.data()),
                  salt.size()) > 0 &&
              EVP_PKEY_CTX_set1_hkdf_key(c.get(), secret.data(),
                                         secret.size()) > 0 &&
              EVP_PKEY_CTX_add1_hkdf_info(
                  c.get(), reinterpret_cast<const unsigned char *>(info.data()),
                  info.size()) > 0,
          "HKDF init");
  Bytes out(n);
  require(EVP_PKEY_derive(c.get(), out.data(), &n) > 0, "HKDF derive");
  out.resize(n);
  return out;
}
Bytes seal(std::span<const uint8_t> key, uint64_t nonce,
           std::span<const uint8_t> aad, std::span<const uint8_t> plain) {
  require(key.size() == 32, "AEAD key length");
  Bytes iv(12);
  le(iv, 4, nonce, 8);
  Bytes out(plain.size() + 16);
  unsigned long long n = 0;
  require(crypto_aead_chacha20poly1305_ietf_encrypt(
              out.data(), &n, plain.data(), plain.size(), aad.data(),
              aad.size(), nullptr, iv.data(), key.data()) == 0,
          "AEAD seal");
  out.resize(n);
  return out;
}
Bytes open_sealed(std::span<const uint8_t> key, uint64_t nonce,
                  std::span<const uint8_t> aad,
                  std::span<const uint8_t> cipher) {
  require(key.size() == 32 && cipher.size() >= 16, "AEAD length");
  Bytes iv(12);
  le(iv, 4, nonce, 8);
  Bytes out(cipher.size() - 16);
  unsigned long long n = 0;
  require(crypto_aead_chacha20poly1305_ietf_decrypt(
              out.data(), &n, nullptr, cipher.data(), cipher.size(), aad.data(),
              aad.size(), iv.data(), key.data()) == 0,
          "AEAD authentication failed");
  out.resize(n);
  return out;
}
static plist_t to_plist(const Json &j) {
  if (j.is_object()) {
    auto p = plist_new_dict();
    for (auto it = j.begin(); it != j.end(); ++it)
      plist_dict_set_item(p, it.key().c_str(), to_plist(it.value()));
    return p;
  }
  if (j.is_array()) {
    auto p = plist_new_array();
    for (const auto &e : j)
      plist_array_append_item(p, to_plist(e));
    return p;
  }
  if (j.is_binary()) {
    auto &b = j.get_binary();
    return plist_new_data(reinterpret_cast<const char *>(b.data()), b.size());
  }
  if (j.is_boolean())
    return plist_new_bool(j.get<bool>());
  if (j.is_number_float())
    return plist_new_real(j.get<double>());
  if (j.is_number())
    return plist_new_uint(j.get<uint64_t>());
  if (j.is_string())
    return plist_new_string(j.get_ref<const std::string &>().c_str());
  throw std::runtime_error("Unsupported plist type");
}
static Json from_plist(plist_t p, int depth = 0) {
  require(p && depth < 32, "Invalid plist depth");
  switch (plist_get_node_type(p)) {
  case PLIST_DICT: {
    Json j = Json::object();
    plist_dict_iter iter = nullptr;
    plist_dict_new_iter(p, &iter);
    char *key = nullptr;
    plist_t val = nullptr;
    while (true) {
      plist_dict_next_item(p, iter, &key, &val);
      if (!key)
        break;
      std::string k(key);
      free(key);
      j[k] = from_plist(val, depth + 1);
    }
    free(iter);
    return j;
  }
  case PLIST_ARRAY: {
    Json j = Json::array();
    for (uint32_t i = 0; i < plist_array_get_size(p); ++i)
      j.push_back(from_plist(plist_array_get_item(p, i), depth + 1));
    return j;
  }
  case PLIST_STRING:
  case PLIST_KEY: {
    char *v = nullptr;
    plist_get_string_val(p, &v);
    std::string s = v ? v : "";
    free(v);
    return s;
  }
  case PLIST_UINT: {
    uint64_t n = 0;
    plist_get_uint_val(p, &n);
    return n;
  }
  case PLIST_REAL: {
    double n = 0;
    plist_get_real_val(p, &n);
    return n;
  }
  case PLIST_BOOLEAN: {
    uint8_t n = 0;
    plist_get_bool_val(p, &n);
    return bool(n);
  }
  case PLIST_DATA: {
    char *v = nullptr;
    uint64_t n = 0;
    plist_get_data_val(p, &v, &n);
    Bytes b(v, v + n);
    free(v);
    return Json::binary(b);
  }
  default:
    return nullptr;
  }
}
Bytes plist_encode(const Json &j) {
  auto p = to_plist(j);
  char *b = nullptr;
  uint32_t n = 0;
  plist_to_bin(p, &b, &n);
  plist_free(p);
  require(b && n, "Plist encode");
  Bytes out(b, b + n);
  free(b);
  return out;
}
Json plist_decode(std::span<const uint8_t> b) {
  require(b.size() <= 1024 * 1024, "Plist too large");
  plist_t p = nullptr;
  plist_from_memory(reinterpret_cast<const char *>(b.data()), b.size(), &p);
  require(p, "Plist decode");
  try {
    auto j = from_plist(p);
    plist_free(p);
    return j;
  } catch (...) {
    plist_free(p);
    throw;
  }
}
void private_write_new(const std::filesystem::path &p, std::string_view data) {
  std::filesystem::create_directories(p.parent_path());
  chmod(p.parent_path().c_str(), 0700);
  auto tmp = p.string() + ".tmp-" + uuid();
  int fd = ::open(tmp.c_str(), O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW, 0600);
  require(fd >= 0, "Cannot create private state");
  try {
    size_t i = 0;
    while (i < data.size()) {
      ssize_t n = ::write(fd, data.data() + i, data.size() - i);
      require(n > 0, "Private state write");
      i += n;
    }
    require(fsync(fd) == 0, "Private state sync");
    ::close(fd);
    fd = -1;
    require(::link(tmp.c_str(), p.c_str()) == 0,
            "State already exists or cannot be installed; preserved existing "
            "state");
    ::unlink(tmp.c_str());
    int dir = ::open(p.parent_path().c_str(), O_RDONLY | O_DIRECTORY);
    if (dir >= 0) {
      fsync(dir);
      ::close(dir);
    }
  } catch (...) {
    if (fd >= 0)
      ::close(fd);
    ::unlink(tmp.c_str());
    throw;
  }
}
std::string private_read(const std::filesystem::path &p) {
  int fd = ::open(p.c_str(), O_RDONLY | O_NOFOLLOW);
  require(fd >= 0, "Cannot open saved state");
  struct stat st {};
  if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || (st.st_mode & 077) != 0 ||
      st.st_size > 16384) {
    ::close(fd);
    throw std::runtime_error(
        "Saved state permissions or size invalid; file preserved");
  }
  std::string s(st.st_size, '\0');
  size_t i = 0;
  while (i < s.size()) {
    ssize_t n = ::read(fd, s.data() + i, s.size() - i);
    if (n <= 0) {
      ::close(fd);
      throw std::runtime_error("Saved state read failed");
    }
    i += n;
  }
  ::close(fd);
  return s;
}
} // namespace lab
