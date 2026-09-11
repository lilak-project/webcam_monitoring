"""Scheduled, serialized OCR using the web server's shared camera."""
from __future__ import annotations

import csv
import json
import math
import re
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

from .pipeline import PipelineError
from .dooray import (build_capture_payloads, current_records, post_webhook,
                     snapshot_url, validate_https_url, webhook_profile)


def save_capture_pair(frame: bytes, monitor_config: dict, directory: Path, stamp: str):
    """Save the full frame and its configured perspective selection together."""
    directory.mkdir(parents=True, exist_ok=True)
    original = directory / f"full-{stamp}.jpg"
    original.write_bytes(frame)
    result = {"original_path": str(original), "selected_path": None}
    corners = monitor_config.get("corners")
    size = monitor_config.get("size", [1400, 600])
    if not corners:
        return result
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise PipelineError("저장 사진을 디코딩하지 못했습니다")
    source = np.float32(corners)
    width, height = int(size[0]), int(size[1])
    target = np.float32([[0, 0], [width - 1, 0],
                         [width - 1, height - 1], [0, height - 1]])
    corrected = cv2.warpPerspective(
        image, cv2.getPerspectiveTransform(source, target), (width, height))
    if monitor_config.get("trim_screen_border", True):
        from .screen import trim_screen_border
        corrected, _ = trim_screen_border(corrected)
    selected = directory / f"selected-{stamp}.jpg"
    if not cv2.imwrite(str(selected), corrected, [cv2.IMWRITE_JPEG_QUALITY, 92]):
        raise PipelineError("선택 영역 사진을 저장하지 못했습니다")
    result["selected_path"] = str(selected)
    return result


def display_record_columns(config: dict) -> list[str]:
    columns = ["timestamp", "status", "error", "tracking_shift_px"]
    for field in config.get("fields", []):
        columns += [field["key"], field["key"] + "_status"]
    return columns


