#pragma once
#include <algorithm>
#include <span>
#include <va/va.h>

namespace lab {
// Match FFmpeg 8's default entrypoint preference. A driver can expose only
// EncSliceLP even when the caller did not request low-power encoding.
inline VAEntrypoint default_h264_entrypoint(std::span<const VAEntrypoint> available) {
  for(auto candidate : {VAEntrypointEncSlice, VAEntrypointEncPicture, VAEntrypointEncSliceLP})
    if(std::find(available.begin(),available.end(),candidate)!=available.end()) return candidate;
  return VAEntrypoint(0);
}
}
