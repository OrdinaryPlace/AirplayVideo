#!/bin/sh
set -eu
directory=$(mktemp -d)
trap 'rm -rf "$directory"' EXIT
ffmpeg -nostdin -loglevel error -f lavfi -i testsrc2=size=320x180:rate=30 \
  -t 2 -an -c:v libopenh264 -pix_fmt yuv420p -f matroska "$directory/synthetic.mkv"
"$1" "$directory/synthetic.mkv"
"$2" "$directory/synthetic.mkv" > "$directory/result.json"
test "$(wc -c < "$directory/result.json")" -lt 16384
grep -q '"version":1' "$directory/result.json"
grep -q '"video_frames":60' "$directory/result.json"
if "$2" "$directory/invalid.mkv" > "$directory/failure.json" 2> "$directory/failure.err"; then exit 1; fi
test ! -s "$directory/failure.json"
test ! -s "$directory/failure.err"
