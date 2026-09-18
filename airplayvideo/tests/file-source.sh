#!/bin/sh
set -eu
fixture=$(mktemp --suffix=.ts)
trap 'rm -f "$fixture"' EXIT
ffmpeg -loglevel error -f lavfi -i testsrc2=size=1280x720:rate=30 \
  -f lavfi -i sine=frequency=440:sample_rate=48000 -t 8 \
  -af 'asetpts=PTS+gte(T\,1)*0.3/TB' \
  -c:v libopenh264 -b:v 4M -c:a aac -f mpegts -y "$fixture"
"$1" "$fixture"
