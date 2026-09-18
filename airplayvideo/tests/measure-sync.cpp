// Measure the flash/beep fixture using decoded content and original timestamps.
#include <algorithm>
#include <cmath>
#include <iostream>
#include <vector>
#include <nlohmann/json.hpp>
extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
}
int main(int argc,char **argv) {
  if(argc!=2)return 2;
  av_log_set_level(AV_LOG_ERROR);
  AVFormatContext *input=nullptr;
  if(avformat_open_input(&input,argv[1],nullptr,nullptr)<0||avformat_find_stream_info(input,nullptr)<0)return 3;
  std::vector<AVCodecContext*> codecs(input->nb_streams,nullptr);
  for(unsigned i=0;i<input->nb_streams;++i) {
    auto *p=input->streams[i]->codecpar;
    auto *decoder=avcodec_find_decoder(p->codec_id);if(!decoder)continue;
    auto *c=avcodec_alloc_context3(decoder);c->thread_count=2;
    if(avcodec_parameters_to_context(c,p)<0||avcodec_open2(c,decoder,nullptr)<0)return 4;
    codecs[i]=c;
  }
  std::vector<double> flashes,beeps;bool white=false,tone=false;int frames=0;
  AVPacket *p=av_packet_alloc();AVFrame *f=av_frame_alloc();
  while(av_read_frame(input,p)>=0) {
    int index=p->stream_index;auto *c=codecs[index];
    if(!c){av_packet_unref(p);continue;}
    if(avcodec_send_packet(c,p)<0)return 5;av_packet_unref(p);
    while(avcodec_receive_frame(c,f)>=0) {
      double t=f->best_effort_timestamp*av_q2d(input->streams[index]->time_base);
      if(c->codec_type==AVMEDIA_TYPE_VIDEO) {
        if(f->format!=AV_PIX_FMT_YUV420P)return 6;
        double total=0;int count=0;
        for(int y=0;y<f->height;y+=24)for(int x=0;x<f->width;x+=24){total+=f->data[0][y*f->linesize[0]+x];++count;}
        bool bright=total/count>180;if(bright&&!white)flashes.push_back(t);white=bright;++frames;
      } else if(c->codec_type==AVMEDIA_TYPE_AUDIO) {
        if(f->format!=AV_SAMPLE_FMT_S16)return 7;
        const auto *samples=reinterpret_cast<const int16_t*>(f->data[0]);
        int channels=f->ch_layout.nb_channels;
        for(int start=0;start<f->nb_samples;start+=44) {
          int count=std::min(44,f->nb_samples-start);double power=0;
          for(int i=0;i<count;++i){double value=samples[(start+i)*channels];power+=value*value;}
          bool active=std::sqrt(power/count)>1500;
          if(active&&!tone)beeps.push_back(t+double(start)/f->sample_rate);
          tone=active;
        }
      }
      av_frame_unref(f);
    }
  }
  std::vector<double> offsets;
  for(double flash:flashes)if(!beeps.empty()) {
    double beep=*std::min_element(beeps.begin(),beeps.end(),[&](double a,double b){return std::abs(a-flash)<std::abs(b-flash);});
    if(std::abs(beep-flash)<0.4)offsets.push_back((beep-flash)*1000);
  }
  auto sorted=offsets;std::sort(sorted.begin(),sorted.end());
  nlohmann::json result={{"video_frames",frames},{"flashes",flashes.size()},{"beeps",beeps.size()},
    {"matched_events",offsets.size()},{"offsets_ms",offsets}};
  if(!sorted.empty())result["audio_minus_video_ms"]={{"min",sorted.front()},{"median",sorted[sorted.size()/2]},{"max",sorted.back()}};
  std::cout<<result.dump(2)<<'\n';
  av_frame_free(&f);av_packet_free(&p);for(auto *c:codecs)avcodec_free_context(&c);avformat_close_input(&input);
  return offsets.size()>=8?0:8;
}
