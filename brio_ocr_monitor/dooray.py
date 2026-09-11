"""Dooray incoming-webhook publishing for automatic monitor captures."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .pipeline import PipelineError


def validate_https_url(value: str, label: str, required: bool = False) -> str:
    value = value.strip()
    if not value and not required:
        return ""
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise PipelineError(f"{label}는 https:// 주소여야 합니다")
    return value.rstrip("/")


def webhook_profile(url: str) -> dict:
    if not url:
        return {"host": "", "masked_url": ""}
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    masked = []
    for index, part in enumerate(parts):
        if index == len(parts) - 1:
            masked.append("••••••••")
        elif part.isdigit() and len(part) > 10:
            masked.append(f"{part[:4]}…{part[-4:]}")
        else:
            masked.append(part)
    return {"host": parsed.netloc, "masked_url": f"{parsed.scheme}://{parsed.netloc}/{'/'.join(masked)}"}


def current_records(csv_path: Path, since: str | None) -> list[dict[str, str]]:
    if not csv_path.is_file():
        return []
    since_time = datetime.fromisoformat(since) if since else None
    with csv_path.open(newline="", encoding="utf-8") as handle:
        records = []
        for row in csv.DictReader(handle):
            try:
                timestamp = datetime.fromisoformat(row.get("timestamp", ""))
            except ValueError:
                continue
            if since_time is not None and timestamp <= since_time:
                continue
            records.append({"timestamp": row["timestamp"], "value": row.get("current_ua", ""),
                            "status": row.get("current_ua_status", "")})
        return records


def format_current_records(records: list[dict[str, str]]) -> str:
    if not records:
        return "해당 기간의 Current 분석 기록이 없습니다."
    lines = []
    for row in records:
        when = datetime.fromisoformat(row["timestamp"]).astimezone().strftime("%H:%M:%S")
        value = f"{row['value']} µA" if row["value"] else "확인 필요"
        lines.append(f"{when}  {value}")
    return "\n".join(lines)


def snapshot_url(public_base_url: str, selected_path: str, access_token: str = "") -> str:
    path = Path(selected_path)
    day = path.parent.name
    url = f"{public_base_url}/api/snapshots/{quote(day)}/{quote(path.name)}"
    return f"{url}?token={quote(access_token)}" if access_token else url


def post_webhook(url: str, payload: dict, timeout: float = 10) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8",
                                               "User-Agent": "webcam-monitoring/1.0"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise PipelineError(f"Dooray 응답 오류: HTTP {response.status}")
    except HTTPError as exc:
        raise PipelineError(f"Dooray 응답 오류: HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise PipelineError(f"Dooray 연결 실패: {exc}") from exc


def build_capture_payload(captured_at: datetime, records: list[dict[str, str]], image_url: str = "") -> dict:
    text = format_current_records(records)
    attachment = {"title": f"자동 저장 사진 · {captured_at:%Y-%m-%d %H:%M:%S}",
                  "text": f"이전 전송 이후 Current 기록 {len(records)}개\n{text}", "color": "#1677d2"}
    if image_url:
        attachment["imageUrl"] = image_url
        attachment["titleLink"] = image_url
    return {"botName": "Webcam Monitor", "text": "카메라 자동 저장 알림",
            "attachments": [attachment]}


def build_capture_payloads(captured_at: datetime, records: list[dict[str, str]], image_url: str = "",
                           max_text_chars: int = 2800) -> list[dict]:
    """Build small plain-text messages that Dooray Incoming Hook renders reliably."""
    lines = format_current_records(records).splitlines()
    chunks: list[list[str]] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        added = len(line) + (1 if current else 0)
        if current and current_length + added > max_text_chars:
            chunks.append(current)
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + (1 if current_length else 0)
    if current:
        chunks.append(current)
    if not chunks:
        chunks = [["해당 기간의 Current 분석 기록이 없습니다."]]

    total = len(chunks)
    payloads = []
    for index, chunk in enumerate(chunks, 1):
        heading = (f"카메라 자동 저장 · {captured_at:%Y-%m-%d %H:%M:%S}\n"
                   f"이전 전송 이후 Current 기록 {len(records)}개 · {index}/{total}")
        payloads.append({"botName": "Webcam Monitor", "text": heading + "\n" + "\n".join(chunk)})
    if image_url:
        payloads[-1]["attachments"] = [{
            "title": "선택 영역 사진 보기", "titleLink": image_url,
            "imageUrl": image_url, "color": "#1677d2",
        }]
    return payloads
