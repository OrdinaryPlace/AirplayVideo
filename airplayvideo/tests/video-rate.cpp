#include "stream.hpp"
#include "video_rate.hpp"
#include <iostream>
using namespace lab;

int main() {
  try {
    Json config={{"source",{{"kind","synthetic"}}},{"width",1920},{"height",1080},
                 {"fps",30},{"encoder","h264_vaapi"},{"bitrate",16000000},
                 {"rate_control","vbr"},{"max_bitrate",30000000}};
    const auto *encoder=avcodec_find_encoder_by_name("h264_vaapi");
    require(encoder,"VAAPI encoder is built in");
    auto *context=avcodec_alloc_context3(encoder);
    require(context,"Encoder context");
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
    std::cout<<"Rate-control validation, explicit VBR and legacy compatibility passed\n";
    return 0;
  }catch(const std::exception &e){std::cerr<<e.what()<<'\n';return 1;}
}