def ensure_display_csv_schema(path: Path, columns: list[str]) -> None:
    """Migrate rows written after the configured field list changed."""
    if not path.is_file():
        return
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    if not rows or rows[0] == columns:
        return
    old_columns = rows[0]
    temporary = path.with_suffix(path.suffix + ".schema.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for values in rows[1:]:
            # Rows appended with the newer schema have its shorter field count,
            # even though the file still has the older header.
            source_columns = columns if len(values) == len(columns) else old_columns
            source = dict(zip(source_columns, values))
            writer.writerow({column: source.get(column, "") for column in columns})
    temporary.replace(path)


class DisplayMonitor:
    def __init__(self, camera, config: dict, config_dir: Path, data_dir: Path):
        self.camera = camera
        self.config = config
        self.config_dir = config_dir
        self.data_dir = data_dir
        self.lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.records_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.service_enabled = True
        legacy_enabled = bool(config.get("auto_start", False))
        legacy_interval = float(config.get("interval_seconds", 30))
        self.analysis_enabled = bool(config.get("analysis_enabled", legacy_enabled))
        self.analysis_interval = self._interval(config.get("analysis_interval_seconds", legacy_interval))
        self.capture_enabled = bool(config.get("capture_enabled", False))
        self.capture_interval = self._interval(config.get("capture_interval_seconds", 60))
        self.latest = None
        self.last_capture_corners = self._load_last_tracking_corners()
        self.latest_capture = None
        self.busy = False
        self.schedule_version = 0
        self.tracker = None
        self.analysis_worker = None
        self.manual_requested = False
        legacy_voice = bool(config.get("voice_alerts_enabled", True))
        self.voice_beam_enabled = bool(config.get("voice_beam_enabled", legacy_voice))
        self.voice_current_enabled = bool(config.get("voice_current_enabled", legacy_voice))
        self.voice_hourly_enabled = bool(config.get("voice_hourly_enabled", True))
        self.alert_states = {"beam": None, "current": None}
        self.latest_alert = None
        self.voice_lock = threading.Lock()
        self.voice_chime_path = data_dir / "voice-chime.wav"
        self._ensure_voice_chime()
        self.dooray_lock = threading.Lock()
        self.dooray_config = config.setdefault("dooray", {})
        self.dooray_config.setdefault("photo_access_token", secrets.token_urlsafe(24))
        self.dooray_state_path = data_dir / "dooray-state.json"
        ensure_display_csv_schema(data_dir / "display-readings.csv",
                                  display_record_columns(self.config))
        self.latest_dooray = None
        try:
            self.dooray_last_sent_at = json.loads(
                self.dooray_state_path.read_text(encoding="utf-8")) .get("last_sent_at")
        except (OSError, ValueError, AttributeError):
            self.dooray_last_sent_at = None
        self.last_hour_key = datetime.now().astimezone().strftime("%Y%m%d%H")
        self.thread = threading.Thread(target=self._loop, daemon=True, name="display-ocr")

    def status(self):
        with self.state_lock:
            profile = webhook_profile(self.dooray_config.get("webhook_url", ""))
            return {"available": True, "enabled": self.analysis_enabled,
                    "service_enabled": self.service_enabled,
                    "analysis_enabled": self.analysis_enabled,
                    "analysis_interval_seconds": self.analysis_interval,
                    "capture_enabled": self.capture_enabled,
                    "capture_interval_seconds": self.capture_interval,
                    "voice_beam_enabled": self.voice_beam_enabled,
                    "voice_current_enabled": self.voice_current_enabled,
                    "voice_hourly_enabled": self.voice_hourly_enabled,
                    "latest_voice_alert": self.latest_alert,
                    "dooray": {"enabled": bool(self.dooray_config.get("enabled", False)),
                               "configured": bool(self.dooray_config.get("webhook_url")),
                               "host": profile["host"], "masked_url": profile["masked_url"],
                               "public_base_url": self.dooray_config.get("public_base_url", ""),
                               "photo_ready": bool(self.dooray_config.get("public_base_url")),
                               "last_sent_at": self.dooray_last_sent_at,
                               "latest": self.latest_dooray},
                    "busy": self.busy, "latest": self.latest,
                    "latest_capture": self.latest_capture,
                    "perspective": {"points": self.config.get("corners", []),
                                    "size": self.config.get("size", [1400, 600])}}

    @staticmethod
    def _interval(value):
        try:
            interval = float(value)
        except (TypeError, ValueError) as exc:
            raise PipelineError("기록 주기는 숫자여야 합니다") from exc
        if not 2 <= interval <= 86400:
            raise PipelineError("기록 주기는 2초 이상 86400초 이하여야 합니다")
        return interval

    def update_schedule(self, values):
        analysis_enabled = self.analysis_enabled
        capture_enabled = self.capture_enabled
        analysis_interval = self.analysis_interval
        capture_interval = self.capture_interval
        voice_names = ("voice_beam_enabled", "voice_current_enabled", "voice_hourly_enabled")
        for name in ("analysis_enabled", "capture_enabled", *voice_names):
            if name in values and not isinstance(values[name], bool):
                raise PipelineError("시작/중지 설정은 true 또는 false여야 합니다")
        if "analysis_enabled" in values:
            analysis_enabled = values["analysis_enabled"]
        if "capture_enabled" in values:
            capture_enabled = values["capture_enabled"]
        voice_beam_enabled = values.get("voice_beam_enabled", self.voice_beam_enabled)
        voice_current_enabled = values.get("voice_current_enabled", self.voice_current_enabled)
        voice_hourly_enabled = values.get("voice_hourly_enabled", self.voice_hourly_enabled)
        if "analysis_interval_seconds" in values:
            analysis_interval = self._interval(values["analysis_interval_seconds"])
        if "capture_interval_seconds" in values:
            capture_interval = self._interval(values["capture_interval_seconds"])
        with self.state_lock:
            self.analysis_enabled = analysis_enabled
            self.capture_enabled = capture_enabled
            self.analysis_interval = analysis_interval
            self.capture_interval = capture_interval
            self.voice_beam_enabled = voice_beam_enabled
            self.voice_current_enabled = voice_current_enabled
            self.voice_hourly_enabled = voice_hourly_enabled
            self.config.update(
                analysis_enabled=self.analysis_enabled,
                analysis_interval_seconds=self.analysis_interval,
                capture_enabled=self.capture_enabled,
                capture_interval_seconds=self.capture_interval,
                voice_beam_enabled=self.voice_beam_enabled,
                voice_current_enabled=self.voice_current_enabled,
                voice_hourly_enabled=self.voice_hourly_enabled,
            )
            self.schedule_version += 1
        return self.status()

    def set_enabled(self, enabled):
        return self.update_schedule({"analysis_enabled": enabled})

    def set_service_enabled(self, enabled: bool):
        with self.state_lock:
            self.service_enabled = bool(enabled)
            self.schedule_version += 1
        return self.status()

    def update_dooray(self, values):
        if not isinstance(values, dict):
            raise PipelineError("Dooray 설정이 필요합니다")
        enabled = values.get("enabled", self.dooray_config.get("enabled", False))
        if not isinstance(enabled, bool):
            raise PipelineError("Dooray 사용 설정은 true 또는 false여야 합니다")
        webhook = values.get("webhook_url", "")
        if webhook:
            webhook = validate_https_url(webhook, "Dooray Webhook URL", required=True)
        else:
            webhook = self.dooray_config.get("webhook_url", "")
        public = validate_https_url(values.get(
            "public_base_url", self.dooray_config.get("public_base_url", "")), "사진 공개 주소")
        if enabled and not webhook:
            raise PipelineError("Dooray Webhook URL을 먼저 입력하세요")
        self.dooray_config.update(enabled=enabled, webhook_url=webhook, public_base_url=public)
        return self.status()["dooray"]

    def test_dooray(self):
        webhook = self.dooray_config.get("webhook_url", "")
        if not webhook:
            raise PipelineError("Dooray Webhook URL을 먼저 저장하세요")
        post_webhook(webhook, {"botName": "Webcam Monitor", "text": "Dooray 웹훅 테스트가 정상적으로 도착했습니다."})
        self.latest_dooray = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                              "status": "ok", "message": "테스트 전송 완료"}
        return {"ok": True}

    def send_dooray_now(self):
        if not self.dooray_config.get("webhook_url"):
            raise PipelineError("Dooray Webhook URL을 먼저 저장하세요")
        now = datetime.now().astimezone()
        directory = self.data_dir / "snapshots" / now.strftime("%Y%m%d")
        paths = save_capture_pair(self.camera.snapshot(), self._capture_config(), directory,
                                  f"{now:%Y%m%d-%H%M%S-%f}")
        result = self._publish_dooray(now, paths, wait=True)
        if not result or result.get("status") != "ok":
            raise PipelineError((result or {}).get("message", "Dooray 전송에 실패했습니다"))
        return {"ok": True, "dooray": result, **paths}

    def _loop(self):
        next_analysis = next_capture = 0.0
        schedule_version = -1
        analysis_was_running = False
        while not self.stop_event.wait(.25):
            self._announce_hour()
            now = time.monotonic()
            with self.state_lock:
                analysis_enabled = self.analysis_enabled and self.service_enabled
                capture_enabled = self.capture_enabled and self.service_enabled
                analysis_interval = self.analysis_interval
                capture_interval = self.capture_interval
                current_version = self.schedule_version
            if current_version != schedule_version:
                next_analysis = next_capture = 0.0
                schedule_version = current_version
            if capture_enabled and now >= next_capture:
                try:
                    self.capture()
                except Exception as exc:
                    with self.state_lock:
                        self.latest_capture = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                                               "status": "error", "error": str(exc)}
                next_capture = time.monotonic() + capture_interval
            elif not capture_enabled:
                next_capture = 0.0
            now = time.monotonic()
            worker_running = self.analysis_worker is not None and self.analysis_worker.is_alive()
            if analysis_was_running and not worker_running:
                next_analysis = now + analysis_interval
                analysis_was_running = False
            if analysis_enabled and now >= next_analysis:
                if not worker_running:
                    self.analysis_worker = threading.Thread(
                        target=self._scheduled_run, daemon=True, name="display-analysis")
                    self.analysis_worker.start()
                    analysis_was_running = True
                    next_analysis = float("inf")
                else:
                    next_analysis = time.monotonic() + 1
            elif not analysis_enabled:
                next_analysis = 0.0

    def _scheduled_run(self):
        while not self.stop_event.is_set():
            try:
                self.run()
            except Exception as exc:
                with self.state_lock:
                    self.latest = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                                   "status": "error", "fields": [], "error": str(exc)}
            with self.state_lock:
                rerun = self.manual_requested
                self.manual_requested = False
            if not rerun:
                break

    def request_run(self):
        queued = False
        with self.state_lock:
            if self.busy or (self.analysis_worker is not None and self.analysis_worker.is_alive()):
                self.manual_requested = True
                queued = True
        if queued:
            result = self.status()
            result["queued"] = True
            return result
        return self.run()

    def capture(self):
        now = datetime.now().astimezone()
        directory = self.data_dir / "snapshots" / now.strftime("%Y%m%d")
        paths = save_capture_pair(self.camera.snapshot(), self._capture_config(), directory,
                                  f"{now:%Y%m%d-%H%M%S-%f}")
        result = {"timestamp": now.isoformat(timespec="seconds"), "status": "ok",
                  "path": paths["original_path"], **paths, "error": ""}
        with self.state_lock:
            self.latest_capture = result
        self._queue_dooray(now, paths)
        return result

    def _capture_config(self):
        """Use the screen corners established by the most recent analysis."""
        with self.state_lock:
            corners = self.last_capture_corners
        capture_config = dict(self.config)
        if corners:
            capture_config["corners"] = corners
        return capture_config

    def _load_last_tracking_corners(self):
        display_dir = self.data_dir / "display"
        if display_dir.is_dir():
            for directory in sorted(display_dir.iterdir(), reverse=True):
                result_path = directory / "result.json"
                if not result_path.is_file():
                    continue
                try:
                    tracking = json.loads(result_path.read_text(encoding="utf-8")).get("tracking") or {}
                    if tracking.get("corners"):
                        return tracking["corners"]
                except (OSError, ValueError, AttributeError):
                    continue
        return self.config.get("corners")

    def _queue_dooray(self, captured_at, paths):
        if not self.dooray_config.get("enabled") or not self.dooray_config.get("webhook_url"):
            return
        threading.Thread(target=self._publish_dooray, args=(captured_at, paths), daemon=True,
                         name="dooray-push").start()

    def _publish_dooray(self, captured_at, paths, wait=False):
        if not self.dooray_lock.acquire(blocking=wait):
            return
        try:
            records = current_records(self.data_dir / "display-readings.csv", self.dooray_last_sent_at)
            public = self.dooray_config.get("public_base_url", "")
            selected = paths.get("selected_path")
            if public and selected:
                image_url = snapshot_url(public, selected,
                                         self.dooray_config["photo_access_token"])
            else:
                image_url = ""
            payloads = build_capture_payloads(captured_at, records, image_url)
            for index, payload in enumerate(payloads):
                post_webhook(self.dooray_config["webhook_url"], payload)
                if index + 1 < len(payloads):
                    time.sleep(.2)
            sent_at = captured_at.isoformat(timespec="seconds")
            temporary = self.dooray_state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"last_sent_at": sent_at}, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
            temporary.replace(self.dooray_state_path)
            with self.state_lock:
                self.dooray_last_sent_at = sent_at
                message = f"Current 기록 {len(records)}개를 {len(payloads)}개 메시지로 전송"
                if image_url:
                    message = f"사진과 {message}"
                elif selected:
                    message += " · 사진 생략(공개 주소 필요)"
                self.latest_dooray = {"timestamp": sent_at, "status": "ok", "message": message}
                return self.latest_dooray
        except Exception as exc:
            with self.state_lock:
                self.latest_dooray = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                                      "status": "error", "message": str(exc)}
                return self.latest_dooray
        finally:
            self.dooray_lock.release()

    def run(self):
        if not self.lock.acquire(blocking=False):
            raise PipelineError("OCR 처리 중입니다")
        with self.state_lock:
            self.busy = True
        try:
            return self._run()
        finally:
            with self.state_lock:
                self.busy = False
            self.lock.release()

    def _run(self):
        from .screen import ScreenTracker, read_fields_ensemble
        import cv2
        import numpy as np
        now = datetime.now().astimezone()
        stamp = now.strftime("%Y%m%dT%H%M%S-%f%z")
        directory = self.data_dir / "display" / stamp
        directory.mkdir(parents=True, exist_ok=True)
        result = {"timestamp": now.isoformat(timespec="seconds"), "status": "error", "fields": [],
                  "tracking": None, "error": "", "artifact": stamp}
        try:
            if not self.camera.status()["connected"]:
                raise PipelineError("카메라 영상이 없거나 5초 이상 지연되었습니다")
            frame = self.camera.snapshot()
            (directory / "source.jpg").write_bytes(frame)
            image = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
            if self.tracker is None:
                reference = Path(self.config["reference"])
                if not reference.is_absolute():
                    reference = self.config_dir / reference
                self.tracker = ScreenTracker(reference, self.config["corners"],
                                             self.config.get("size", [1400, 600]),
                                             self.config.get("max_shift_px", 100))
            aligned, result["tracking"] = self.tracker.align(image)
            with self.state_lock:
                self.last_capture_corners = result["tracking"]["corners"]
            cv2.imwrite(str(directory / "aligned.jpg"), aligned)
            result["fields"] = read_fields_ensemble(aligned, self.config["fields"], directory)
            result["status"] = "ok" if all(f["status"] == "ok" for f in result["fields"]) else "partial"
            self._announce_transitions(result["fields"])
        except Exception as exc:
            result["error"] = str(exc)
        (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        path = self.data_dir / "display-readings.csv"
        columns = display_record_columns(self.config)
        new = not path.exists()
        row = {"timestamp": result["timestamp"], "status": result["status"], "error": result["error"],
               "tracking_shift_px": (result["tracking"] or {}).get("max_shift_px", "")}
        for field in self.config["fields"]:
            row[field["key"] + "_status"] = "unavailable"
        for field in result["fields"]:
            row[field["key"]] = field["value"] if field["value"] is not None else ""
            row[field["key"] + "_status"] = field["status"]
        with self.records_lock, path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            if new:
                writer.writeheader()
            writer.writerow(row)
        with self.state_lock:
            self.latest = result
        keep = max(1, int(self.config.get("keep_artifacts", 200)))
        artifacts = sorted(p for p in (self.data_dir / "display").iterdir()
                           if p.is_dir() and not p.is_symlink()
                           and re.fullmatch(r"\d{8}T\d{6}-\d{6}[+-]\d{4}", p.name)
                           and (p / "result.json").is_file())
        for old in artifacts[:-keep]:
            shutil.rmtree(old)
        return result

    def _speak(self, message: str):
        if sys.platform != "darwin" or not Path("/usr/bin/say").is_file():
            return
        def play():
            with self.voice_lock:
                if self.voice_chime_path.is_file() and Path("/usr/bin/afplay").is_file():
                    subprocess.run(["/usr/bin/afplay", str(self.voice_chime_path)],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, check=False)
                subprocess.run(["/usr/bin/say", message], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)

        threading.Thread(target=play, daemon=True, name="voice-alert").start()

    def _ensure_voice_chime(self):
        if self.voice_chime_path.is_file():
            return
        self.voice_chime_path.parent.mkdir(parents=True, exist_ok=True)
        rate = 44100
        samples = bytearray()
        for frequency, duration in ((523.25, .30), (392.00, .42)):
            count = int(rate * duration)
            for index in range(count):
                elapsed = index / rate
                attack = min(1.0, elapsed / .012)
                decay = math.exp(-4.0 * elapsed / duration)
                value = attack * decay * (
                    .72 * math.sin(2 * math.pi * frequency * elapsed)
                    + .20 * math.sin(4 * math.pi * frequency * elapsed))
                samples.extend(struct.pack("<h", int(26000 * value)))
            samples.extend(b"\0\0" * int(rate * .07))
        temporary = self.voice_chime_path.with_suffix(".tmp.wav")
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(samples)
        temporary.replace(self.voice_chime_path)

    def test_voice(self):
        self._speak("음성 알림 테스트입니다")
        return {"ok": True}

    def _announce_hour(self):
        now = datetime.now().astimezone()
        hour_key = now.strftime("%Y%m%d%H")
        if hour_key == self.last_hour_key:
            return
        self.last_hour_key = hour_key
        if self.voice_hourly_enabled:
            self._record_and_speak(self._hour_message(now.hour))

    @staticmethod
    def _hour_message(hour: int) -> str:
        if hour == 0:
            return "자정입니다"
        if hour == 12:
            return "정오입니다"
        names = {1: "한", 2: "두", 3: "세", 4: "네", 5: "다섯", 6: "여섯",
                 7: "일곱", 8: "여덟", 9: "아홉", 10: "열", 11: "열한"}
        period = "오전" if hour < 12 else "오후"
        return f"{period} {names[hour if hour < 12 else hour - 12]} 시입니다"

    def _record_and_speak(self, message):
        self.latest_alert = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                             "message": message}
        self._speak(message)

    def _announce_transitions(self, fields):
        beam = next((field for field in fields if field["key"] == "beam" and field["status"] == "ok"), None)
        current = next((field for field in fields if field["key"] == "current_ua" and field["status"] == "ok"), None)
        next_states = {
            "beam": beam["value"] if beam and beam["value"] in ("ON", "OFF") else None,
            "current": ("OFF" if current and current["value"] == 0 else
                        "ON" if current and current["value"] >= 1 else None),
        }
        messages = []
        for key, value in next_states.items():
            if value is None:
                continue
            previous = self.alert_states[key]
            enabled = self.voice_beam_enabled if key == "beam" else self.voice_current_enabled
            if enabled and previous is not None and previous != value:
                subject = "빔이" if key == "beam" else "커런트가"
                messages.append(f"{subject} {'켜졌습니다' if value == 'ON' else '꺼졌습니다'}")
            self.alert_states[key] = value
        if messages:
            self._record_and_speak(". ".join(messages))
