#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
BRIO 500의 실시간 영상을 ffplay 창으로 표시합니다.

사용법:
  ./scripts/live-view.sh [옵션]

옵션:
  -d, --device PATH       비디오 장치 (기본값: /dev/video0)
  -s, --size WIDTHxHEIGHT 해상도 (기본값: 1920x1080)
  -r, --framerate FPS     초당 프레임 수 (기본값: 30)
  -f, --format FORMAT     입력 형식 (기본값: mjpeg)
      --flip-horizontal   영상을 좌우 반전
  -h, --help              도움말 표시

예시:
  ./scripts/live-view.sh
  ./scripts/live-view.sh -d /dev/video2 -s 1280x720 -r 30
  ./scripts/live-view.sh --flip-horizontal

종료: 영상 창에서 q 또는 Esc를 누르세요.
EOF
}

device="/dev/video0"
video_size="1920x1080"
framerate="30"
input_format="mjpeg"
video_filter=""

while (($# > 0)); do
  case "$1" in
    -d|--device)
      [[ $# -ge 2 ]] || { echo "오류: $1에 값이 필요합니다." >&2; exit 2; }
      device="$2"
      shift 2
      ;;
    -s|--size)
      [[ $# -ge 2 ]] || { echo "오류: $1에 값이 필요합니다." >&2; exit 2; }
      video_size="$2"
      shift 2
      ;;
    -r|--framerate)
      [[ $# -ge 2 ]] || { echo "오류: $1에 값이 필요합니다." >&2; exit 2; }
      framerate="$2"
      shift 2
      ;;
    -f|--format)
      [[ $# -ge 2 ]] || { echo "오류: $1에 값이 필요합니다." >&2; exit 2; }
      input_format="$2"
      shift 2
      ;;
    --flip-horizontal)
      video_filter="hflip"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "오류: 알 수 없는 옵션: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v ffplay >/dev/null 2>&1; then
  echo "오류: ffplay가 없습니다. 다음 명령으로 설치하세요:" >&2
  echo "  sudo apt install ffmpeg" >&2
  exit 1
fi

if [[ ! -e "$device" ]]; then
  echo "오류: 비디오 장치를 찾을 수 없습니다: $device" >&2
  echo "USB 연결을 확인한 뒤 다음 명령으로 장치를 찾아보세요:" >&2
  echo "  ls -l /dev/video*" >&2
  exit 1
fi

if [[ ! "$video_size" =~ ^[0-9]+x[0-9]+$ ]]; then
  echo "오류: 해상도는 1920x1080 형식이어야 합니다: $video_size" >&2
  exit 2
fi

if [[ ! "$framerate" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "오류: FPS는 양수여야 합니다: $framerate" >&2
  exit 2
fi

echo "장치: $device"
echo "영상: $input_format, $video_size, ${framerate}fps"
echo "종료하려면 영상 창에서 q 또는 Esc를 누르세요."

command=(
  ffplay
  -hide_banner
  -loglevel warning
  -fflags nobuffer
  -flags low_delay
  -f v4l2
  -input_format "$input_format"
  -video_size "$video_size"
  -framerate "$framerate"
  -i "$device"
  -window_title "BRIO 500 Live - $device"
)

if [[ -n "$video_filter" ]]; then
  command+=( -vf "$video_filter" )
fi

exec "${command[@]}"
