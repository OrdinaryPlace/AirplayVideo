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
Json audio_stream_setup(uint64_t features, std::span<const uint8_t> key,
                        uint64_t stream_id, uint16_t control_port, int lead_ms) {
  require(key.size()==32&&control_port!=0,"Invalid encrypted audio transport");
  Json stream={{"type",96},{"audioMode","default"},
    {"shk",Json::binary(Bytes(key.begin(),key.end()))},
    {"streamConnectionID",stream_id},{"supportsDynamicStreamID",false}};
  stream.update(audio_format_setup());
  stream.update(audio_timing_setup(lead_ms));
  if(features&(uint64_t(1)<<59)) {
    // A modern RTP connection must explicitly opt into the key supplied by
    // this stream. Merely including shk in a legacy descriptor is not enough.
    stream["streamConnections"]={
      {"streamConnectionTypeRTP",{{"streamConnectionKeyUseStreamEncryptionKey",true}}},
      {"streamConnectionTypeRTCP",{{"streamConnectionKeyPort",control_port}}}};
  } else stream["controlPort"]=control_port;
  return stream;
}
std::pair<uint16_t,uint16_t> audio_stream_ports(const Json &stream) {
  auto port=[](const Json &object,const char *name)->uint16_t {
    require(object.is_object(),"Invalid audio connection descriptor");
    if(!object.contains(name)) return 0;
    const auto &value=object.at(name);
    require(value.is_number_integer()&&value>0&&value<=65535,"Invalid audio transport port");
    return value.get<uint16_t>();
  };
  uint16_t data=0,control=0;
  if(stream.contains("streamConnections")) {
    const auto &connections=stream.at("streamConnections");
    require(connections.is_object(),"Invalid audio connections");
    if(connections.contains("streamConnectionTypeRTP"))
      data=port(connections.at("streamConnectionTypeRTP"),"streamConnectionKeyPort");
    if(connections.contains("streamConnectionTypeRTCP"))
      control=port(connections.at("streamConnectionTypeRTCP"),"streamConnectionKeyPort");
  }
  if(!data) data=port(stream,"dataPort");
  if(!control) control=port(stream,"controlPort");
  require(data&&control,"Receiver did not offer ALAC audio transport");
  return {data,control};
}
}
