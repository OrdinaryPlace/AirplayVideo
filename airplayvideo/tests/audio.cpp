#include "stream.hpp"
#include <iostream>
#include <memory>
#include <sodium.h>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/log.h>
}
using namespace lab;

// Decode the actual authenticated RTP payload with an independent decoder.
// A seal/open round trip alone also passes for raw PCM sent to an ALAC receiver.
int main() {
  try {
    require(sodium_init()>=0,"Crypto initialization");
    av_log_set_level(AV_LOG_QUIET);
    auto setup=audio_format_setup();
    require(setup.at("ct")==2&&setup.at("audioFormat")==0x40000&&
            setup.at("sr")==44100&&setup.at("spf")==352,"ALAC negotiation");
    auto codec=avcodec_find_decoder(AV_CODEC_ID_ALAC);
    require(codec,"Independent ALAC decoder is required");
    auto free_context=[](AVCodecContext *p){avcodec_free_context(&p);};
    auto free_packet=[](AVPacket *p){av_packet_free(&p);};
    auto free_frame=[](AVFrame *p){av_frame_free(&p);};
    std::unique_ptr<AVCodecContext,decltype(free_context)> context(avcodec_alloc_context3(codec),free_context);
    std::unique_ptr<AVPacket,decltype(free_packet)> packet(av_packet_alloc(),free_packet);
    std::unique_ptr<AVFrame,decltype(free_frame)> frame(av_frame_alloc(),free_frame);
    require(context&&packet&&frame,"Decoder allocation");
    // Standard ALACSpecificConfig atom describing the receiver's negotiated
    // 352-frame, 44.1 kHz, stereo 16-bit decoder. It is not sent in each RTP frame.
    Bytes config(36);be(config,0,36,4);
    std::copy_n("alac",4,config.begin()+4);
    be(config,12,352,4);config[17]=16;config[18]=40;config[19]=10;config[20]=14;config[21]=2;
    be(config,22,255,2);be(config,32,44100,4);
    context->extradata=static_cast<uint8_t*>(av_mallocz(config.size()+AV_INPUT_BUFFER_PADDING_SIZE));
    require(context->extradata,"Decoder configuration allocation");
    context->extradata_size=config.size();std::copy(config.begin(),config.end(),context->extradata);
    require(avcodec_open2(context.get(),codec,nullptr)>=0,"Open independent decoder");
    auto key=random_bytes(32);
    for(unsigned trial=0;trial<12;++trial) {
      Bytes pcm(352*4);
      for(size_t i=0;i<pcm.size()/2;++i) {
        uint16_t sample=trial==0?0:trial==1?(i%2?0x8000:0x7fff):uint16_t((i*19391+trial*11213)^0xa55a);
        be(pcm,i*2,sample,2);
      }
      const uint64_t nonce=trial==11?0x100000005ULL:trial;
      auto wire=audio_packet(pcm,key,nonce,uint16_t(65530+trial),0xffffffe0U+trial*352,0x10203040,trial==0);
      require(wire.size()+28<=1500,"IPv4 UDP packet must fit ordinary Ethernet MTU");
      require(read_le(std::span(wire).last(8))==nonce,"64-bit nonce trailer");
      auto payload=open_sealed(key,nonce,std::span(wire).subspan(4,8),std::span(wire).subspan(12,wire.size()-20));
      require(payload!=pcm,"Wire audio must be ALAC framed, not raw PCM");
      require(av_new_packet(packet.get(),payload.size())>=0,"Packet allocation");
      std::copy(payload.begin(),payload.end(),packet->data);
      require(avcodec_send_packet(context.get(),packet.get())>=0&&
              avcodec_receive_frame(context.get(),frame.get())>=0,"Receiver can decode the transmitted payload");
      require(frame->nb_samples==352&&frame->ch_layout.nb_channels==2&&
              frame->format==AV_SAMPLE_FMT_S16P,"Decoded audio geometry");
      for(size_t i=0;i<352;++i) for(size_t channel=0;channel<2;++channel) {
        const size_t offset=(i*2+channel)*2;
        const int16_t expected=int16_t(uint16_t(pcm[offset])<<8|pcm[offset+1]);
        require(reinterpret_cast<int16_t*>(frame->extended_data[channel])[i]==expected,
                "Every decoded stereo sample must match, including sign and channel order");
      }
      av_frame_unref(frame.get());av_packet_unref(packet.get());
    }
    bool rejected=false;
    try {alac_frame(Bytes(1407));} catch(...) {rejected=true;}
    require(rejected,"Reject partial audio packet");
    std::cout<<"Encrypted ALAC packets decode to exact stereo PCM across sequence and nonce boundaries\n";
    return 0;
  } catch(const std::exception &e) {std::cerr<<e.what()<<'\n';return 1;}
}
