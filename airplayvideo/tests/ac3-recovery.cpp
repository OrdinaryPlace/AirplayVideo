// Real FFmpeg AC-3 parser errors and bounded broadcast/camera recovery.
#include "broadcast_decode.hpp"
#include "camera_hls.hpp"
#include <cmath>
#include <iostream>
#include <sys/stat.h>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavdevice/avdevice.h>
#include <libavformat/avformat.h>
}
using namespace lab;
namespace {
struct Input {
  AVFormatContext *value=nullptr;
  explicit Input(const std::filesystem::path &path) {
    require(avformat_open_input(&value,path.c_str(),nullptr,nullptr)>=0,"AC-3 fixture opens");
    require(avformat_find_stream_info(value,nullptr)>=0,"AC-3 fixture formats are readable");
  }
  ~Input(){avformat_close_input(&value);}
};
struct Packet {
  AVPacket *value=av_packet_alloc();
  Packet(){require(value,"AC-3 test packet allocation");}
  ~Packet(){av_packet_free(&value);}
};
struct Decoder {
  AVCodecContext *value=nullptr;
  AVFrame *frame=av_frame_alloc();
  explicit Decoder(const AVCodecParameters *parameters,AVRational time_base) {
    require(frame,"AC-3 test frame allocation");
    value=avcodec_alloc_context3(avcodec_find_decoder(parameters->codec_id));
    require(value&&avcodec_parameters_to_context(value,parameters)>=0,"AC-3 decoder allocation");
    value->pkt_timebase=time_base;
    require(avcodec_open2(value,value->codec,nullptr)>=0,"AC-3 decoder opens");
  }
  ~Decoder(){av_frame_free(&frame);avcodec_free_context(&value);}
};
void allowlist() {
  for(int code:{-0x1030c0a,-0x2030c0a,-0x3030c0a,-0x4030c0a,-0x5030c0a,-0x6030c0a,-0x7030c0a}) {
    require(recoverable_broadcast_decode_error(AV_CODEC_ID_AC3,code),"Known AC-3 bad-data error is recoverable");
    require(!recoverable_broadcast_decode_error(AV_CODEC_ID_H264,code)&&
            !recoverable_broadcast_decode_error(AV_CODEC_ID_AAC,code),"AC-3 errors do not broaden other decoders' policy");
  }
  for(int code:{0,AVERROR(EAGAIN),AVERROR(ENOMEM),AVERROR(EINVAL),AVERROR_EOF,AVERROR_DECODER_NOT_FOUND,-0x8030c0a})
    require(!recoverable_broadcast_decode_error(AV_CODEC_ID_AC3,code),"Other decoder and resource failures remain fatal");
  require(recoverable_broadcast_decode_error(AV_CODEC_ID_H264,AVERROR_INVALIDDATA),"Existing broadcast invalid-data recovery stays intact");
  auto *parser=av_parser_init(AV_CODEC_ID_AC3);require(parser,"The installed AC-3 packet parser is present");av_parser_close(parser);
}
void decoder_recovers(const std::filesystem::path &fixture) {
  Input input(fixture);Packet packet;
  const int audio=av_find_best_stream(input.value,AVMEDIA_TYPE_AUDIO,-1,-1,nullptr,0);
  require(audio>=0&&input.value->streams[audio]->codecpar->codec_id==AV_CODEC_ID_AC3,"Fixture supplies real AC-3");
  while(av_read_frame(input.value,packet.value)>=0) {
    if(packet.value->stream_index==audio&&packet.value->size>=7&&packet.value->data[0]==0x0b&&packet.value->data[1]==0x77)break;
    av_packet_unref(packet.value);
  }
  require(packet.value->size>=7,"Fixture contains a complete AC-3 frame");
  Decoder decoder(input.value->streams[audio]->codecpar,input.value->streams[audio]->time_base);
  Packet bad;require(av_packet_ref(bad.value,packet.value)>=0&&av_packet_make_writable(bad.value)>=0,"Trailing-header packet copy");
  const int valid_size=bad.value->size;
  require(av_grow_packet(bad.value,32)>=0,"Trailing-header packet allocation");
  std::fill(bad.value->data+valid_size,bad.value->data+bad.value->size,0);
  // A corrupt FIRST header is normalized to AVERROR_INVALIDDATA. The reported
  // raw error comes from inspecting >16 trailing bytes after a valid frame.
  // Keep that exact distinction in the real decoder regression.
  int error=avcodec_send_packet(decoder.value,bad.value);
  if(error>=0)error=avcodec_receive_frame(decoder.value,decoder.frame);
  require(error==-0x1030c0a,"Real AC-3 decoder returns the reported -16976906 sync error");
  require(avcodec_send_packet(decoder.value,packet.value)>=0,"The same decoder accepts following valid AC-3 without a reset");
  require(avcodec_receive_frame(decoder.value,decoder.frame)>=0&&decoder.frame->nb_samples==1536,
          "The same decoder produces all following valid AC-3 samples");
  require(decoder.frame->format==AV_SAMPLE_FMT_FLTP,"Pinned AC-3 decoder returns planar float samples");
  float peak=0;const auto *samples=reinterpret_cast<const float*>(decoder.frame->extended_data[0]);
  for(int i=0;i<decoder.frame->nb_samples;++i)peak=std::max(peak,std::abs(samples[i]));
  require(peak>0.001,"Recovered AC-3 frame contains the real synthetic tone");
}
void corrupt_audio_packets(const std::filesystem::path &fixture,const std::filesystem::path &path,bool permanent) {
  Input input(fixture);AVFormatContext *output=nullptr;bool header=false;
  try {
    require(avformat_alloc_output_context2(&output,nullptr,"matroska",path.c_str())>=0&&output,"Packet-preserving fixture allocation");
    for(unsigned i=0;i<input.value->nb_streams;++i) {
      auto *stream=avformat_new_stream(output,nullptr);require(stream,"Fixture output stream allocation");
      require(avcodec_parameters_copy(stream->codecpar,input.value->streams[i]->codecpar)>=0,"Fixture retains source codecs");
      stream->codecpar->codec_tag=0;stream->time_base=input.value->streams[i]->time_base;
    }
    require(avio_open(&output->pb,path.c_str(),AVIO_FLAG_WRITE)>=0&&avformat_write_header(output,nullptr)>=0,"Packet-preserving fixture opens");header=true;
    Packet packet;unsigned audios=0,damaged=0;
    while(av_read_frame(input.value,packet.value)>=0) {
      const auto *stream=input.value->streams[packet.value->stream_index];
      if(stream->codecpar->codec_id==AV_CODEC_ID_AC3) {
        if(permanent||audios==2||audios==6||audios==10) {
          require(av_packet_make_writable(packet.value)>=0&&packet.value->size>=7,"Damaged AC-3 packet is writable");
          const int valid_size=packet.value->size;
          require(av_grow_packet(packet.value,32)>=0,"Trailing-header fixture allocation");
          std::fill(packet.value->data+valid_size,packet.value->data+packet.value->size,0);++damaged;
        }
        ++audios;
      }
      av_packet_rescale_ts(packet.value,stream->time_base,output->streams[packet.value->stream_index]->time_base);
      Packet duplicate;
      if(permanent&&stream->codecpar->codec_id==AV_CODEC_ID_AC3) {
        // More than 256 malformed packets must reach the decoder before this
        // finite source ends, so the test proves the recovery limit itself.
        require(av_packet_ref(duplicate.value,packet.value)>=0,"Permanent-damage packet copy");
        if(duplicate.value->pts!=AV_NOPTS_VALUE)++duplicate.value->pts;
        if(duplicate.value->dts!=AV_NOPTS_VALUE)++duplicate.value->dts;
      }
      require(av_interleaved_write_frame(output,packet.value)>=0,"Fixture retains packet timestamps");av_packet_unref(packet.value);
      if(duplicate.value->size)require(av_interleaved_write_frame(output,duplicate.value)>=0,"Permanent-damage fixture exceeds the recovery packet limit");
    }
    require(damaged>=3,"Fixture includes valid AC-3 frames with malformed trailing headers");
    require(av_write_trailer(output)>=0,"Fixture finalizes");header=false;
  } catch(...) {
    if(output){if(header)av_write_trailer(output);avio_closep(&output->pb);avformat_free_context(output);}throw;
  }
  avio_closep(&output->pb);avformat_free_context(output);
}
Json options(const std::filesystem::path &path) {
  return {{"source",{{"kind","fixture"},{"url",path.string()}}},{"width",1280},{"height",720},
    {"fps",30},{"bitrate",4000000},{"encoder","libopenh264"},{"audio",true},{"deinterlace",true},
    {"latency_ms",500},{"camera_only",true}};
}
void camera_decodes(const std::filesystem::path &playlist) {
  Input input(playlist);int video=-1,audio=-1;
  for(unsigned i=0;i<input.value->nb_streams;++i) {
    if(input.value->streams[i]->codecpar->codec_id==AV_CODEC_ID_H264)video=i;
    if(input.value->streams[i]->codecpar->codec_id==AV_CODEC_ID_AAC)audio=i;
  }
  require(input.value->nb_streams==2&&video>=0&&audio>=0,"Recovered camera contains H.264 and AAC");
  Decoder v(input.value->streams[video]->codecpar,input.value->streams[video]->time_base);
  Decoder a(input.value->streams[audio]->codecpar,input.value->streams[audio]->time_base);
  Packet packet;int videos=0,audios=0;float peak=0;int64_t last[2]={AV_NOPTS_VALUE,AV_NOPTS_VALUE};
  while(av_read_frame(input.value,packet.value)>=0) {
    const bool sound=packet.value->stream_index==audio;auto &decoder=sound?a:v;const int track=sound?1:0;
    require(avcodec_send_packet(decoder.value,packet.value)>=0,"Recovered camera packet decodes");
    while(avcodec_receive_frame(decoder.value,decoder.frame)>=0) {
      const auto pts=decoder.frame->best_effort_timestamp;
      require(pts!=AV_NOPTS_VALUE&&(last[track]==AV_NOPTS_VALUE||pts>last[track]),"Recovered camera timestamps advance on both tracks");last[track]=pts;
      if(sound) {
        ++audios;require(decoder.frame->format==AV_SAMPLE_FMT_FLTP,"Camera AAC returns planar float samples");
        const auto *samples=reinterpret_cast<const float*>(decoder.frame->extended_data[0]);
        for(int j=0;j<decoder.frame->nb_samples;++j)peak=std::max(peak,std::abs(samples[j]));
      } else ++videos;
      av_frame_unref(decoder.frame);
    }
    av_packet_unref(packet.value);
  }
  require(videos>=30&&audios>=40&&peak>0.001,"Camera decodes advancing video and non-silent AAC after AC-3 recovery");
}
void mixed_camera(const std::filesystem::path &fixture,const std::filesystem::path &directory) {
  const auto path=directory/"mixed.mkv",camera_directory=directory/"camera";
  corrupt_audio_packets(fixture,path,false);std::filesystem::create_directory(camera_directory);chmod(camera_directory.c_str(),0700);
  std::atomic<bool> stop{false},ready{false};std::atomic<unsigned> recovered{0};
  Media media(options(path),stop,[&](const auto &event,const auto &fields) {
    if(event=="source_recovered"&&fields.at("track")=="audio")recovered+=fields.at("invalid_packets").template get<unsigned>();
  });
  auto camera=std::make_unique<CameraHls>(media,camera_directory,[&](const auto &event,const auto &){if(event=="camera_ready")ready=true;});
  media.start();auto deadline=Clock::now()+std::chrono::seconds(7);
  while(!ready&&!media.failed()&&Clock::now()<deadline)std::this_thread::sleep_for(std::chrono::milliseconds(20));
  require(ready&&!media.failed(),"Broadcast recovers sparse malformed AC-3 trailing headers before camera startup");
  require(recovered>=3,"Real Media decoder reports recovery after each malformed AC-3 packet");
  stop=true;camera.reset();media.close();camera_decodes(camera_directory/"index.m3u8");
}
void permanent_damage_fails(const std::filesystem::path &fixture,const std::filesystem::path &directory) {
  const auto path=directory/"unrecoverable.mkv";corrupt_audio_packets(fixture,path,true);
  std::atomic<bool> stop{false};std::mutex mutex;std::string error;
  const auto started=Clock::now();bool failed=false;
  {
    Media media(options(path),stop,[&](const auto &event,const auto &fields) {
      if(event=="source_error") {std::lock_guard lock(mutex);if(error.empty())error=fields.at("message").template get<std::string>();}
    });
    media.start();
    while(!media.failed()&&Clock::now()-started<std::chrono::seconds(10))std::this_thread::sleep_for(std::chrono::milliseconds(20));
    failed=media.failed();stop=true;media.close();
  } // Join the decoder before reading the final error.
  require(failed&&Clock::now()-started<std::chrono::seconds(10),"Persistent AC-3 corruption fails within the bounded recovery policy");
  require(error=="Channel has too much damaged data; check reception and reconnect",
          "The 256-packet recovery limit stops permanent AC-3 damage, rather than silently waiting for source EOF");
}
}
int main(int argc,char **argv) {
  auto directory=std::filesystem::temp_directory_path()/("airplayvideo-ac3-"+uuid());
  try {
    require(argc==2,"Synthetic broadcast fixture is required");avdevice_register_all();av_log_set_level(AV_LOG_QUIET);
    std::filesystem::create_directory(directory);chmod(directory.c_str(),0700);
    allowlist();decoder_recovers(argv[1]);mixed_camera(argv[1],directory);permanent_damage_fails(argv[1],directory);
    std::filesystem::remove_all(directory);
    std::cout<<"Real AC-3 sync error, exact recovery allowlist, following valid tone, camera H.264/AAC decode and persistent-damage bound passed\n";return 0;
  } catch(const std::exception &e) {std::cerr<<e.what()<<'\n';std::filesystem::remove_all(directory);return 1;}
}
