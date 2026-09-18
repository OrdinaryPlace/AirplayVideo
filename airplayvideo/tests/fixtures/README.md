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
