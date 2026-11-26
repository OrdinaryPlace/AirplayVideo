#pragma once
extern "C" {
#include <libavcodec/codec_id.h>
#include <libavutil/error.h>
}

namespace lab {
inline bool recoverable_broadcast_decode_error(AVCodecID codec,int error) {
  if(error==AVERROR_INVALIDDATA)return true;
  if(codec!=AV_CODEC_ID_AC3)return false;
  // FFmpeg 8.0.1's AC-3 decoder returns these parser-specific bad-data
  // errors directly, rather than AVERROR_INVALIDDATA. The header defining
  // them is private: libavcodec/ac3_parser_internal.h, AC3ParseError.
  // Keep the allowlist exact; allocation, codec and other failures stay fatal.
  switch(error) {
  case -0x1030c0a: // sync word
  case -0x2030c0a: // bitstream ID
  case -0x3030c0a: // sample rate
  case -0x4030c0a: // frame size
  case -0x5030c0a: // frame type
  case -0x6030c0a: // CRC
  case -0x7030c0a: // channel map
    return true;
  default:
    return false;
  }
}
}
