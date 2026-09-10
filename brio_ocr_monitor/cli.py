from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .pipeline import PipelineError, doctor, load_config, run_once


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="BRIO 500 screen OCR monitor")
    subparsers = result.add_subparsers(dest="command", required=True)
    for name in ("doctor", "run", "loop"):
        command = subparsers.add_parser(name)
        command.add_argument("--config", type=Path, default=Path("config.json"))
        if name in ("run", "loop"):
            command.add_argument("--input", type=Path, help="process an existing image")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        config_path = args.config.resolve()
        config = load_config(config_path)
        if args.command == "doctor":
            checks = doctor(config)
            for name, passed, detail in checks:
                print(f"{'OK' if passed else 'FAIL':4} {name}: {detail}")
            return 0 if all(item[1] for item in checks) else 1
        while True:
            reading = run_once(config, config_path.parent, args.input)
            print(json.dumps(reading.__dict__, ensure_ascii=False))
            if args.command == "run":
                return 0 if reading.status == "ok" else 2
            time.sleep(float(config["schedule"]["interval_seconds"]))
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, json.JSONDecodeError, PipelineError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
