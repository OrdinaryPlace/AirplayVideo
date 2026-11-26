#include "stream.hpp"
#include "video_rate.hpp"
#include "frame_rate.hpp"
#include "vaapi_rate.hpp"
#include <iostream>
using namespace lab;

namespace {
void rejected_join_keyframe() {
  AVCodecContext *context=nullptr;AVFrame *frame=nullptr;AVPacket *packet=nullptr;
  try {
    const AVCodec *codec=avcodec_find_encoder_by_name("libopenh264");
    require(codec,"Software keyframe test encoder is built in");
    context=avcodec_alloc_context3(codec);require(context,"Keyframe encoder context");
    context->width=320;context->height=180;context->pix_fmt=AV_PIX_FMT_YUV420P;
    context->time_base={1,90000};context->framerate={30,1};context->bit_rate=1000000;
    context->gop_size=60;context->max_b_frames=0;context->thread_count=1;
    require(avcodec_open2(context,codec,nullptr)>=0,"Keyframe encoder opens");
    frame=av_frame_alloc();packet=av_packet_alloc();require(frame&&packet,"Keyframe test allocation");
    frame->format=context->pix_fmt;frame->width=context->width;frame->height=context->height;
    require(av_frame_get_buffer(frame,32)>=0,"Keyframe test picture allocation");
    VideoFrameGate gate(30,false);std::map<int64_t,bool> output;
    auto receive=[&] {
      for(;;) {
        const int result=avcodec_receive_packet(context,packet);
        if(result==AVERROR(EAGAIN)||result==AVERROR_EOF)break;
        require(result>=0,"Keyframe test encoder produces packets");
        output[packet->pts]=(packet->flags&AV_PKT_FLAG_KEY)!=0;
        av_packet_unref(packet);
      }
    };
    // The joining frame at 34 ms falls in the already accepted 33 ms bin.
    // Its I-frame request must reach the 67 ms frame, rather than disappearing
    // until the next periodic GOP two seconds later.
    for(int64_t us:{0LL,33333LL,34000LL,66667LL,100000LL}) {
      const auto decision=gate.accept(us,us==34000);
      if(us==34000)require(!decision.accepted,"Joining capture is deterministically rejected by the FPS gate");
      if(!decision.accepted)continue;
      require(av_frame_make_writable(frame)>=0,"Keyframe test picture is writable");
      for(int plane=0;plane<3;++plane)
        std::fill(frame->data[plane],frame->data[plane]+frame->linesize[plane]*(plane?90:180),plane?128:16);
      frame->pts=av_rescale_q(us,{1,1000000},context->time_base);
      frame->pict_type=decision.keyframe?AV_PICTURE_TYPE_I:AV_PICTURE_TYPE_NONE;
      require(avcodec_send_frame(context,frame)>=0,"Keyframe test picture encodes");receive();
    }
    require(avcodec_send_frame(context,nullptr)>=0,"Keyframe test encoder flushes");receive();
    require(output.size()==4&&output.at(0),"Frame selection retains four pictures and an initial keyframe");
    require(!output.at(3000)&&output.at(6000)&&!output.at(9000),
            "A rejected join request produces exactly one keyframe on the next accepted encoded picture");
  } catch(...) {av_packet_free(&packet);av_frame_free(&frame);avcodec_free_context(&context);throw;}
  av_packet_free(&packet);av_frame_free(&frame);avcodec_free_context(&context);
}
}

int main() {
  try {
    rejected_join_keyframe();
    const VAEntrypoint low_power_only[]={VAEntrypointVLD,VAEntrypointEncSliceLP};
    const VAEntrypoint both[]={VAEntrypointEncSliceLP,VAEntrypointEncSlice};
    const VAEntrypoint decode_only[]={VAEntrypointVLD};
    require(default_h264_entrypoint(low_power_only)==VAEntrypointEncSliceLP,
            "Low-power-only hardware must be queried at the path FFmpeg selects");
    require(default_h264_entrypoint(both)==VAEntrypointEncSlice,
            "Capabilities must follow FFmpeg preference, not driver enumeration order");
    require(default_h264_entrypoint(decode_only)==VAEntrypoint(0),
            "Decode support does not establish an encoder entrypoint");
    Json config={{"source",{{"kind","synthetic"}}},{"width",1920},{"height",1080},
                 {"fps",30},{"encoder","h264_vaapi"},{"bitrate",16000000},
                 {"rate_control","vbr"},{"max_bitrate",30000000}};
    const auto *encoder=avcodec_find_encoder_by_name("h264_vaapi");
    require(encoder,"VAAPI encoder is built in");
    auto *context=avcodec_alloc_context3(encoder);
    require(context,"Encoder context");
    // Match the stream encoder's explicit no-reordering policy; FFmpeg's
    // newly allocated VAAPI context can default to enabling B-frames.
    context->max_b_frames=0;
    VideoRate::parse(config).apply(context);
    int64_t selected=0;
    require(av_opt_get_int(context->priv_data,"rc_mode",0,&selected)>=0&&selected!=0,
            "VBR must select a mode instead of leaving rate control automatic");
    require(context->bit_rate==16000000&&context->rc_max_rate==30000000&&context->rc_buffer_size==15000000,
            "Independent target, peak and bounded coded-bit reservoir reach libavcodec");
    require(context->max_b_frames==0,"Rate control must not enable picture reordering");
    avcodec_free_context(&context);

    for(const auto &bad:Json::array({{{"rate_control","unknown"}},{{"bitrate",true}},
        {{"bitrate",16000000.5}},{{"bitrate",21000000}},{{"max_bitrate",8000000}},
        {{"max_bitrate",41000000}},{{"max_bitrate",true}},{{"encoder","libopenh264"}}})) {
      auto input=config;input.update(bad);std::atomic<bool> stop{false};bool rejected=false;
      try {Media media(input,stop,[](const auto &,const auto &){});} catch(...) {rejected=true;}
      require(rejected,"Invalid rate control must fail before a source or GPU is opened");
    }
    config.erase("max_bitrate");bool rejected=false;
    try{VideoRate::parse(config);}catch(...){rejected=true;}
    require(rejected,"Explicit VBR requires an explicit maximum");
    config.erase("rate_control");config["bitrate"]=8000000;config["encoder"]="libopenh264";
    context=avcodec_alloc_context3(avcodec_find_encoder_by_name("libopenh264"));
    require(context,"Software encoder context");
    VideoRate::parse(config).apply(context);
    require(context->bit_rate==8000000&&context->rc_max_rate==0&&context->rc_buffer_size==0,
            "Legacy requests preserve automatic rate control without a new cap");
    avcodec_free_context(&context);
    std::cout<<"Retained join keyframes, rate-control validation, explicit VBR and legacy compatibility passed\n";
    return 0;
  }catch(const std::exception &e){std::cerr<<e.what()<<'\n';return 1;}
}
