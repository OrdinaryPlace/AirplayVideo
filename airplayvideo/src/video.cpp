#include "lab.hpp"
#include <bit>
#include <cstring>
#include <fstream>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/opt.h>
}
namespace lab {
static void avcheck(int n, const char *message) {
  if (n < 0) {
    char error[AV_ERROR_MAX_STRING_SIZE];
    av_strerror(n, error, sizeof(error));
    throw std::runtime_error(std::string(message) + ": " + error);
  }
}
// Original 5x7 block glyphs for the generated slate, not an imported font.
static std::array<uint8_t, 7> glyph(char c) {
  switch (c) {
  case 'H':
    return {17, 17, 17, 31, 17, 17, 17};
  case 'E':
    return {31, 16, 16, 30, 16, 16, 31};
  case 'L':
    return {16, 16, 16, 16, 16, 16, 31};
  case 'O':
    return {14, 17, 17, 17, 17, 17, 14};
  case 'W':
    return {17, 17, 17, 21, 21, 27, 17};
  case 'R':
    return {30, 17, 17, 30, 20, 18, 17};
  case 'D':
    return {30, 17, 17, 17, 17, 17, 30};
  case 'F':
    return {31, 16, 16, 30, 16, 16, 16};
  case 'A':
    return {14, 17, 17, 31, 17, 17, 17};
  case 'M':
    return {17, 27, 21, 21, 17, 17, 17};
  case 'C':
    return {14, 17, 16, 16, 16, 17, 14};
  case 'P':
    return {30, 17, 17, 30, 16, 16, 16};
  case '+':
    return {0, 4, 4, 31, 4, 4, 0};
  case 'X':
    return {17, 17, 10, 4, 10, 17, 17};
  case '0':
    return {14, 17, 19, 21, 25, 17, 14};
  case '1':
    return {4, 12, 4, 4, 4, 4, 14};
  case '2':
    return {14, 17, 1, 2, 4, 8, 31};
  case '3':
    return {30, 1, 1, 14, 1, 1, 30};
  case '4':
    return {2, 6, 10, 18, 31, 2, 2};
  case '5':
    return {31, 16, 16, 30, 1, 1, 30};
  case '6':
    return {14, 16, 16, 30, 17, 17, 14};
  case '7':
    return {31, 1, 2, 4, 8, 8, 8};
  case '8':
    return {14, 17, 17, 14, 17, 17, 14};
  case '9':
    return {14, 17, 17, 15, 1, 1, 14};
  default:
    return {0, 0, 0, 0, 0, 0, 0};
  }
}
struct Video::Impl {
  AVCodecContext *codec = nullptr;
  AVFrame *frame = nullptr;
  AVPacket *packet = nullptr;
  Bytes sps, pps, avcc;
  Impl() {
    auto encoder = avcodec_find_encoder_by_name("libopenh264");
    require(encoder, "FFmpeg OpenH264 encoder missing");
    codec = avcodec_alloc_context3(encoder);
    require(codec, "Codec allocation");
    codec->width = 1920;
    codec->height = 1080;
    codec->pix_fmt = AV_PIX_FMT_YUV420P;
    codec->time_base = {1, 30};
    codec->framerate = {30, 1};
    codec->bit_rate = 4000000;
    codec->gop_size = 60;
    codec->max_b_frames = 0;
    codec->thread_count = 2;
    codec->profile = AV_PROFILE_H264_BASELINE;
    codec->color_range = AVCOL_RANGE_MPEG;
    codec->colorspace = AVCOL_SPC_BT709;
    codec->color_primaries = AVCOL_PRI_BT709;
    codec->color_trc = AVCOL_TRC_BT709;
    avcheck(avcodec_open2(codec, encoder, nullptr), "Open H.264 encoder");
    frame = av_frame_alloc();
    packet = av_packet_alloc();
    require(frame && packet, "Video allocation");
    frame->format = codec->pix_fmt;
    frame->width = codec->width;
    frame->height = codec->height;
    frame->color_range = codec->color_range;
    frame->colorspace = codec->colorspace;
    avcheck(av_frame_get_buffer(frame, 32), "Video buffer");
  }
  ~Impl() {
    av_packet_free(&packet);
    av_frame_free(&frame);
    avcodec_free_context(&codec);
  }
  void rect(int x, int y, int w, int h, uint8_t value) {
    for (int row = std::max(0, y); row < std::min(1080, y + h); ++row) {
      int start = std::max(0, x), end = std::min(1920, x + w);
      if (end > start)
        std::memset(frame->data[0] + row * frame->linesize[0] + start, value,
                    end - start);
    }
  }
  void text(std::string_view value, int x, int y, int scale) {
    for (char c : value) {
      auto g = glyph(c);
      for (int row = 0; row < 7; ++row)
        for (int col = 0; col < 5; ++col)
          if (g[row] & (1 << (4 - col)))
            rect(x + col * scale, y + row * scale, scale, scale, 230);
      x += 6 * scale;
    }
  }
  EncodedFrame encode(int index) {
    avcheck(av_frame_make_writable(frame), "Writable video buffer");
    for (int y = 0; y < 1080; ++y)
      std::memset(frame->data[0] + y * frame->linesize[0], 24 + y / 90, 1920);
    for (int y = 0; y < 540; ++y) {
      std::memset(frame->data[1] + y * frame->linesize[1], 145, 960);
      std::memset(frame->data[2] + y * frame->linesize[2], 120, 960);
    }
    text("HELLO WORLD", 300, 280, 20);
    text("C++ 1920 X 1080", 430, 500, 12);
    text("FRAME " + std::to_string(index), 590, 690, 12);
    rect(180, 880, 1560, 8, 70);
    rect(180 + (index * 12) % 1480, 850, 80, 68, 210);
    // A binary frame marker allows decoded-frame identity checks.
    for (int bit = 0; bit < 16; ++bit)
      rect(560 + bit * 50, 100, 36, 36, (index & (1 << bit)) ? 235 : 16);
    frame->pts = index;
    avcheck(avcodec_send_frame(codec, frame), "Encode frame");
    EncodedFrame out;
    while (true) {
      int result = avcodec_receive_packet(codec, packet);
      if (result == AVERROR(EAGAIN) || result == AVERROR_EOF)
        break;
      avcheck(result, "Read encoded frame");
      out.keyframe |= bool(packet->flags & AV_PKT_FLAG_KEY);
      std::span<const uint8_t> data(packet->data, packet->size);
      std::vector<std::pair<size_t, size_t>> starts;
      for (size_t i = 0; i + 3 < data.size();) {
        if (data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 1) {
          starts.emplace_back(i, i + 3);
          i += 3;
        } else if (i + 4 <= data.size() && data[i] == 0 && data[i + 1] == 0 &&
                   data[i + 2] == 0 && data[i + 3] == 1) {
          starts.emplace_back(i, i + 4);
          i += 4;
        } else
          ++i;
      }
      require(!starts.empty(), "Encoder did not produce Annex B");
      for (size_t i = 0; i < starts.size(); ++i) {
        size_t first = starts[i].second,
               last = i + 1 < starts.size() ? starts[i + 1].first : data.size();
        if (last <= first)
          continue;
        Bytes nal(data.begin() + first, data.begin() + last);
        uint8_t type = nal[0] & 31;
        if (type == 7)
          sps = nal;
        else if (type == 8)
          pps = nal;
        else {
          size_t off = out.payload.size();
          out.payload.resize(off + 4);
          be(out.payload, off, nal.size(), 4);
          out.payload.insert(out.payload.end(), nal.begin(), nal.end());
        }
      }
      av_packet_unref(packet);
    }
    if (avcc.empty() && !sps.empty() && !pps.empty()) {
      require(sps.size() >= 4 && sps.size() < 65536 && pps.size() < 65536,
              "Invalid H.264 parameter sets");
      avcc = {1, sps[1], sps[2], sps[3], 255, 225};
      size_t off = avcc.size();
      avcc.resize(off + 2);
      be(avcc, off, sps.size(), 2);
      avcc.insert(avcc.end(), sps.begin(), sps.end());
      avcc.push_back(1);
      off = avcc.size();
      avcc.resize(off + 2);
      be(avcc, off, pps.size(), 2);
      avcc.insert(avcc.end(), pps.begin(), pps.end());
    }
    require(!avcc.empty() && !out.payload.empty(),
            "Encoder did not emit a complete frame");
    out.avcc = avcc;
    return out;
  }
};
Video::Video() : impl_(std::make_unique<Impl>()) {}
Video::~Video() = default;
EncodedFrame Video::frame(int index) { return impl_->encode(index); }
Bytes mirror_header(uint32_t size, uint8_t type, uint64_t pts, int width, int height) {
  Bytes header(128);
  le(header, 0, size, 4);
  header[4] = type;
  le(header, 6, 6, 2);
  le(header, 8, pts, 8);
  for (size_t o : {16, 40}) {
    le(header, o, std::bit_cast<uint32_t>(float(width)), 4);
    le(header, o + 4, std::bit_cast<uint32_t>(float(height)), 4);
  }
  return header;
}
Bytes mirror_packet(const EncodedFrame &f, std::span<const uint8_t> key,
                    uint64_t nonce, uint64_t pts, int width, int height) {
  require(f.payload.size() < 4 * 1024 * 1024,
          "Encoded frame exceeds demo bound");
  auto header = mirror_header(f.payload.size() + 16, 0, pts, width, height);
  auto cipher = seal(key, nonce, header, f.payload);
  header.insert(header.end(), cipher.begin(), cipher.end());
  return header;
}
void write_sample(const std::filesystem::path &path, int seconds) {
  require(seconds > 0 && seconds <= 60, "Sample duration out of range");
  Video video;
  std::ofstream file(path, std::ios::binary);
  require(bool(file), "Cannot write sample");
  const char prefix[] = {0, 0, 0, 1};
  auto write_nal = [&](std::span<const uint8_t> nal) {
    file.write(prefix, 4);
    file.write(reinterpret_cast<const char *>(nal.data()), nal.size());
  };
  for (int index = 0; index < seconds * 30; ++index) {
    auto f = video.frame(index);
    if (index == 0) {
      size_t pos = 6;
      size_t n = (f.avcc[pos] << 8) | f.avcc[pos + 1];
      pos += 2;
      write_nal(std::span(f.avcc).subspan(pos, n));
      pos += n + 1;
      n = (f.avcc[pos] << 8) | f.avcc[pos + 1];
      pos += 2;
      write_nal(std::span(f.avcc).subspan(pos, n));
    }
    for (size_t pos = 0; pos < f.payload.size();) {
      require(pos + 4 <= f.payload.size(), "AVCC length");
      size_t n = 0;
      for (int i = 0; i < 4; ++i)
        n = (n << 8) | f.payload[pos++];
      require(pos + n <= f.payload.size(), "AVCC NAL");
      write_nal(std::span(f.payload).subspan(pos, n));
      pos += n;
    }
  }
  require(bool(file), "Sample write failed");
}
} // namespace lab
