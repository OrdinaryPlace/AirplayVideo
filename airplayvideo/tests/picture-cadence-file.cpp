#include "picture_cadence.hpp"
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <unistd.h>
using namespace lab::picture;
void require(bool value){if(!value)throw std::runtime_error("Synthetic file analysis failed");}
template<class F>void rejects(F operation){bool rejected=false;try{operation();}catch(...){rejected=true;}require(rejected);}
int main(int argc,char **argv) {
  if(argc!=2)return 2;
  auto root=std::filesystem::path(argv[1]).parent_path();
  try {
    auto result=analyze_file(argv[1]);
    require(result.video_frames==60&&std::abs(result.encoded_fps-30)<0.1&&result.regions.size()==3);
    for(const auto &region:result.regions)require(region.thresholds.size()==6);
    int inherited=open(argv[1],O_RDONLY);require(inherited>=3);
    auto via_fd=analyze_file("/proc/self/fd/"+std::to_string(inherited));close(inherited);
    require(via_fd.video_frames==result.video_frames);
    auto link=root/"symlink.mkv";std::filesystem::create_symlink(argv[1],link);
    rejects([&]{analyze_file(link);});std::filesystem::remove(link);
    auto malformed=root/"invalid.mkv";
    {std::ofstream out(malformed);out<<"not a media file";}
    rejects([&]{analyze_file(malformed);});
    auto truncated=root/"truncated.mkv";
    {std::ifstream source(argv[1],std::ios::binary);std::ofstream out(truncated,std::ios::binary);char header[8]{};source.read(header,sizeof(header));out.write(header,source.gcount());}
    rejects([&]{analyze_file(truncated);});
    auto oversized=root/"oversized.mkv";int fd=open(oversized.c_str(),O_WRONLY|O_CREAT|O_EXCL,0600);require(fd>=0);
    require(ftruncate(fd,128LL*1024*1024+1)==0);close(fd);
    rejects([&]{analyze_file(oversized);});
    rejects([&]{analyze_file("/proc/self/fd/not-a-number");});
    std::cout<<"Synthetic Matroska H264 decode, FD inheritance, truncation, symlink and byte limits passed\n";return 0;
  } catch(...){return 1;}
}
