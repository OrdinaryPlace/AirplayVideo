// Measure content timing, not just equality of timestamp fields.
// No receiver, profile, LAN connection or household media is used by this test.
#include "stream.hpp"
#include <cmath>
#include <cstring>
#include <iostream>
#include <openssl/evp.h>
#include <sodium.h>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavdevice/avdevice.h>
#include <libavformat/avformat.h>
}
using namespace lab;
namespace {
constexpr AVRational micros{1,1000000};
constexpr int width=1920,height=1080,fps=30,seconds=7;
constexpr long double fixed_scale=4294967296.0L;
void check(int value,const char *message) { require(value>=0,message); }
struct CodecDelete { void operator()(AVCodecContext *p) const {avcodec_free_context(&p);} };
struct FrameDelete { void operator()(AVFrame *p) const {av_frame_free(&p);} };
struct PacketDelete { void operator()(AVPacket *p) const {av_packet_free(&p);} };
using Codec=std::unique_ptr<AVCodecContext,CodecDelete>;
using Frame=std::unique_ptr<AVFrame,FrameDelete>;
using Packet=std::unique_ptr<AVPacket,PacketDelete>;

// The independent input has 100 ms white/1 kHz pulses at exact integer seconds.
// PCM avoids codec priming in the reference; the app still resamples 48->44.1 kHz.
void make_fixture(const std::filesystem::path &path,int audio_shift_ms=0) {
  AVFormatContext *raw=nullptr;
  check(avformat_alloc_output_context2(&raw,nullptr,"matroska",path.c_str()),"Fixture muxer");
  auto release=[](AVFormatContext *p){if(p){if(p->pb)avio_closep(&p->pb);avformat_free_context(p);}};
  std::unique_ptr<AVFormatContext,decltype(release)> out(raw,release);
  const auto *encoder=avcodec_find_encoder_by_name("libopenh264");require(encoder,"Fixture encoder");
  Codec codec(avcodec_alloc_context3(encoder));require(bool(codec),"Fixture codec allocation");
  codec->width=width;codec->height=height;codec->pix_fmt=AV_PIX_FMT_YUV420P;
  codec->time_base={1,fps};codec->framerate={fps,1};codec->bit_rate=2000000;
  codec->gop_size=fps;codec->max_b_frames=0;codec->thread_count=2;
  codec->flags|=AV_CODEC_FLAG_GLOBAL_HEADER;
  check(avcodec_open2(codec.get(),encoder,nullptr),"Open fixture encoder");
  auto *video=avformat_new_stream(out.get(),nullptr),*audio=avformat_new_stream(out.get(),nullptr);
  require(video&&audio,"Fixture streams");video->time_base=codec->time_base;audio->time_base={1,48000};
  check(avcodec_parameters_from_context(video->codecpar,codec.get()),"Fixture video format");
  auto *ap=audio->codecpar;ap->codec_type=AVMEDIA_TYPE_AUDIO;ap->codec_id=AV_CODEC_ID_PCM_S16LE;
  ap->format=AV_SAMPLE_FMT_S16;ap->sample_rate=48000;ap->bits_per_coded_sample=16;
  ap->block_align=4;av_channel_layout_default(&ap->ch_layout,2);
  check(avio_open(&out->pb,path.c_str(),AVIO_FLAG_WRITE),"Fixture file");
  check(avformat_write_header(out.get(),nullptr),"Fixture header");
  Frame frame(av_frame_alloc());Packet packet(av_packet_alloc());require(frame&&packet,"Fixture frame allocation");
  frame->format=codec->pix_fmt;frame->width=width;frame->height=height;
  check(av_frame_get_buffer(frame.get(),32),"Fixture frame storage");
  auto drain=[&] {
    for(;;) {
      int status=avcodec_receive_packet(codec.get(),packet.get());
      if(status==AVERROR(EAGAIN)||status==AVERROR_EOF)break;
      check(status,"Fixture encoded frame");
      av_packet_rescale_ts(packet.get(),codec->time_base,video->time_base);packet->stream_index=video->index;
      check(av_interleaved_write_frame(out.get(),packet.get()),"Fixture video write");
    }
  };
  for(int tick=0;tick<seconds*fps;++tick) {
    check(av_frame_make_writable(frame.get()),"Fixture writable frame");
    const bool bright=tick>=fps&&tick%fps<3;
    for(int y=0;y<height;++y)std::memset(frame->data[0]+y*frame->linesize[0],bright?235:16,width);
    for(int plane=1;plane<3;++plane)for(int y=0;y<height/2;++y)
      std::memset(frame->data[plane]+y*frame->linesize[plane],128,width/2);
    frame->pts=tick;check(avcodec_send_frame(codec.get(),frame.get()),"Fixture encode");drain();
    check(av_new_packet(packet.get(),1600*4),"Fixture audio storage");
    for(int sample=0;sample<1600;++sample) {
      const int position=tick*1600+sample-audio_shift_ms*48;
      int16_t value=position>=48000&&position%48000<4800?
        int16_t(12000*std::sin(2*3.14159265358979323846*1000*position/48000.0)):0;
      for(int channel=0;channel<2;++channel) {
        auto *p=packet->data+(sample*2+channel)*2;p[0]=uint16_t(value)&255;p[1]=uint16_t(value)>>8;
      }
    }
    packet->pts=packet->dts=av_rescale_q(int64_t(tick)*1600,{1,48000},audio->time_base);
    packet->duration=av_rescale_q(1600,{1,48000},audio->time_base);packet->stream_index=audio->index;
    check(av_interleaved_write_frame(out.get(),packet.get()),"Fixture audio write");
  }
  check(avcodec_send_frame(codec.get(),nullptr),"Fixture flush");drain();
  check(av_write_trailer(out.get()),"Fixture trailer");
}

struct Meter {
  std::vector<double> flashes,beeps;
  bool white=false;
  double last_tone=-10;
  void video(AVFrame *f,double time) {
    require(f->format==AV_PIX_FMT_YUV420P&&f->width==width&&f->height==height,"Decoded 1080p picture");
    int total=0,count=0;
    for(int y=0;y<height;y+=32)for(int x=0;x<width;x+=32){total+=f->data[0][y*f->linesize[0]+x];++count;}
    bool bright=total/count>180;if(bright&&!white)flashes.push_back(time);white=bright;
  }
  void audio(AVFrame *f,double time) {
    require(f->format==AV_SAMPLE_FMT_S16||f->format==AV_SAMPLE_FMT_S16P,"Decoded 16-bit audio");
    const auto *samples=reinterpret_cast<const int16_t*>(f->extended_data[0]);
    const int stride=f->format==AV_SAMPLE_FMT_S16P?1:f->ch_layout.nb_channels;
    for(int i=0;i<f->nb_samples;++i)if(std::abs(int(samples[i*stride]))>3000) {
      double now=time+double(i)/f->sample_rate;
      if(now-last_tone>0.25)beeps.push_back(now);last_tone=now;
    }
  }
  Json report(const std::string &stage) const {
    require(flashes.size()>=5&&beeps.size()>=5,"At least five measured flash/beep events");
    std::vector<double> offsets;
    for(double flash:flashes) {
      auto closest=std::min_element(beeps.begin(),beeps.end(),[&](double a,double b){return std::abs(a-flash)<std::abs(b-flash);});
      if(std::abs(*closest-flash)<0.4)offsets.push_back((*closest-flash)*1000);
    }
    require(offsets.size()>=5,"Five matched content events");auto sorted=offsets;std::sort(sorted.begin(),sorted.end());
    return {{"stage",stage},{"matched_events",offsets.size()},{"audio_minus_video_ms",{
      {"min",sorted.front()},{"median",sorted[sorted.size()/2]},{"max",sorted.back()}}},{"offsets_ms",offsets}};
  }
};
class Decoder {
  Codec codec_;
  Frame frame_{av_frame_alloc()};
public:
  Decoder(AVCodecID id,const Bytes &extra):codec_(avcodec_alloc_context3(avcodec_find_decoder(id))) {
    require(avcodec_find_decoder(id),"Measurement requires the current image with H.264 and ALAC decoders");
    require(codec_&&frame_,"Measurement decoder allocation");codec_->thread_count=1;codec_->pkt_timebase=micros;
    if(!extra.empty()) {
      codec_->extradata=static_cast<uint8_t*>(av_mallocz(extra.size()+AV_INPUT_BUFFER_PADDING_SIZE));
      require(codec_->extradata,"Measurement decoder configuration");
      codec_->extradata_size=extra.size();std::copy(extra.begin(),extra.end(),codec_->extradata);
    }
    check(avcodec_open2(codec_.get(),avcodec_find_decoder(id),nullptr),"Measurement decoder open");
  }
  void consume(const Bytes &data,double time,Meter &meter,bool audio) {
    Packet packet(av_packet_alloc());require(bool(packet),"Measurement packet");
    check(av_new_packet(packet.get(),data.size()),"Measurement packet bytes");
    std::copy(data.begin(),data.end(),packet->data);packet->pts=packet->dts=std::llround(time*1000000);
    check(avcodec_send_packet(codec_.get(),packet.get()),"Measurement decoder input");
    for(;;) {
      int status=avcodec_receive_frame(codec_.get(),frame_.get());
      if(status==AVERROR(EAGAIN)||status==AVERROR_EOF)break;
      check(status,"Measurement decoder output");double pts=frame_->best_effort_timestamp/1000000.0;
      if(audio)meter.audio(frame_.get(),pts);else meter.video(frame_.get(),pts);
      av_frame_unref(frame_.get());
    }
  }
};

Json reference(const std::filesystem::path &path,double expected_ms) {
  AVFormatContext *raw=nullptr;check(avformat_open_input(&raw,path.c_str(),nullptr,nullptr),"Reference open");
  auto close=[](AVFormatContext *p){avformat_close_input(&p);};
  std::unique_ptr<AVFormatContext,decltype(close)> input(raw,close);
  check(avformat_find_stream_info(input.get(),nullptr),"Reference formats");
  std::vector<Codec> codecs;Meter meter;
  for(unsigned i=0;i<input->nb_streams;++i) {
    auto *format=input->streams[i]->codecpar;auto *decoder=avcodec_find_decoder(format->codec_id);
    require(decoder,"Reference decoder");Codec c(avcodec_alloc_context3(decoder));require(bool(c),"Reference allocation");
    check(avcodec_parameters_to_context(c.get(),format),"Reference parameters");c->thread_count=1;
    check(avcodec_open2(c.get(),decoder,nullptr),"Reference decoder open");codecs.push_back(std::move(c));
  }
  Packet packet(av_packet_alloc());Frame frame(av_frame_alloc());require(packet&&frame,"Reference frame storage");
  while(av_read_frame(input.get(),packet.get())>=0) {
    const auto index=packet->stream_index;auto *codec=codecs.at(index).get();
    check(avcodec_send_packet(codec,packet.get()),"Reference decode");av_packet_unref(packet.get());
    for(;;) {
      int status=avcodec_receive_frame(codec,frame.get());if(status==AVERROR(EAGAIN)||status==AVERROR_EOF)break;
      check(status,"Reference frame");double time=frame->best_effort_timestamp*av_q2d(input->streams[index]->time_base);
      if(codec->codec_type==AVMEDIA_TYPE_AUDIO)meter.audio(frame.get(),time);else meter.video(frame.get(),time);
      av_frame_unref(frame.get());
    }
  }
  auto result=meter.report("decoded_reference");std::cout<<result.dump()<<'\n';
  require(std::abs(result.at("audio_minus_video_ms").at("min").get<double>()-expected_ms)<0.1&&
          std::abs(result.at("audio_minus_video_ms").at("max").get<double>()-expected_ms)<0.1,
          "Independently decoded reference must measure the intended offset within 0.1 ms");
  return result;
}

// Parse independently of the sender's integer and clock helpers.
uint64_t integer(std::span<const uint8_t> b,bool little=false) {
  uint64_t value=0;
  if(little)for(auto i=b.rbegin();i!=b.rend();++i)value=value*256+*i;
  else for(auto byte:b)value=value*256+byte;
  return value;
}
Bytes decrypt(const Bytes &key,std::span<const uint8_t> nonce,std::span<const uint8_t> aad,std::span<const uint8_t> ciphertext) {
  require(nonce.size()==8&&ciphertext.size()>=16,"Receiver cipher framing");
  Bytes iv(12),plain(ciphertext.size()-16);std::copy(nonce.begin(),nonce.end(),iv.begin()+4);
  std::unique_ptr<EVP_CIPHER_CTX,decltype(&EVP_CIPHER_CTX_free)> cipher(EVP_CIPHER_CTX_new(),EVP_CIPHER_CTX_free);
  int size=0,total=0;
  require(cipher&&EVP_DecryptInit_ex(cipher.get(),EVP_chacha20_poly1305(),nullptr,key.data(),iv.data())==1&&
    EVP_DecryptUpdate(cipher.get(),nullptr,&size,aad.data(),aad.size())==1&&
    EVP_DecryptUpdate(cipher.get(),plain.data(),&size,ciphertext.data(),plain.size())==1,"Receiver decryption");
  total=size;
  require(EVP_CIPHER_CTX_ctrl(cipher.get(),EVP_CTRL_AEAD_SET_TAG,16,const_cast<uint8_t*>(ciphertext.data()+plain.size()))==1&&
    EVP_DecryptFinal_ex(cipher.get(),plain.data()+total,&size)==1,"Receiver authentication");
  plain.resize(total+size);return plain;
}
Bytes alac_config() {
  // Standard 352-sample, 44.1 kHz stereo ALACSpecificConfig, not sender code.
  return {0,0,0,36,'a','l','a','c',0,0,0,0,0,0,1,96,0,16,40,10,14,2,0,255,
          0,0,0,0,0,0,0,0,0,0,172,68};
}
Json measure(const std::filesystem::path &path,int lead,double expected_ms) {
  Json config={{"source",{{"kind","fixture"},{"url",path.string()}}},{"width",width},{"height",height},
    {"fps",fps},{"bitrate",4000000},{"encoder","libopenh264"},{"audio",true},{"deinterlace",true},{"latency_ms",lead}};
  std::atomic<bool> stop{false};
  Media media(config,stop,[](const auto &name,const auto &detail){if(name=="source_error")std::cerr<<detail.at("message")<<'\n';});
  auto sink=media.subscribe();media.start();
  Meter capture,wire;std::unique_ptr<Decoder> captured_video,wire_video;Decoder audio(AV_CODEC_ID_ALAC,alac_config());
  auto key=random_bytes(32);uint64_t video_nonce=0,audio_nonce=0;uint16_t sequence=65500;
  const uint32_t rtp_origin=0xffff0000U;Bytes sync;int64_t last_sync=-1000000;double max_schedule_error=0;
  const auto deadline=Clock::now()+std::chrono::seconds(12);
  while(Clock::now()<deadline&&capture.flashes.size()<6) {
    auto p=sink->next(stop);
    if(!p){require(!media.failed(),"Fixture source failed");continue;}
    if(p->audio) {
      // The capture tap is PCM before ALAC packetization; use its actual samples.
      Frame raw(av_frame_alloc());require(bool(raw),"Capture audio frame");
      raw->format=AV_SAMPLE_FMT_S16;raw->sample_rate=44100;raw->nb_samples=352;
      av_channel_layout_default(&raw->ch_layout,2);check(av_frame_get_buffer(raw.get(),0),"Capture audio storage");
      auto *samples=reinterpret_cast<int16_t*>(raw->data[0]);
      for(size_t i=0;i<704;++i)samples[i]=int16_t(uint16_t(p->pcm[i*2])<<8|p->pcm[i*2+1]);
      capture.audio(raw.get(),p->pts_us/1000000.0);
      const uint32_t rtp=rtp_origin+uint32_t(uint64_t(p->pts_us)*44100/1000000);
      if(p->pts_us-last_sync>=1000000) {
        sync=audio_sync_packet(media.epoch_ntp,p->pts_us,rtp,lead,last_sync<0);last_sync=p->pts_us;
      }
      auto packet=audio_packet(p->pcm,key,audio_nonce++,sequence++,rtp,0,false);
      const uint32_t sample=integer(std::span(packet).subspan(4,4));
      const uint32_t reference=integer(std::span(sync).subspan(4,4));
      const int32_t distance=int32_t(sample-reference);
      // NTP control/audio epoch differs from raw screen-header seconds.32.
      const uint64_t sync_clock=integer(std::span(sync).subspan(8,8))-(2208988800ULL<<32);
      const long double relative=static_cast<int64_t>(sync_clock-media.epoch_ntp)/fixed_scale;
      const double time=double(relative+distance/44100.0L);
      max_schedule_error=std::max(max_schedule_error,std::abs(time-p->pts_us/1000000.0)*1000000);
      auto payload=decrypt(key,std::span(packet).last(8),std::span(packet).subspan(4,8),std::span(packet).subspan(12,packet.size()-20));
      audio.consume(payload,time,wire,true);
    } else {
      if(!captured_video) {
        captured_video=std::make_unique<Decoder>(AV_CODEC_ID_H264,p->video.avcc);
        wire_video=std::make_unique<Decoder>(AV_CODEC_ID_H264,p->video.avcc);
      }
      captured_video->consume(p->video.payload,p->pts_us/1000000.0,capture,false);
      auto packet=mirror_packet(p->video,key,video_nonce,media.epoch_ntp+ntp_delta(p->pts_us),width,height);
      Bytes nonce(8);for(size_t i=0;i<8;++i)nonce[i]=uint8_t(video_nonce>>(i*8));++video_nonce;
      const uint64_t presentation=integer(std::span(packet).subspan(8,8),true);
      const double time=double(static_cast<int64_t>(presentation-media.epoch_ntp)/fixed_scale);
      max_schedule_error=std::max(max_schedule_error,std::abs(time-p->pts_us/1000000.0)*1000000);
      auto payload=decrypt(key,nonce,std::span(packet).first(128),std::span(packet).subspan(128));
      wire_video->consume(payload,time,wire,false);
    }
  }
  stop=true;media.close();sink->close();
  auto before=capture.report("capture"),after=wire.report("decoded_airplay_packets");
  Json result={{"buffer_ms",lead},{"reference_audio_minus_video_ms",expected_ms},{"capture",before},{"wire",after},
    {"maximum_schedule_error_us",max_schedule_error}};
  std::cout<<result.dump()<<'\n';
  for(const auto &report:{before,after}) {
    const auto &offset=report.at("audio_minus_video_ms");
    require(std::abs(offset.at("min").get<double>()-expected_ms)<2&&std::abs(offset.at("max").get<double>()-expected_ms)<2,
      "Measured content offset must stay within 2 ms of the independent reference");
  }
  require(max_schedule_error<24,"Packetization must preserve PTS within one audio sample");
  return result;
}
}
int main() {
  const auto path=std::filesystem::temp_directory_path()/("airplayvideo-sync-"+uuid()+".mkv");
  try {
    require(sodium_init()>=0,"Crypto initialization");avdevice_register_all();av_log_set_level(AV_LOG_ERROR);
    make_fixture(path);
    reference(path,0);
    for(int lead:{500,1500})measure(path,lead,0);
    // Measurement control: move the waveform without changing its timestamps.
    // Equal track endpoints must not hide 75 ms of incorrect source content.
    make_fixture(path,75);reference(path,75);measure(path,500,75);
    std::filesystem::remove(path);return 0;
  } catch(const std::exception &e) {
    std::filesystem::remove(path);std::cerr<<e.what()<<'\n';return 1;
  }
}
