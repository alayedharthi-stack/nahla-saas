#!/bin/bash
set -e
cd /tmp/nahla-video/out
printf "file 'seg%d.mp4'\n" 0 1 2 3 > list.txt
ffmpeg -y -loglevel error -f concat -safe 0 -i list.txt -c copy video_only.mp4
ffmpeg -y -loglevel error -i video_only.mp4 -i ../audio.wav \
  -af "loudnorm=I=-14:TP=-1.5:LRA=9" -ar 48000 \
  -c:v copy -c:a aac -b:a 192k -shortest -movflags +faststart nahla-salla.mp4
ffprobe -v error -show_entries format=duration,size:stream=codec_name,width,height,r_frame_rate -of compact nahla-salla.mp4
