from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class PipelineError(RuntimeError):
    pass


@dataclass
class Reading:
    timestamp: str
    status: str
    value: str
    raw_text: str
    source_path: str
    processed_path: str
    error: str = ""


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    required = ("camera", "roi", "preprocess", "ocr", "storage", "schedule")
    missing = [key for key in required if key not in config]
    if missing:
        raise PipelineError(f"Missing config sections: {', '.join(missing)}")
    return config


def run_checked(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise PipelineError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise PipelineError(f"{command[0]} failed: {detail}") from exc


def apply_camera_controls(config: dict[str, Any]) -> None:
    camera = config["camera"]
    controls = camera.get("controls", {})
    if not controls:
        return
    command = ["v4l2-ctl", "-d", camera["device"]]
    command.extend(f"--set-ctrl={name}={value}" for name, value in controls.items())
    run_checked(command)


def capture(config: dict[str, Any], destination: Path) -> None:
    camera = config["camera"]
    device = Path(camera["device"])
    if not device.exists():
        raise PipelineError(f"Camera device does not exist: {device}")
    apply_camera_controls(config)
    run_checked([
        "ffmpeg", "-loglevel", "error", "-f", "v4l2",
        "-input_format", str(camera["input_format"]),
        "-video_size", f"{camera['width']}x{camera['height']}",
        "-framerate", str(camera["framerate"]), "-i", str(device),
        "-frames:v", "1", "-y", str(destination),
    ])


def build_video_filter(config: dict[str, Any]) -> str:
    roi = config["roi"]
    preprocessing = config["preprocess"]
    filters = [f"crop={roi['width']}:{roi['height']}:{roi['x']}:{roi['y']}"]
    scale = int(roi.get("scale", 1))
    if scale > 1:
        filters.append(f"scale=iw*{scale}:ih*{scale}:flags=lanczos")
    if preprocessing.get("grayscale", True):
        filters.append("format=gray")
    contrast = float(preprocessing.get("contrast", 1.0))
    brightness = float(preprocessing.get("brightness", 0.0))
    if contrast != 1.0 or brightness != 0.0:
        filters.append(f"eq=contrast={contrast}:brightness={brightness}")
    if preprocessing.get("sharpen", False):
        filters.append("unsharp=5:5:1.0:5:5:0.0")
    return ",".join(filters)


def preprocess(config: dict[str, Any], source: Path, destination: Path) -> None:
    run_checked([
        "ffmpeg", "-loglevel", "error", "-i", str(source),
        "-vf", build_video_filter(config), "-y", str(destination),
    ])


def recognize(config: dict[str, Any], image: Path) -> str:
    ocr = config["ocr"]
    command = [
        "tesseract", str(image), "stdout", "-l", str(ocr.get("language", "eng")),
        "--psm", str(ocr.get("psm", 6)), "--dpi", str(ocr.get("dpi", 300)),
    ]
    whitelist = str(ocr.get("whitelist", ""))
    if whitelist:
        command.extend(["-c", f"tessedit_char_whitelist={whitelist}"])
    return run_checked(command).stdout.strip()


def validate(config: dict[str, Any], raw_text: str) -> tuple[str, str]:
    normalized = " ".join(raw_text.split())
    if not normalized:
        return "ocr_failed", ""
    pattern = str(config.get("validation", {}).get("pattern", ""))
    if not pattern:
        return "ok", normalized
    match = re.search(pattern, normalized)
    if not match:
        return "ocr_failed", ""
    return "ok", match.group(0)


def append_csv(path: Path, reading: Reading) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(Reading.__dataclass_fields__)
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if is_new:
            writer.writeheader()
        writer.writerow(reading.__dict__)


def _escape_line(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def write_influx(config: dict[str, Any], reading: Reading) -> None:
    influx = config.get("influx", {})
    if not influx.get("enabled", False):
        return
    token_name = str(influx.get("token_env", "INFLUXDB_TOKEN"))
    token = os.environ.get(token_name)
    if not token:
        raise PipelineError(f"InfluxDB is enabled but {token_name} is not set")
    measurement = str(influx.get("measurement", "screen_ocr")).replace(" ", "\\ ")
    fields = [
        f'status="{_escape_line(reading.status)}"',
        f'raw_text="{_escape_line(reading.raw_text)}"',
    ]
    try:
        fields.append(f"value={float(reading.value)}")
    except ValueError:
        fields.append(f'value_text="{_escape_line(reading.value)}"')
    params = urllib.parse.urlencode({"org": influx["org"], "bucket": influx["bucket"], "precision": "s"})
    url = f"{str(influx['url']).rstrip('/')}/api/v2/write?{params}"
    timestamp = int(datetime.fromisoformat(reading.timestamp).timestamp())
    request = urllib.request.Request(
        url,
        data=f"{measurement} {','.join(fields)} {timestamp}".encode(),
        headers={"Authorization": f"Token {token}", "Content-Type": "text/plain"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except Exception as exc:
        raise PipelineError(f"InfluxDB write failed: {exc}") from exc


def run_once(config: dict[str, Any], config_dir: Path, input_path: Path | None = None) -> Reading:
    now = datetime.now(timezone.utc).astimezone()
    timestamp = now.isoformat(timespec="seconds")
    stamp = now.strftime("%Y%m%dT%H%M%S%z")
    storage = config["storage"]
    data_dir = Path(storage["directory"])
    if not data_dir.is_absolute():
        data_dir = config_dir / data_dir
    artifact_dir = data_dir / "artifacts" / stamp
    artifact_dir.mkdir(parents=True, exist_ok=True)
    source = artifact_dir / "source.jpg"
    processed = artifact_dir / "roi.png"
    text_path = artifact_dir / "ocr.txt"
    try:
        if input_path is None:
            capture(config, source)
        else:
            if not input_path.is_file():
                raise PipelineError(f"Input image does not exist: {input_path}")
            shutil.copy2(input_path, source)
        preprocess(config, source, processed)
        raw_text = recognize(config, processed)
        text_path.write_text(raw_text + "\n", encoding="utf-8")
        status, value = validate(config, raw_text)
        reading = Reading(timestamp, status, value, raw_text, str(source), str(processed))
    except Exception as exc:
        reading = Reading(timestamp, "error", "", "", str(source), str(processed), str(exc))
    csv_path = Path(storage.get("csv", "readings.csv"))
    if not csv_path.is_absolute():
        csv_path = data_dir / csv_path
    append_csv(csv_path, reading)
    if reading.status != "error":
        try:
            write_influx(config, reading)
        except PipelineError as exc:
            reading.error = str(exc)
            append_csv(data_dir / "influx_errors.csv", reading)
    if reading.status == "ok" and not storage.get("keep_success_artifacts", True):
        shutil.rmtree(artifact_dir)
    return reading


def doctor(config: dict[str, Any]) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    for command in ("ffmpeg", "tesseract", "v4l2-ctl", "lsusb"):
        path = shutil.which(command)
        checks.append((command, path is not None, path or "not installed"))
    device = Path(config["camera"]["device"])
    checks.append(("camera device", device.exists(), str(device)))
    try:
        result = subprocess.run(["lsusb", "-d", "046d:0943"], text=True, capture_output=True)
        detected = result.returncode == 0 and bool(result.stdout.strip())
        checks.append(("BRIO 500 USB", detected, result.stdout.strip() or "046d:0943 not found"))
    except FileNotFoundError:
        checks.append(("BRIO 500 USB", False, "lsusb not installed"))
    return checks
