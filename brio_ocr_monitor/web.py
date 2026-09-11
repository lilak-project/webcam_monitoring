from __future__ import annotations

import argparse
import csv
import errno
import hmac
import ipaddress
import json
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .pipeline import PipelineError, camera_input_args, load_config, run_once
from .monitor import DisplayMonitor, save_capture_pair


class CameraStream:
    """Read one camera process and share its latest JPEG frame."""

    def __init__(self, camera_config: dict[str, Any], stream_fps: float = 10.0):
        self.config = camera_config
        self.stream_fps = stream_fps
        self.frame: bytes | None = None
        self.sequence = 0
        self.frame_time: float | None = None
        self.last_error = ""
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.enabled_event = threading.Event()
        self.enabled_event.set()
        self.process: subprocess.Popen[bytes] | None = None
        self.thread = threading.Thread(target=self._run, name="camera-stream", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        with self.condition:
            self.condition.notify_all()

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self.enabled_event.set()
            return
        self.enabled_event.clear()
        process = self.process
        if process and process.poll() is None:
            process.terminate()
        with self.condition:
            self.frame = None
            self.frame_time = None
            self.condition.notify_all()

    def _command(self) -> list[str]:
        camera = self.config
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            *camera_input_args(camera),
            "-vf", f"fps={self.stream_fps}", "-f", "image2pipe",
            "-vcodec", "mjpeg", "-q:v", "5", "-",
        ]

    def _run(self) -> None:
        while not self.stop_event.is_set():
            if not self.enabled_event.is_set():
                self.stop_event.wait(.25)
                continue
            try:
                self.process = subprocess.Popen(
                    self._command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                error_thread = threading.Thread(
                    target=self._read_errors, args=(self.process,), daemon=True
                )
                error_thread.start()
                self._read_frames(self.process)
                if self.process.returncode not in (None, 0) and not self.last_error:
                    self.last_error = f"ffmpeg exited with code {self.process.returncode}"
            except FileNotFoundError:
                self.last_error = "ffmpeg를 찾을 수 없습니다"
                return
            except Exception as exc:
                self.last_error = str(exc)
            if not self.stop_event.wait(3):
                self.last_error = self.last_error or "카메라 연결 재시도 중"

    def _read_errors(self, process: subprocess.Popen[bytes]) -> None:
        if process.stderr is None:
            return
        for line in iter(process.stderr.readline, b""):
            message = line.decode(errors="replace").strip()
            if message:
                self.last_error = message

    def _read_frames(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdout is None:
            return
        buffer = bytearray()
        while not self.stop_event.is_set():
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 2:
                        del buffer[:-2]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start:
                        del buffer[:start]
                    break
                frame = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                if self.enabled_event.is_set():
                    with self.condition:
                        self.frame = frame
                        self.sequence += 1
                        self.frame_time = time.time()
                        self.last_error = ""
                        self.condition.notify_all()
        process.wait()

    def wait_for_frame(self, after: int = -1, timeout: float = 5.0) -> tuple[int, bytes | None]:
        with self.condition:
            self.condition.wait_for(
                lambda: self.sequence > after or self.stop_event.is_set(), timeout=timeout
            )
            return self.sequence, self.frame

    def snapshot(self) -> bytes:
        with self.condition:
            if self.frame is None or self.frame_time is None or time.time() - self.frame_time >= 5:
                raise PipelineError("아직 카메라 프레임을 받지 못했습니다")
            return self.frame

    def status(self) -> dict[str, Any]:
        age = None if self.frame_time is None else round(time.time() - self.frame_time, 1)
        return {
            "connected": self.enabled_event.is_set() and self.frame is not None and age is not None and age < 5,
            "enabled": self.enabled_event.is_set(),
            "device": self.config["device"],
            "resolution": f"{self.config['width']}x{self.config['height']}",
            "stream_fps": self.stream_fps,
            "frame_age_seconds": age,
            "last_error": self.last_error,
        }


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        config: dict[str, Any],
        config_path: Path,
        camera: CameraStream,
        project_dir: Path,
    ):
        super().__init__(address, handler)
        self.config = config
        self.config_path = config_path
        self.camera = camera
        self.project_dir = project_dir
        self.config_lock = threading.Lock()
        self.perspective_preview: bytes | None = None
        self.last_snapshot_paths: dict[str, str | None] = {}
        self.monitor = (DisplayMonitor(camera, config["display_monitor"], config_path.parent, self.data_dir)
                        if config.get("display_monitor", {}).get("enabled") else None)

    @property
    def data_dir(self) -> Path:
        path = Path(self.config["storage"]["directory"])
        return path if path.is_absolute() else self.config_path.parent / path

    def save_snapshot(self) -> Path:
        now = datetime.now().astimezone()
        directory = self.data_dir / "snapshots" / now.strftime("%Y%m%d")
        monitor_config = (self.monitor._capture_config() if self.monitor
                          else self.config.get("display_monitor", {}))
        paths = save_capture_pair(self.camera.snapshot(), monitor_config,
                                  directory, f"{now:%Y%m%d-%H%M%S-%f}")
        self.last_snapshot_paths = paths
        return Path(paths["original_path"])

    def save_config(self) -> None:
        with self.config_lock:
            temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(self.config_path)

    def update_monitor_schedule(self, value: dict[str, Any]) -> dict[str, Any]:
        if self.monitor is None:
            raise PipelineError("자동 판독 설정이 없습니다")
        allowed = {"analysis_enabled", "analysis_interval_seconds",
                   "capture_enabled", "capture_interval_seconds", "voice_beam_enabled",
                   "voice_current_enabled", "voice_hourly_enabled"}
        if not isinstance(value, dict) or not set(value).issubset(allowed):
            raise PipelineError("올바른 사진/분석 주기 설정이 필요합니다")
        status = self.monitor.update_schedule(value)
        self.save_config()
        return status

    def set_service_enabled(self, enabled: bool) -> dict[str, Any]:
        if enabled:
            self.camera.set_enabled(True)
            if self.monitor:
                self.monitor.set_service_enabled(True)
        else:
            if self.monitor:
                self.monitor.set_service_enabled(False)
            self.camera.set_enabled(False)
        status = self.monitor.status() if self.monitor else {"available": False}
        status["camera_enabled"] = self.camera.enabled_event.is_set()
        return status

    def update_dooray(self, value: dict[str, Any]) -> dict[str, Any]:
        if self.monitor is None:
            raise PipelineError("자동 판독 설정이 없습니다")
        result = self.monitor.update_dooray(value)
        self.save_config()
        return result

    def perspective(self, value: dict[str, Any], save: bool = False) -> dict[str, Any]:
        try:
            points = [[float(coordinate) for coordinate in point] for point in value["points"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError("원근 보정에는 네 점의 좌표가 필요합니다") from exc
        if len(points) != 4 or any(len(point) != 2 for point in points):
            raise PipelineError("좌상, 우상, 우하, 좌하 순서의 네 점이 필요합니다")
        import cv2
        import numpy as np
        frame = self.camera.snapshot()
        image = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
        source = np.float32(points)
        height, width = image.shape[:2]
        if (not np.isfinite(source).all() or (source < 0).any()
                or (source[:, 0] >= width).any() or (source[:, 1] >= height).any()
                or not cv2.isContourConvex(source.astype(np.int32))
                or cv2.contourArea(source, oriented=True) < 1000):
            raise PipelineError("네 점이 영상 안에서 겹치지 않는 볼록 사각형을 이루어야 합니다")
        monitor_config = self.config.get("display_monitor", {})
        output = value.get("size", monitor_config.get("size", [1400, 600]))
        try:
            output_width, output_height = (int(output[0]), int(output[1]))
        except (TypeError, ValueError, IndexError) as exc:
            raise PipelineError("보정 화면 크기가 올바르지 않습니다") from exc
        if not 100 <= output_width <= 3840 or not 100 <= output_height <= 2160:
            raise PipelineError("보정 화면 크기는 100~3840 × 100~2160 범위여야 합니다")
        target = np.float32([[0, 0], [output_width - 1, 0],
                             [output_width - 1, output_height - 1], [0, output_height - 1]])
        corrected = cv2.warpPerspective(image, cv2.getPerspectiveTransform(source, target),
                                        (output_width, output_height))
        ok, encoded = cv2.imencode(".jpg", corrected, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            raise PipelineError("보정 화면을 만들지 못했습니다")
        self.perspective_preview = encoded.tobytes()
        if save:
            reference = Path(monitor_config.get("reference", "data/screen-reference.jpg"))
            if not reference.is_absolute():
                reference = self.config_path.parent / reference
            reference.parent.mkdir(parents=True, exist_ok=True)
            reference.write_bytes(frame)
            monitor_config["corners"] = [[round(v, 1) for v in point] for point in points]
            monitor_config["size"] = [output_width, output_height]
            self.config["display_monitor"] = monitor_config
            if self.monitor:
                self.monitor.tracker = None
            self.save_config()
        return {"ok": True, "points": points, "size": [output_width, output_height], "saved": save}

    def readings(self, limit: int) -> list[dict[str, str]]:
        csv_path = Path(self.config["storage"].get("csv", "readings.csv"))
        if not csv_path.is_absolute():
            csv_path = self.data_dir / csv_path
        if not csv_path.is_file():
            return []
        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        return rows[-limit:][::-1]

    def monitor_artifacts(self, limit: int = 20) -> list[dict[str, Any]]:
        display_dir = self.data_dir / "display"
        if not display_dir.is_dir():
            return []
        items = []
        pattern = re.compile(r"\d{8}T\d{6}-\d{6}[+-]\d{4}")
        for directory in sorted(display_dir.iterdir(), reverse=True):
            result_path = directory / "result.json"
            if (not directory.is_dir() or not pattern.fullmatch(directory.name)
                    or not result_path.is_file() or not (directory / "aligned.jpg").is_file()):
                continue
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            current = next((field for field in result.get("fields", [])
                            if field.get("key") == "current_ua"), None)
            fields = {field.get("key"): {
                          "value": field.get("value"),
                          "status": field.get("status", "uncertain"),
                          "unit": field.get("unit", ""),
                          "decimals": field.get("decimals", 0),
                      } for field in result.get("fields", []) if field.get("key")}
            items.append({"artifact": directory.name,
                          "timestamp": result.get("timestamp", directory.name),
                          "status": result.get("status", "unknown"),
                          "error": result.get("error", ""),
                          "current": current, "fields": fields})
            if len(items) >= limit:
                break
        return items

    def monitor_artifact_path(self, artifact: str, filename: str) -> Path:
        if not re.fullmatch(r"\d{8}T\d{6}-\d{6}[+-]\d{4}", artifact):
            raise PipelineError("올바르지 않은 분석 기록입니다")
        allowed = {"aligned.jpg", "source.jpg", "current_ua-gray.png"}
        if filename not in allowed:
            raise PipelineError("허용되지 않은 분석 이미지입니다")
        path = self.data_dir / "display" / artifact / filename
        if not path.is_file():
            raise PipelineError("분석 이미지를 찾을 수 없습니다")
        return path

    def update_roi(self, value: dict[str, Any]) -> dict[str, int]:
        try:
            roi = {name: int(value[name]) for name in ("x", "y", "width", "height")}
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError("ROI에는 x, y, width, height 정수가 필요합니다") from exc
        camera = self.config["camera"]
        if roi["x"] < 0 or roi["y"] < 0 or roi["width"] < 10 or roi["height"] < 10:
            raise PipelineError("ROI 위치는 0 이상, 크기는 10픽셀 이상이어야 합니다")
        if roi["x"] + roi["width"] > int(camera["width"]):
            raise PipelineError("ROI가 영상의 오른쪽 범위를 벗어났습니다")
        if roi["y"] + roi["height"] > int(camera["height"]):
            raise PipelineError("ROI가 영상의 아래쪽 범위를 벗어났습니다")
        roi["scale"] = int(self.config["roi"].get("scale", 4))
        self.config["roi"] = roi
        self.save_config()
        return roi


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.client_address[0]} - {fmt % args}")

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self._json({"ok": False, "error": message}, status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65536:
            raise PipelineError("올바른 JSON 요청 본문이 필요합니다")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise PipelineError("JSON 객체가 필요합니다")
        return value

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        request_host = self.headers.get("Host", "").split(":", 1)[0].lower()
        public_host = urlparse(self.server.config.get("display_monitor", {}).get(
            "dooray", {}).get("public_base_url", "")).hostname
        try:
            address = ipaddress.ip_address(request_host)
            is_public = not (address.is_private or address.is_loopback)
        except ValueError:
            is_public = bool(request_host and request_host != "localhost"
                             and not request_host.endswith(".local"))
        is_public = is_public or request_host.endswith(".trycloudflare.com") or (
            public_host is not None and request_host == public_host.lower())
        if is_public:
            supplied = parse_qs(parsed.query).get("token", [""])[0]
            expected = self.server.config.get("display_monitor", {}).get(
                "dooray", {}).get("photo_access_token", "")
            if (not parsed.path.startswith("/api/snapshots/") or not expected
                    or not hmac.compare_digest(supplied, expected)):
                return self._error("사진 접근 권한이 없습니다", HTTPStatus.FORBIDDEN)
        if parsed.path == "/":
            body = (self.server.project_dir / "web" / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/status":
            status = self.server.camera.status()
            status["roi"] = self.server.config["roi"]
            self._json(status)
        elif parsed.path == "/api/readings":
            query = parse_qs(parsed.query)
            try:
                limit = min(max(int(query.get("limit", ["50"])[0]), 1), 500)
            except ValueError:
                return self._error("limit은 숫자여야 합니다")
            self._json({"readings": self.server.readings(limit)})
        elif parsed.path == "/api/monitor":
            self._json(self.server.monitor.status() if self.server.monitor else {"available": False})
        elif parsed.path == "/api/monitor/artifacts":
            self._json({"artifacts": self.server.monitor_artifacts(20)})
        elif parsed.path.startswith("/api/monitor/artifacts/"):
            parts = parsed.path.split("/")
            if len(parts) != 6:
                return self._error("올바르지 않은 분석 이미지 주소입니다", HTTPStatus.NOT_FOUND)
            try:
                path = self.server.monitor_artifact_path(unquote(parts[4]), unquote(parts[5]))
            except PipelineError as exc:
                return self._error(str(exc), HTTPStatus.NOT_FOUND)
            body = path.read_bytes()
            content_type = "image/jpeg" if path.suffix == ".jpg" else "image/png"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/monitor/data.csv":
            path = self.server.data_dir / "display-readings.csv"
            if not path.is_file():
                return self._error("아직 기록이 없습니다", HTTPStatus.NOT_FOUND)
            if self.server.monitor:
                with self.server.monitor.records_lock:
                    body = path.read_bytes()
            else:
                body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="display-readings.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/monitor/preview.jpg":
            monitor = self.server.monitor
            latest = monitor.status()["latest"] if monitor else None
            path = (self.server.data_dir / "display" / latest["artifact"] / "aligned.jpg"
                    if latest and latest.get("artifact") else None)
            if path is None or not path.is_file():
                return self._error("보정 화면이 없습니다", HTTPStatus.NOT_FOUND)
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/perspective/preview.jpg":
            body = self.server.perspective_preview
            if body is None:
                return self._error("먼저 네 점을 선택해 미리보기를 만드세요", HTTPStatus.NOT_FOUND)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path.startswith("/api/snapshots/"):
            parts = parsed.path.split("/")
            if (len(parts) != 5 or not re.fullmatch(r"\d{8}", parts[3])
                    or not re.fullmatch(r"(?:full|selected)-\d{8}-\d{6}-\d{6}\.jpg", parts[4])):
                return self._error("올바르지 않은 사진 주소입니다", HTTPStatus.NOT_FOUND)
            root = (self.server.data_dir / "snapshots").resolve()
            path = (root / parts[3] / parts[4]).resolve()
            if root not in path.parents or not path.is_file():
                return self._error("사진을 찾을 수 없습니다", HTTPStatus.NOT_FOUND)
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/latest.jpg":
            try:
                frame = self.server.camera.snapshot()
            except PipelineError as exc:
                return self._error(str(exc), HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(frame)
        elif parsed.path == "/api/live.mjpg":
            self._stream()
        else:
            self._error("찾을 수 없습니다", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path in ("/api/service/start", "/api/service/stop"):
            try:
                self._json(self.server.set_service_enabled(path.endswith("/start")))
            except PipelineError as exc:
                self._error(str(exc), HTTPStatus.CONFLICT)
        elif path in ("/api/monitor/run", "/api/monitor/start", "/api/monitor/stop",
                    "/api/monitor/voice/test"):
            monitor = self.server.monitor
            if monitor is None:
                return self._error("자동 판독 설정이 없습니다")
            try:
                if path.endswith("/voice/test"):
                    self._json(monitor.test_voice())
                elif path.endswith("/run"):
                    self._json(monitor.request_run())
                else:
                    monitor.set_enabled(path.endswith("/start"))
                    self._json(monitor.status())
            except PipelineError as exc:
                self._error(str(exc), HTTPStatus.CONFLICT)
            except Exception as exc:
                self._error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path == "/api/monitor/settings":
            try:
                self._json(self.server.update_monitor_schedule(self._read_json()))
            except (json.JSONDecodeError, OSError, PipelineError) as exc:
                self._error(str(exc))
        elif path == "/api/monitor/dooray/settings":
            try:
                self._json(self.server.update_dooray(self._read_json()))
            except (json.JSONDecodeError, OSError, PipelineError) as exc:
                self._error(str(exc))
        elif path == "/api/monitor/dooray/test":
            try:
                if self.server.monitor is None:
                    raise PipelineError("자동 판독 설정이 없습니다")
                self._json(self.server.monitor.test_dooray())
            except PipelineError as exc:
                self._error(str(exc), HTTPStatus.BAD_GATEWAY)
        elif path == "/api/monitor/dooray/send-now":
            try:
                if self.server.monitor is None:
                    raise PipelineError("자동 판독 설정이 없습니다")
                self._json(self.server.monitor.send_dooray_now())
            except PipelineError as exc:
                self._error(str(exc), HTTPStatus.BAD_GATEWAY)
            except Exception as exc:
                self._error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path in ("/api/perspective/preview", "/api/perspective/save"):
            try:
                self._json(self.server.perspective(
                    self._read_json(), save=path.endswith("/save")))
            except (json.JSONDecodeError, OSError, PipelineError) as exc:
                self._error(str(exc))
        elif path == "/api/capture":
            try:
                saved = self.server.save_snapshot()
                self._json({"ok": True, "path": str(saved),
                            "original_path": self.server.last_snapshot_paths.get("original_path"),
                            "selected_path": self.server.last_snapshot_paths.get("selected_path")})
            except (OSError, PipelineError) as exc:
                self._error(str(exc), HTTPStatus.SERVICE_UNAVAILABLE)
        elif path == "/api/ocr":
            try:
                saved = self.server.save_snapshot()
                reading = run_once(
                    self.server.config, self.server.config_path.parent, input_path=saved
                )
                self._json({"ok": reading.status == "ok", "reading": reading.__dict__})
            except (OSError, PipelineError) as exc:
                self._error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path == "/api/roi":
            try:
                value = self._read_json()
                roi = self.server.update_roi(value)
                self._json({"ok": True, "roi": roi})
            except (json.JSONDecodeError, OSError, PipelineError) as exc:
                self._error(str(exc))
        else:
            self._error("찾을 수 없습니다", HTTPStatus.NOT_FOUND)

    def _stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        sequence = -1
        try:
            while True:
                new_sequence, frame = self.server.camera.wait_for_frame(sequence)
                if frame is None or new_sequence == sequence:
                    continue
                sequence = new_sequence
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BRIO 500 LAN web dashboard")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--stream-fps", type=float, default=10.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = args.config.resolve()
    try:
        config = load_config(config_path)
    except (OSError, ValueError, json.JSONDecodeError, PipelineError) as exc:
        print(f"설정 오류: {exc}")
        return 2
    camera = CameraStream(config["camera"], args.stream_fps)
    project_dir = Path(__file__).resolve().parent.parent
    try:
        server = DashboardServer(
            (args.host, args.port), DashboardHandler, config, config_path, camera, project_dir
        )
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f"시작 오류: {args.port} 포트를 이미 다른 프로세스가 사용 중입니다.")
            print(f"확인 명령: ss -ltnp 'sport = :{args.port}'")
            print(f"다른 포트로 실행: PORT={args.port + 1} ./scripts/web-service.sh")
            return 2
        print(f"시작 오류: {exc}")
        return 2
    stop_requested = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        if not stop_requested.is_set():
            stop_requested.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    camera.start()
    if server.monitor:
        server.monitor.thread.start()
    print(f"웹 대시보드: http://{args.host}:{args.port}")
    print("같은 네트워크에서는 http://<이 서버의 192.168.1.x 주소>:8080 으로 접속하세요.")
    try:
        server.serve_forever()
    finally:
        if server.monitor:
            server.monitor.stop_event.set()
            server.monitor.thread.join(timeout=60)
            if server.monitor.analysis_worker:
                server.monitor.analysis_worker.join(timeout=60)
        camera.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
