#!/usr/bin/env bash
# 生成一批模拟“家庭旅行视频”，用来演示流水线。
# 覆盖：开头误录的黑屏、结尾静音、竖屏、25/30 混合帧率、无音轨、误触短片、拍摄时间与 GPS 元数据。
# 语音用 macOS 自带的 `say`（婷婷）合成，所以转录步骤有真实中文语音可识别。
set -euo pipefail

OUT="${1:-samples}"
mkdir -p "$OUT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

speak() { # speak <文件名> <文本>
  say -v Tingting -o "$TMP/$1.aiff" "$2"
}

# make <名称> <视频源> <宽x高> <帧率> <时长> <语音文件|-> <语音起始秒> <拍摄时间> <ISO6709 坐标> [前置黑屏秒]
make() {
  local name=$1 src=$2 size=$3 fps=$4 dur=$5 speech=$6 delay=$7 ctime=$8 loc=$9 black=${10:-0}
  local w=${size%x*} h=${size#*x}
  local vf="[0:v]null[v]"
  if [[ $black != 0 ]]; then
    # 前几秒黑屏，模拟“镜头还在口袋里”
    vf="[0:v]drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='lt(t,$black)'[v]"
  fi
  local inputs=(-f lavfi -i "$src=s=$size:r=$fps"
                -f lavfi -i "anoisesrc=color=pink:amplitude=0.004:r=48000:d=$dur")
  local af="[1:a]anull[a]"
  if [[ $speech != - ]]; then
    inputs+=(-i "$TMP/$speech.aiff")
    local ms=$((delay * 1000))
    af="[2:a]aresample=48000,adelay=${ms}|${ms},apad[s];[1:a][s]amix=inputs=2:duration=first:normalize=0[a]"
  fi
  ffmpeg -hide_banner -loglevel error -y "${inputs[@]}" \
    -filter_complex "$vf;$af" -map "[v]" -map "[a]" -t "$dur" \
    -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -c:a aac -b:a 128k \
    -metadata creation_time="$ctime" -metadata location="$loc" \
    -movflags +use_metadata_tags "$OUT/$name.mov"
  echo "  $OUT/$name.mov  (${w}x${h} ${fps}fps ${dur}s)"
}

echo "合成语音…"
speak airport "我们终于到了东京羽田机场，现在坐车去酒店。"
speak asakusa "这里是浅草寺，人非常多，大家先在门口拍张合影。"
speak river   "孩子们在鸭川边上玩水，玩得特别开心。"

echo "生成视频…"
make IMG_0001 testsrc2   1920x1080 30 12 airport 4 2026-04-01T00:05:00Z "+35.5494+139.7798/" 3
make IMG_0002 smptebars  1920x1080 25 11 asakusa 1 2026-04-01T05:10:00Z "+35.7148+139.7967/"
make IMG_0003 mandelbrot 1080x1920 30 8  -       0 2026-04-02T01:00:00Z "+34.9671+135.7727/"
make IMG_0004 testsrc    1920x1080 30 2  -       0 2026-04-02T01:03:00Z "+34.9671+135.7727/" 2
make IMG_0005 life       1920x1080 30 10 river   2 2026-04-02T07:30:00Z "+35.0094+135.7714/"

# 无音轨的视频（有些相机/延时摄影就是这样），拍摄时间比 IMG_0003 早，用来演示按时间排序
ffmpeg -hide_banner -loglevel error -y -f lavfi -i "cellauto=s=1920x1080:r=30:rule=110" -t 6 \
  -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -an \
  -metadata creation_time=2026-04-01T09:00:00Z -metadata location="+35.6586+139.7454/" \
  -movflags +use_metadata_tags "$OUT/DJI_0100.mov"
echo "  $OUT/DJI_0100.mov  (1920x1080 30fps 6s, 无音轨)"
echo "完成：$OUT/"
