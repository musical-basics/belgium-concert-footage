#!/bin/bash
# Generate small, browser-friendly proxies for the 3-up editor.
# 640x360, 30fps, short GOP for precise scrubbing, faststart for streaming.
# Each proxy keeps its own audio (GUI mutes all but the active one).
set -e
cd "$(dirname "$0")/.."
SRC="Main Footage"
OUT="proxies"
mkdir -p "$OUT"

# name pairs: source file -> proxy id
declare -a NAMES=("back camera v2" "Livestream Footage" "camera next to piano")
declare -a IDS=("back" "livestream" "piano")

for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"
  id="${IDS[$i]}"
  src="$SRC/$name.mov"
  dst="$OUT/$id.mp4"
  if [ -f "$dst" ]; then
    echo "[$id] proxy exists, skipping"
    continue
  fi
  echo "[$id] encoding proxy from $src ..."
  ffmpeg -hide_banner -loglevel warning -stats \
    -hwaccel videotoolbox -i "$src" \
    -vf "scale=640:360,fps=30" \
    -c:v h264_videotoolbox -b:v 1200k -g 30 \
    -c:a aac -b:a 96k -ac 2 \
    -movflags +faststart \
    "$dst.tmp.mp4"
  mv "$dst.tmp.mp4" "$dst"
  echo "[$id] done -> $dst"
done
echo "ALL PROXIES DONE"
