#include "stream.hpp"
#include <iostream>
#include <memory>
#include <openssl/evp.h>
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
    auto key=random_bytes(32);
    for(uint64_t features:{uint64_t(1)<<19,(uint64_t(1)<<19)|(uint64_t(1)<<59)}) {
      // Exercise the serialized SETUP seen by a receiver, including its choice
      // of encryption key. A local cipher round trip cannot catch a missing
      // use-stream-key flag in the negotiated RTP connection.
      auto request=plist_decode(plist_encode(audio_stream_setup(features,key,42,18201,500)));
      require(request.at("usingScreen")==true&&request.at("isMedia")==false,
              "Keep audio on the screen output path");
      require(request.at("latencyMin")==0&&request.at("latencyMax")==22050,
              "Connection negotiation must preserve the common presentation lead");
      if(features&(uint64_t(1)<<59)) {
        require(!request.contains("controlPort"),"Do not mix legacy and modern connections");
        const auto &connections=request.at("streamConnections");
        require(connections.at("streamConnectionTypeRTP").at("streamConnectionKeyUseStreamEncryptionKey")==true,
                "Receiver must select the declared stream encryption key");
        require(connections.at("streamConnectionTypeRTCP").at("streamConnectionKeyPort")==18201,
                "Receiver can return sync and retransmission requests to the bound socket");
      } else require(request.at("controlPort")==18201&&!request.contains("streamConnections"),
                     "Retain legacy receivers without feature 59");
      auto negotiated=request.at("shk").get_binary();
      Bytes original(352*4,37);
      auto wire=audio_packet(original,key,0x100000005ULL,123,456,0,true);
      require(wire[1]==0x60&&std::all_of(wire.begin()+8,wire.begin()+12,[](uint8_t b){return b==0;}),
              "First screen audio packet uses PT 96 without a marker and fixed zero SSRC");
      // AirPlay uses RFC 7539 authentication with a 64-bit nonce, equivalent
      // to a zero-prefixed IETF nonce for these bounded packets. Sodium's
      // pre-RFC crypto_aead_chacha20poly1305 has a different MAC construction.
      Bytes iv(12),decoded(wire.size());
      std::copy(wire.end()-8,wire.end(),iv.begin()+4);
      std::unique_ptr<EVP_CIPHER_CTX,decltype(&EVP_CIPHER_CTX_free)>
        cipher(EVP_CIPHER_CTX_new(),EVP_CIPHER_CTX_free);
      int length=0,total=0;
      const size_t payload_size=wire.size()-12-16-8;
      require(cipher&&EVP_DecryptInit_ex(cipher.get(),EVP_chacha20_poly1305(),nullptr,negotiated.data(),iv.data())==1&&
              EVP_DecryptUpdate(cipher.get(),nullptr,&length,wire.data()+4,8)==1&&
              EVP_DecryptUpdate(cipher.get(),decoded.data(),&length,wire.data()+12,payload_size)==1,
              "Independent receiver decrypts the negotiated stream");
      total=length;
      require(EVP_CIPHER_CTX_ctrl(cipher.get(),EVP_CTRL_AEAD_SET_TAG,16,wire.data()+12+payload_size)==1&&
              EVP_DecryptFinal_ex(cipher.get(),decoded.data()+total,&length)==1,
              "Independent receiver authenticates the negotiated key and RTP header");
      decoded.resize(total+length);require(decoded==alac_frame(original),"Negotiated payload is intact");
    }
    const Json legacy={{"type",96},{"dataPort",6000},{"controlPort",6001}};
    Json modern={{"type",96},{"streamConnections",{
      {"streamConnectionTypeRTP",{{"streamConnectionKeyPort",7000}}},
      {"streamConnectionTypeRTCP",{{"streamConnectionKeyPort",7001}}}}}};
    require(audio_stream_ports(plist_decode(plist_encode(legacy)))==std::pair<uint16_t,uint16_t>{6000,6001},
            "Legacy receiver endpoint response");
    require(audio_stream_ports(plist_decode(plist_encode(modern)))==std::pair<uint16_t,uint16_t>{7000,7001},
            "Modern receiver endpoint response without legacy fields");
    modern["dataPort"]=6000;modern["controlPort"]=6001;
    require(audio_stream_ports(modern)==std::pair<uint16_t,uint16_t>{7000,7001},
            "Use negotiated connection endpoints when both forms are present");
    for(const Json &bad:std::vector<Json>{0,-1,65536,"7000",nullptr,true}) {
      auto response=modern;
      response["streamConnections"]["streamConnectionTypeRTP"]["streamConnectionKeyPort"]=bad;
      bool rejected=false;try{audio_stream_ports(response);}catch(...){rejected=true;}
      require(rejected,"Malformed modern endpoint must not silently downgrade to legacy");
    }
    bool missing=false;try{audio_stream_ports(Json{{"type",96},{"dataPort",7000}});}catch(...){missing=true;}
    require(missing,"Both data and control endpoints are required");
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
