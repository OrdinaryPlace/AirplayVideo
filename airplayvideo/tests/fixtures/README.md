# Synthetic broadcast fixture

`broadcast-mid-gop.ts.gz` contains only generated SMPTE color bars and an 880 Hz
tone. No household recording, broadcast content or metadata is included. The
source is 1920x1080, top-field-first interlaced MPEG-2 at 30000/1001 fps, with B
pictures and 48 kHz AC-3. It deliberately begins after the first sequence header.

Generated in a disposable Debian bookworm Linux container using its FFmpeg 5.1.9
package (development only; the app still ships its minimal LGPL FFmpeg build):

```sh
ffmpeg -f lavfi -i smptebars=size=1920x1080:rate=30000/1001 \
  -f lavfi -i sine=frequency=880:sample_rate=48000 -t 8 -vf setfield=tff \
  -c:v mpeg2video -flags +ilme+ildct -top 1 -g 60 -bf 2 -b:v 2M \
  -c:a ac3 -b:a 192k -f mpegts broadcast.ts
```

Discard transport packets before the second video PES start, then compress:

```python
from pathlib import Path
import gzip
data = Path("broadcast.ts").read_bytes()
starts = [i for i in range(0, len(data), 188)
          if data[i] == 0x47 and ((data[i+1] & 31) << 8 | data[i+2]) == 256
          and data[i+1] & 64]
Path("broadcast-mid-gop.ts.gz").write_bytes(gzip.compress(data[starts[1]:], mtime=0))
```

The uncompressed fixture SHA-256 is
`05d6591b258db83cd04c696af763696071cfed62fc8c3cc2be9a8d1dd60f59b9`.
Version 0.2.1 fails this fixture on its first incomplete video packet. The
regression requires recovery, non-silent audio, a common timeline, bounded audio
processing age, prompt cancellation, and bounded failure for unrecoverable data.

# Fragmented MP4 probe coverage fixture

`large-video-aac.mp4.gz` contains one generated black H.264 video frame and a
one-second 1 kHz AAC-LC tone (44.1 kHz stereo). It has no external media, URLs or
private metadata. Its first video sample includes a valid H.264 filler-data NAL
unit, putting the first AAC packet beyond FFprobe's default 5,000,000-byte probe
limit. The 6,304,771-byte file compresses to 17,597 bytes. This models a large
video fragment without expensive encoding in CI.

The test exercises the installed FFprobe binary: default analysis exits zero
but leaves the AAC profile absent; analysis covering the bounded local file
identifies LC. The existing strict output validator rejects the former and
accepts the latter. The same behavior was separately reproduced with a generated
59.9 MB, four-second HEVC/AAC HLS fragment using FFprobe 8.0.1.

The small base MP4 was generated with development-only FFmpeg 4.4.1/libx264:

```sh
ffmpeg -f lavfi -i color=c=black:s=320x180:r=1 \
  -f lavfi -i sine=frequency=1000:sample_rate=44100 -t 1 \
  -c:v libx264 -preset ultrafast -profile:v baseline -bf 0 \
  -c:a aac -profile:a aac_low -ac 2 \
  -f mp4 -movflags frag_keyframe+empty_moov+default_base_moof small.mp4
```

Its single video sample was extended with a length-prefixed filler NAL: byte
`0x0c`, 6 MiB of `0xff`, then `0x80`. Update the video `tfhd` default sample size,
the audio `trun` data offset, and the `mdat` box size by the filler length before
inserting it immediately after the original video sample. This leaves the
encoded picture, AAC packets, sample counts and timestamps intact. Compress
with `gzip.compress(data, mtime=0)`.

The uncompressed fixture SHA-256 is
`331892eb3708a312bd7edc32d83de629379e47fd1d32e360c2f2ee3a3893c76d`.
