#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
if [[ -x "$project_dir/.runtime/bin/python3" ]]; then
  export PATH="$project_dir/.runtime/bin:$PATH"
fi

if [[ ! -f config.json ]]; then
  cp config.example.json config.json
  echo "config.json을 기본 설정으로 생성했습니다."
fi

exec python3 -m brio_ocr_monitor.web --config config.json --host 0.0.0.0 --port "${PORT:-8080}"
