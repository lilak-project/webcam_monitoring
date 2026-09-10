from __future__ import annotations

import argparse
import csv
import errno
import json
import signal
import subprocess
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .pipeline import PipelineError, load_config, run_once


class CameraStream:
    """Read one V4L2 camera process and share its latest JPEG frame."""

    def __init__(self, camera_config: dict[str, Any], stream_fps: float = 10.0):
        self.config = camera_config
        self.stream_fps = stream_fps
        self.frame: bytes | None = None
        self.sequence = 0
        self.frame_time: float | None = None
        self.last_error = ""
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
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

    def _command(self) -> list[str]:
        camera = self.config
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", str(camera["input_format"]),
            "-video_size", f"{camera['width']}x{camera['height']}",
            "-framerate", str(camera["framerate"]), "-i", str(camera["device"]),
            "-vf", f"fps={self.stream_fps}", "-f", "image2pipe",
            "-vcodec", "mjpeg", "-q:v", "5", "-",
        ]

    def _run(self) -> None:
        while not self.stop_event.is_set():
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
            if self.frame is None:
                raise PipelineError("아직 카메라 프레임을 받지 못했습니다")
            return self.frame

    def status(self) -> dict[str, Any]:
        age = None if self.frame_time is None else round(time.time() - self.frame_time, 1)
        return {
            "connected": self.frame is not None and age is not None and age < 5,
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

    @property
    def data_dir(self) -> Path:
        path = Path(self.config["storage"]["directory"])
        return path if path.is_absolute() else self.config_path.parent / path

    def save_snapshot(self) -> Path:
        directory = self.data_dir / "web_captures"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"shot-{datetime.now().astimezone():%Y%m%dT%H%M%S%z}.jpg"
        path.write_bytes(self.camera.snapshot())
        return path

    def readings(self, limit: int) -> list[dict[str, str]]:
        csv_path = Path(self.config["storage"].get("csv", "readings.csv"))
        if not csv_path.is_absolute():
            csv_path = self.data_dir / csv_path
        if not csv_path.is_file():
            return []
        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        return rows[-limit:][::-1]

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
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.config_path)
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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = (self.server.project_dir / "web" / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
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
        if path == "/api/capture":
            try:
                saved = self.server.save_snapshot()
                self._json({"ok": True, "path": str(saved)})
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
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 65536:
                    raise PipelineError("올바른 ROI 요청 본문이 필요합니다")
                value = json.loads(self.rfile.read(length))
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
    print(f"웹 대시보드: http://{args.host}:{args.port}")
    print("같은 네트워크에서는 http://<이 서버의 192.168.1.x 주소>:8080 으로 접속하세요.")
    try:
        server.serve_forever()
    finally:
        camera.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
