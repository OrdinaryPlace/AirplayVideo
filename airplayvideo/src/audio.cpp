#include "stream.hpp"

namespace lab {
Bytes alac_frame(std::span<const uint8_t> pcm) {
  require(pcm.size()==352*4,"Audio packet must contain 352 stereo samples");
  // An ALAC escape frame carries the original signed 16-bit samples. The
  // stereo element header is 23 bits, so PCM bytes cannot just be prepended
  // with a byte-aligned header. No predictor, lookahead or extra queue is used.
  Bytes frame((23+pcm.size()*8+3+7)/8);
  size_t bit=0;
  auto put=[&](uint32_t value,unsigned width) {
    for(unsigned i=width;i>0;--i,++bit)
      frame[bit/8]|=((value>>(i-1))&1)<<(7-bit%8);
  };
  put(1,3); // channel pair element
  put(0,4); // element instance
  put(0,12); // reserved
  put(0,1); // full frame: 352 samples, as negotiated in SETUP
  put(0,2); // no shifted low bytes
  put(1,1); // escape: uncompressed, lossless samples
  for(auto byte:pcm) put(byte,8); // interleaved, big-endian L16/R16
  put(7,3); // end element; remaining bits are zero padding
  return frame;
}
Json audio_format_setup() {
  return {{"ct",2},{"audioFormat",0x40000},{"sr",44100},{"spf",352}};
}
}
