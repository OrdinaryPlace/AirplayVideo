#include "generated.hpp"
#include <pango/pangocairo.h>
#include <cmath>
#include <ctime>
#include <iomanip>
#include <sstream>
namespace lab {
namespace {
void validate_text(const std::string &value, size_t maximum, bool required) {
  require(value.size() <= maximum * 4 && g_utf8_validate(value.data(),value.size(),nullptr),"Invalid generated text");
  require(g_utf8_strlen(value.data(),value.size()) <= long(maximum),"Generated text is too long");
  require(!required || value.find_first_not_of(" \n") != std::string::npos,"Enter a title");
  for(unsigned char c:value) require(c>=32 || c=='\n',"Invalid generated text");
}
void text_block(cairo_t *cr,const std::string &text,double y,double height,double size,bool bold,double r,double g,double b) {
  PangoLayout *layout=pango_cairo_create_layout(cr);
  auto *font=pango_font_description_new();
  pango_font_description_set_family(font,"Noto Sans");
  pango_font_description_set_weight(font,bold?PANGO_WEIGHT_BOLD:PANGO_WEIGHT_NORMAL);
  pango_layout_set_text(layout,text.data(),int(text.size())); // Never parse markup.
  pango_layout_set_width(layout,1600*PANGO_SCALE);
  pango_layout_set_alignment(layout,PANGO_ALIGN_CENTER);
  pango_layout_set_wrap(layout,PANGO_WRAP_WORD_CHAR);
  int w=0,h=0;
  do {
    pango_font_description_set_absolute_size(font,size*PANGO_SCALE);
    pango_layout_set_font_description(layout,font);
    pango_layout_get_pixel_size(layout,&w,&h);
    size-=2;
  } while(h>height && size>=24);
  pango_layout_set_height(layout,int(height)*PANGO_SCALE);
  pango_layout_set_ellipsize(layout,PANGO_ELLIPSIZE_END);
  pango_layout_get_pixel_size(layout,&w,&h);
  cairo_move_to(cr,160,y+(height-h)/2);
  cairo_set_source_rgb(cr,r,g,b); pango_cairo_show_layout(cr,layout);
  pango_font_description_free(font); g_object_unref(layout);
}
}
struct GeneratedVideo::Impl {
  int width,height;
  double seconds;
  std::string display;
  GTimeZone *zone=nullptr;
  cairo_surface_t *background=nullptr,*surface=nullptr;
  Impl(const Json &s,int w,int h):width(w),height(h),seconds(s.at("duration_seconds").get<int>()),display(s.at("display")) {
    require((w==1920&&h==1080)||(w==1280&&h==720),"Unsupported generated canvas");
    require(s.at("duration_seconds").is_number_integer()&&seconds>=1&&seconds<=86400,"Length must be 1 to 86400 seconds");
    require(display=="time"||display=="countdown"||display=="neither","Choose time, countdown or neither");
    std::string title=s.at("title"),tagline=s.at("tagline"),timezone=s.at("timezone");
    validate_text(title,160,true); validate_text(tagline,240,false);
    require(timezone.size()<=100&&!timezone.empty()&&timezone[0]!='/'&&timezone.find("..") == std::string::npos,"Invalid time zone");
    for(unsigned char c:timezone) require(std::isalnum(c)||c=='/'||c=='_'||c=='-'||c=='+',"Invalid time zone");
    zone=g_time_zone_new_identifier(timezone.c_str()); require(zone,"Unknown time zone");
    background=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,w,h);
    surface=cairo_image_surface_create(CAIRO_FORMAT_ARGB32,w,h);
    auto *cr=cairo_create(background); cairo_scale(cr,w/1920.0,h/1080.0);
    auto *gradient=cairo_pattern_create_linear(0,0,1920,1080);
    cairo_pattern_add_color_stop_rgb(gradient,0,0.035,0.075,0.15);
    cairo_pattern_add_color_stop_rgb(gradient,1,0.02,0.19,0.23);
    cairo_set_source(cr,gradient); cairo_paint(cr); cairo_pattern_destroy(gradient);
    cairo_set_source_rgba(cr,0.35,0.85,0.85,0.07);
    cairo_arc(cr,1750,0,650,0,2*M_PI); cairo_fill(cr);
    bool timed=display!="neither";
    text_block(cr,title,timed?185:275,300,100,true,0.96,0.98,1);
    text_block(cr,tagline,timed?495:585,155,44,false,0.66,0.8,0.86);
    cairo_destroy(cr);
    require(cairo_surface_status(background)==CAIRO_STATUS_SUCCESS&&cairo_surface_status(surface)==CAIRO_STATUS_SUCCESS,"Generated image allocation failed");
  }
  ~Impl() {cairo_surface_destroy(surface);cairo_surface_destroy(background);g_time_zone_unref(zone);}
  std::string timer(double elapsed,int64_t unix_seconds) const {
    if(display=="neither") return "";
    if(display=="time") {
      auto *utc=g_date_time_new_from_unix_utc(unix_seconds);
      require(utc,"Invalid clock time");
      auto *local=g_date_time_to_timezone(utc,zone);
      char *value=g_date_time_format(local,"%H:%M:%S");
      std::string result=value;g_free(value);g_date_time_unref(local);g_date_time_unref(utc);return result;
    }
    int remaining=int(std::ceil(std::max(0.0,seconds-elapsed)));
    std::ostringstream out;out<<std::setfill('0');
    if(remaining>=3600) out<<remaining/3600<<":"<<std::setw(2);
    else out<<std::setw(2);
    out<<(remaining/60)%60<<":"<<std::setw(2)<<remaining%60;
    return out.str();
  }
  void draw(double elapsed,int64_t unix_seconds) {
    require(std::isfinite(elapsed)&&elapsed>=0,"Invalid animation time");
    auto *cr=cairo_create(surface); cairo_set_source_surface(cr,background,0,0);cairo_paint(cr);
    cairo_scale(cr,width/1920.0,height/1080.0);
    if(display!="neither") text_block(cr,timer(elapsed,unix_seconds),685,195,150,true,0.38,0.91,0.85);
    cairo_set_source_rgba(cr,1,1,1,0.12);cairo_rectangle(cr,160,969,1600,6);cairo_fill(cr);
    cairo_set_source_rgb(cr,0.38,0.91,0.85);
    if(display=="countdown") cairo_rectangle(cr,160,969,1600*std::clamp(1-elapsed/seconds,0.0,1.0),6);
    else cairo_rectangle(cr,160+1440*(0.5-0.5*std::cos(elapsed*0.7)),966,160,12);
    cairo_fill(cr);cairo_destroy(cr);cairo_surface_flush(surface);
  }
};
GeneratedVideo::GeneratedVideo(const Json &s,int w,int h):impl_(std::make_unique<Impl>(s,w,h)) {}
GeneratedVideo::~GeneratedVideo()=default;
const uint8_t *GeneratedVideo::draw(double e,int64_t t) {impl_->draw(e,t);return cairo_image_surface_get_data(impl_->surface);}
int GeneratedVideo::stride() const {return cairo_image_surface_get_stride(impl_->surface);}
double GeneratedVideo::duration() const {return impl_->seconds;}
std::string GeneratedVideo::timer(double e,int64_t t) const {return impl_->timer(e,t);}
Bytes GeneratedVideo::png(double e,int64_t t) {
  draw(e,t);Bytes result;
  auto write=[](void *out,const unsigned char *data,unsigned int size) {auto &bytes=*static_cast<Bytes*>(out);bytes.insert(bytes.end(),data,data+size);return CAIRO_STATUS_SUCCESS;};
  require(cairo_surface_write_to_png_stream(impl_->surface,write,&result)==CAIRO_STATUS_SUCCESS,"Preview encoding failed");return result;
}
Json generated_preview(const Json &settings) {
  GeneratedVideo renderer(settings,1920,1080);
  auto png=renderer.png(0,std::time(nullptr));
  gchar *encoded=g_base64_encode(png.data(),png.size());std::string data=encoded;g_free(encoded);
  return {{"image","data:image/png;base64,"+data},{"width",1920},{"height",1080}};
}
}
