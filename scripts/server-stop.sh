#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
pid_file="$project_dir/data/web-service.pid"

if [[ ! -f "$pid_file" ]]; then
  echo "PID 파일이 없습니다. 서버가 시작 스크립트로 실행되지 않았습니다."
  exit 0
fi

pid="$(cat "$pid_file")"
if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
  rm -f "$pid_file"
  echo "잘못된 PID 파일을 정리했습니다."
  exit 1
fi

if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$pid_file"
  echo "이미 종료된 서버의 PID 파일을 정리했습니다."
  exit 0
fi

command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
if [[ "$command" != *"brio_ocr_monitor.web"* ]]; then
  echo "PID $pid 프로세스가 웹캠 서버가 아니어서 종료하지 않았습니다." >&2
  exit 1
fi

kill -INT "$pid"
for _ in {1..20}; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    echo "서버를 종료했습니다."
    exit 0
  fi
  sleep 0.5
done

echo "서버가 제한 시간 안에 종료되지 않았습니다. (PID $pid)" >&2
exit 1
