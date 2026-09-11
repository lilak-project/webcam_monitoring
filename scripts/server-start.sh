#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="$project_dir/data"
pid_file="$runtime_dir/web-service.pid"
log_file="$runtime_dir/web-service.log"
port="${PORT:-8080}"

mkdir -p "$runtime_dir"

if [[ -f "$pid_file" ]]; then
  pid="$(cat "$pid_file")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "서버가 이미 실행 중입니다. (PID $pid, http://localhost:$port)"
    exit 0
  fi
  rm -f "$pid_file"
fi

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "포트 $port를 다른 서버가 사용 중입니다. 먼저 해당 서버를 종료하세요." >&2
  exit 1
fi

PORT="$port" nohup "$project_dir/scripts/web-service.sh" >>"$log_file" 2>&1 &
pid=$!
echo "$pid" >"$pid_file"

for _ in {1..30}; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    echo "서버 시작에 실패했습니다. 로그: $log_file" >&2
    tail -n 20 "$log_file" >&2 || true
    exit 1
  fi
  status="$(curl --max-time 1 --silent --fail "http://localhost:$port/api/status" 2>/dev/null || true)"
  if [[ "$status" == *'"connected": true'* ]]; then
    echo "서버를 시작했습니다. (PID $pid)"
    echo "주소: http://localhost:$port"
    echo "로그: $log_file"
    exit 0
  fi
  sleep 0.5
done

echo "서버는 실행 중이지만 카메라가 연결되지 않았습니다. 로그: $log_file" >&2
exit 1
