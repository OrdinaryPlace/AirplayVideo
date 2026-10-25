#include "picture_cadence.hpp"
#include <cerrno>
#include <charconv>
#include <fcntl.h>
#include <memory>
#include <sys/stat.h>
#include <unistd.h>
extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libswscale/swscale.h>
}

namespace lab::picture {
namespace {
constexpr int64_t max_bytes=128LL*1024*1024;
void require(bool condition) { if(!condition)throw std::runtime_error("unsupported"); }
struct LocalFile {
  int fd=-1;
  int64_t size=0, position=0;
  explicit LocalFile(const std::string &path) {
    constexpr std::string_view prefix="/proc/self/fd/";
    if(path.starts_with(prefix)) {
      int inherited=-1;
      auto value=std::string_view(path).substr(prefix.size());
      auto [end,error]=std::from_chars(value.data(),value.data()+value.size(),inherited);
      require(error==std::errc()&&end==value.data()+value.size()&&inherited>=3);
      fd=fcntl(inherited,F_DUPFD_CLOEXEC,3);
    } else fd=open(path.c_str(),O_RDONLY|O_CLOEXEC|O_NOFOLLOW);
    require(fd>=0);
    struct stat details{};
    if(fstat(fd,&details)!=0||!S_ISREG(details.st_mode)||details.st_size<=0||details.st_size>max_bytes) {
      close(fd);fd=-1;throw std::runtime_error("bounds");
    }
    size=details.st_size;
  }
  ~LocalFile(){if(fd>=0)close(fd);}
  static int read(void *opaque,uint8_t *buffer,int requested) {
    auto &file=*static_cast<LocalFile*>(opaque);
    if(requested<=0)return AVERROR(EINVAL);
    if(file.position>=file.size)return AVERROR_EOF;
    const auto wanted=size_t(std::min<int64_t>(requested,file.size-file.position));
    ssize_t count;
    do{count=pread(file.fd,buffer,wanted,file.position);}while(count<0&&errno==EINTR);
    if(count<=0)return count==0?AVERROR_EOF:AVERROR(EIO);
    file.position+=count;return int(count);
  }
  static int64_t seek(void *opaque,int64_t offset,int whence) {
    auto &file=*static_cast<LocalFile*>(opaque);
    if(whence==AVSEEK_SIZE)return file.size;
    whence&=~AVSEEK_FORCE;
    int64_t base=whence==SEEK_SET?0:whence==SEEK_CUR?file.position:whence==SEEK_END?file.size:-1;
    if(base<0 || offset < -base || offset > file.size-base)return AVERROR(EINVAL);
    file.position=base+offset;return file.position;
  }
};
struct Input {
  AVFormatContext *format=avformat_alloc_context();
  AVIOContext *io=nullptr;
  AVCodecContext *decoder=nullptr;
  SwsContext *scale=nullptr;
  AVPacket *packet=av_packet_alloc();
  AVFrame *frame=av_frame_alloc();
  ~Input(){
    sws_freeContext(scale);av_frame_free(&frame);av_packet_free(&packet);
    avcodec_free_context(&decoder);avformat_close_input(&format);
    if(io){av_freep(&io->buffer);avio_context_free(&io);}
  }
};
}

Summary analyze_file(const std::string &path) {
  av_log_set_level(AV_LOG_QUIET);
  LocalFile file(path);Input input;
  require(input.format&&input.packet&&input.frame);
  auto *buffer=static_cast<uint8_t*>(av_malloc(32768));require(buffer);
  input.io=avio_alloc_context(buffer,32768,0,&file,LocalFile::read,nullptr,LocalFile::seek);
  if(!input.io){av_free(buffer);throw std::runtime_error("allocation");}
  input.format->pb=input.io;
  input.format->flags|=AVFMT_FLAG_CUSTOM_IO;
  // No nested URL/file access: all demuxer reads use the already-open regular FD.
  input.format->io_open=[](AVFormatContext*,AVIOContext**,const char*,int,AVDictionary**){return AVERROR(EPERM);};
  AVDictionary *options=nullptr;
  av_dict_set(&options,"protocol_whitelist","file",0);
  const auto *matroska=av_find_input_format("matroska");
  if(!matroska){av_dict_free(&options);throw std::runtime_error("unsupported");}
  const int opened=avformat_open_input(&input.format,"",matroska,&options);
  av_dict_free(&options);require(opened>=0);
  require(input.format->nb_streams>0&&input.format->nb_streams<=8);
  if(input.format->duration!=AV_NOPTS_VALUE)
    require(input.format->duration>0&&input.format->duration<=max_duration_us+100000);
  int video=-1;
  for(unsigned i=0;i<input.format->nb_streams;++i)if(input.format->streams[i]->codecpar->codec_type==AVMEDIA_TYPE_VIDEO) {
    require(video<0);video=int(i);
  }
  require(video>=0);
  auto *stream=input.format->streams[video];auto *parameters=stream->codecpar;
  require(parameters->codec_id==AV_CODEC_ID_H264&&parameters->width>0&&parameters->width<=1920&&
          parameters->height>0&&parameters->height<=1080&&stream->time_base.num>0&&stream->time_base.den>0);
  const auto *codec=avcodec_find_decoder(AV_CODEC_ID_H264);require(codec);
  input.decoder=avcodec_alloc_context3(codec);require(input.decoder);
  require(avcodec_parameters_to_context(input.decoder,parameters)>=0);
  input.decoder->thread_count=1;
  input.decoder->max_pixels=1920*1080;
  input.decoder->err_recognition=AV_EF_CRCCHECK|AV_EF_CAREFUL|AV_EF_EXPLODE;
  require(avcodec_open2(input.decoder,codec,nullptr)>=0);
  Cadence cadence;
  auto receive=[&] {
    int result;
    while((result=avcodec_receive_frame(input.decoder,input.frame))>=0) {
      auto *frame=input.frame;
      require(frame->width>0&&frame->width<=1920&&frame->height>0&&frame->height<=1080&&
              frame->best_effort_timestamp!=AV_NOPTS_VALUE);
      const int64_t pts=av_rescale_q(frame->best_effort_timestamp,stream->time_base,{1,1000000});
      require(pts>=0&&pts<=max_duration_us);
      input.scale=sws_getCachedContext(input.scale,frame->width,frame->height,AVPixelFormat(frame->format),
          width,height,AV_PIX_FMT_GRAY8,SWS_AREA,nullptr,nullptr,nullptr);require(input.scale);
      std::vector<uint8_t> grayscale(size_t(width)*height);
      uint8_t *destination[]={grayscale.data(),nullptr,nullptr,nullptr};
      const int lines[]={width,0,0,0};
      require(sws_scale(input.scale,frame->data,frame->linesize,0,frame->height,destination,lines)==height);
      cadence.add(grayscale,pts);av_frame_unref(frame);
    }
    require(result==AVERROR(EAGAIN)||result==AVERROR_EOF);
  };
  int result;
  while((result=av_read_frame(input.format,input.packet))>=0) {
    require(input.packet->size>=0&&input.packet->size<=16*1024*1024);
    if(input.packet->stream_index==video) {
      require(avcodec_send_packet(input.decoder,input.packet)>=0);receive();
    }
    av_packet_unref(input.packet);
  }
  require(result==AVERROR_EOF&&(input.io->error>=0||input.io->error==AVERROR_EOF));
  require(avcodec_send_packet(input.decoder,nullptr)>=0);receive();
  auto summary=cadence.summary();
  if(input.format->duration!=AV_NOPTS_VALUE)
    require(summary.duration_seconds+1>=double(input.format->duration)/AV_TIME_BASE);
  return summary;
}
}
