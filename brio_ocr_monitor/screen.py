"""Track a calibrated display and read independently validated fields."""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from .pipeline import PipelineError


_ensemble_lock = threading.Lock()
_rapid_engine = None
_easy_reader = None


class ScreenTracker:
    def __init__(self, reference: Path, corners: list, size=(1400, 600), max_shift=100):
        self.reference = cv2.imread(str(reference), cv2.IMREAD_GRAYSCALE)
        if self.reference is None:
            raise PipelineError("기준 화면 이미지가 없습니다")
        self.corners = np.float32(corners).reshape(4, 2)
        self.size = tuple(size)
        self.max_shift = max_shift
        self.orb = cv2.ORB_create(nfeatures=3000, fastThreshold=12)
        mask = np.zeros_like(self.reference)
        cv2.fillConvexPoly(mask, self.corners.astype(np.int32), 255)
        self.keys, self.desc = self.orb.detectAndCompute(self.reference, mask)
        if self.desc is None or len(self.keys) < 30:
            raise PipelineError("기준 화면의 특징점이 부족합니다")

    def align(self, image):
        if image is None or image.shape[:2] != self.reference.shape:
            raise PipelineError("카메라 이미지 크기가 기준 화면과 다릅니다")
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        keys, desc = self.orb.detectAndCompute(gray, None)
        if desc is None:
            raise PipelineError("화면을 찾지 못했습니다")
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(self.desc, desc, k=2)
        matches = [p[0] for p in pairs if len(p) == 2 and p[0].distance < .70 * p[1].distance]
        if len(matches) < 20:
            raise PipelineError("화면 위치 보정 실패: 일치하는 특징점 부족")
        source = np.float32([self.keys[m.queryIdx].pt for m in matches])
        target = np.float32([keys[m.trainIdx].pt for m in matches])
        matrix, inliers = cv2.findHomography(source, target, cv2.RANSAC, 3.0)
        if matrix is None or inliers is None:
            raise PipelineError("화면 위치 보정 실패")
        good = inliers.ravel().astype(bool)
        ratio = float(good.mean())
        spread = cv2.contourArea(cv2.convexHull(source[good]))
        area = cv2.contourArea(self.corners)
        if good.sum() < 15 or ratio < .55 or spread < area * .08:
            raise PipelineError("화면 위치 보정 신뢰도가 낮습니다")
        corners = cv2.perspectiveTransform(self.corners[None], matrix)[0]
        shifts = np.linalg.norm(corners - self.corners, axis=1)
        relative_area = abs(cv2.contourArea(corners)) / area
        h, w = gray.shape
        if (not np.isfinite(corners).all() or not cv2.isContourConvex(corners)
                or shifts.max() > self.max_shift or not .75 < relative_area < 1.3
                or (corners < 0).any() or (corners[:, 0] >= w).any() or (corners[:, 1] >= h).any()):
            raise PipelineError("화면 이동이 보정 범위를 벗어났습니다. 기준 화면을 다시 설정하세요")
        width, height = self.size
        destination = np.float32([[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]])
        warp = cv2.getPerspectiveTransform(corners, destination)
        aligned = cv2.warpPerspective(image, warp, self.size)
        aligned, trim = trim_screen_border(aligned)
        return aligned, {"inliers": int(good.sum()), "inlier_ratio": round(ratio, 3),
                         "max_shift_px": round(float(shifts.max()), 1), "corners": corners.tolist(),
                         "trim": trim}


def trim_screen_border(image):
    """Remove a small dark bezel left inside a perspective selection."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    rows = np.flatnonzero((gray > 55).mean(axis=1) > .45)
    columns = np.flatnonzero((gray > 55).mean(axis=0) > .45)
    height, width = gray.shape
    if not len(rows) or not len(columns):
        return image, [0, 0, width, height]
    x0, x1 = int(columns[0]), int(columns[-1] + 1)
    y0, y1 = int(rows[0]), int(rows[-1] + 1)
    # Only trim thin borders. A large dark area may be legitimate screen content.
    if x1 - x0 < width * .8 or y1 - y0 < height * .8:
        return image, [0, 0, width, height]
    if (x0, y0, x1, y1) == (0, 0, width, height):
        return image, [0, 0, width, height]
    cropped = image[y0:y1, x0:x1]
    return cv2.resize(cropped, (width, height), interpolation=cv2.INTER_LINEAR), [x0, y0, x1, y1]


def parse_value(text: str, field: dict):
    value = text.strip().upper().replace(" ", "")
    if field.get("pattern"):
        match = re.fullmatch(field["pattern"], value)
        if match is None:
            return None
        value = match.group(1)
    if field["kind"] == "state":
        return value if value in ("ON", "OFF") else None
    if field["kind"] == "element":
        value = value.replace("*", "+")
        return value if re.fullmatch(r"\d{1,3}[A-Z][A-Z]?\d{1,2}\+", value) else None
    if field["kind"] == "attenuator":
        if value in ("ON", "OFF"):
            return value
        value = value.replace("−", "-").replace("^", "")
        return "1⁻³" if re.search(r"[14]-?3", value) else None
    decimals = field.get("decimals", 0)
    pattern = r"\d+" if not decimals else rf"\d+\.\d{{{decimals}}}"
    if not re.fullmatch(pattern, value):
        return None
    number = float(value)
    if not field.get("min", 0) <= number <= field.get("max", 1e9):
        return None
    return number


def read_field(image, field: dict, directory: Path):
    if field["kind"] == "beam_state":
        return _read_beam_state(image, field, directory)
    if field["kind"] == "element" and field.get("parts"):
        return _read_element_parts(image, field, directory)
    result = _read_field(image, field, directory)
    if result["status"] == "ok":
        return result
    for index, alternative in enumerate(field.get("alternatives", [])):
        other = {**field, **alternative, "key": field["key"] + f"-context{index}"}
        result["observations"].extend(_read_field(image, other, directory)["observations"])
    observations = result["observations"]
    values = [o["value"] for o in observations if o["value"] is not None]
    accepted = [o for o in observations if o["value"] is not None
                and o["confidence"] >= field.get("min_confidence", 30)]
    if len(accepted) >= 2 and len(set(values)) == 1:
        result.update(value=values[0], status="ok")
    return result


def read_fields_ensemble(image, fields: list[dict], directory: Path):
    """Read each tightly cropped field with Tesseract, RapidOCR and Apple Vision."""
    crops = {}
    for field in fields:
        if field["kind"] == "beam_state":
            continue
        x, y, w, h = field["box"]
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > image.shape[1] or y+h > image.shape[0]:
            raise PipelineError("판독 영역이 화면을 벗어났습니다")
        crops[field["key"]] = image[y:y+h, x:x+w]
    sheet, rows = _ocr_sheet(crops, directory)
    tesseract = _tesseract_sheet_observations(sheet, rows, fields, directory)
    easyocr = _easyocr_sheet_observations(sheet, rows, fields)
    results = []
    for field in fields:
        if field["kind"] == "beam_state":
            results.append(_read_beam_state(image, field, directory))
            continue
        observations = [tesseract[field["key"]], _rapid_observation(crops[field["key"]], field),
                        easyocr[field["key"]]]
        value = _choose_ensemble_value(observations, field)
        if value is None and field.get("fallback_value"):
            value = field["fallback_value"]
        results.append({"key": field["key"], "label": field["label"],
                        "unit": field.get("unit", ""), "decimals": field.get("decimals", 0),
                        "value": value, "status": "ok" if value is not None and any(
                            item["value"] == value for item in observations) else "uncertain",
                        "observations": observations})
    return results


def _ocr_sheet(crops: dict[str, np.ndarray], directory: Path):
    """Place all small crops in separate rows so full OCR engines run once."""
    scale, padding, gap = 5, 20, 24
    widths = [crop.shape[1] * scale for crop in crops.values()]
    heights = [crop.shape[0] * scale for crop in crops.values()]
    canvas = np.full((sum(heights) + gap * (len(heights) + 1),
                      max(widths, default=1) + padding * 2), 255, np.uint8)
    rows = {}
    top = gap
    for (key, crop), height in zip(crops.items(), heights):
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        enlarged = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        canvas[top:top+height, padding:padding+enlarged.shape[1]] = enlarged
        rows[key] = (top, top + height, padding + enlarged.shape[1])
        cv2.imwrite(str(directory / f"{key}-gray.png"), enlarged)
        top += height + gap
    return canvas, rows


def _empty_engine_observations(fields: list[dict], engine: str, raw: str = ""):
    return {field["key"]: {"engine": engine, "raw": raw, "confidence": 0, "value": None}
            for field in fields if field["kind"] != "beam_state"}


def _field_for_row(y: float, rows: dict[str, tuple[int, int]]):
    return next((key for key, bounds in rows.items() if bounds[0] <= y <= bounds[1]), None)


def _tesseract_sheet_observations(sheet, rows, fields, directory: Path):
    observations = _empty_engine_observations(fields, "tesseract")
    path = directory / "ocr-tesseract-sheet.png"
    cv2.imwrite(str(path), sheet)
    try:
        bundled = Path(sys.executable).with_name("tesseract")
        executable = shutil.which("tesseract") or (str(bundled) if bundled.is_file() else "tesseract")
        command = [executable, str(path), "stdout", "-l", "eng", "--psm", "6", "tsv"]
        result = subprocess.run(command, env={**os.environ, "OMP_THREAD_LIMIT": "1"},
                                capture_output=True, text=True, check=True, timeout=15)
        grouped = {key: [] for key in rows}
        for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
            if not row.get("text", "").strip() or float(row.get("conf", -1)) < 0:
                continue
            key = _field_for_row(float(row["top"]) + float(row["height"]) / 2, rows)
            if key:
                grouped[key].append(row)
        by_key = {field["key"]: field for field in fields}
        for key, words in grouped.items():
            raw = " ".join(word["text"] for word in words)
            confidence = min((float(word["conf"]) for word in words), default=0)
            observations[key] = {"engine": "tesseract", "raw": raw,
                                 "confidence": round(confidence, 1),
                                 "value": _extract_candidate(raw, by_key[key])}
    except Exception as exc:
        return _empty_engine_observations(fields, "tesseract", f"사용 불가: {exc}")
    return observations


def _easyocr_sheet_observations(sheet, rows, fields):
    global _easy_reader
    observations = _empty_engine_observations(fields, "easyocr")
    try:
        import easyocr
        with _ensemble_lock:
            if _easy_reader is None:
                _easy_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
            boxes = [[20, right, top, bottom] for top, bottom, right in rows.values()]
            detected = _easy_reader.recognize(sheet, horizontal_list=boxes, free_list=[],
                                              decoder="greedy", batch_size=len(boxes), workers=0,
                                              detail=1)
        grouped = {key: [] for key in rows}
        for box, text, confidence in detected:
            center_y = sum(point[1] for point in box) / len(box)
            key = _field_for_row(center_y, rows)
            if key:
                grouped[key].append((text, float(confidence)))
        by_key = {field["key"]: field for field in fields}
        for key, candidates in grouped.items():
            raw = " ".join(item[0] for item in candidates)
            confidence = min((item[1] for item in candidates), default=0) * 100
            observations[key] = {"engine": "easyocr", "raw": raw,
                                 "confidence": round(confidence, 1),
                                 "value": _extract_candidate(raw, by_key[key])}
    except Exception as exc:
        return _empty_engine_observations(fields, "easyocr", f"사용 불가: {exc}")
    return observations


def _extract_candidate(text: str, field: dict):
    normalized = text.strip().upper().replace("−", "-").replace("Μ", "U").replace("Μ", "U")
    compact = re.sub(r"\s+", "", normalized)
    kind = field["kind"]
    if kind == "state":
        match = re.search(r"(?<![A-Z])(ON|OFF)(?![A-Z])", normalized)
        return match.group(1) if match else None
    if kind == "attenuator":
        match = re.search(r"(?<![A-Z])(ON|OFF)(?![A-Z])", normalized)
        if match:
            return match.group(1)
        return "1⁻³" if re.search(r"1\s*(?:\^|-|⁻)?\s*3", normalized) else None
    if kind == "element":
        match = re.search(r"(\d{1,3})([A-Z][A-Z]?)(\d{1,2})\+", compact)
        return f"{match.group(1)}{match.group(2).title()}{match.group(3)}+" if match else None
    decimals = int(field.get("decimals", 0))
    pattern = rf"(?<![\d.])\d+\.\d{{{decimals}}}(?!\d)" if decimals else r"(?<![\d.])\d+(?![\d.])"
    for candidate in re.findall(pattern, normalized):
        number = float(candidate)
        if field.get("min", 0) <= number <= field.get("max", 1e9):
            return number
    return None


def _choose_ensemble_value(observations: list[dict], field: dict | None = None):
    valid = [item for item in observations if item.get("value") is not None]
    counts = Counter(item["value"] for item in valid)
    if counts:
        winner, count = counts.most_common(1)[0]
        if count >= 2:
            return winner
    rapid_threshold = 60 if field and field.get("kind") == "number" and not field.get("decimals") else 80
    strong = [item for item in valid if item.get("confidence", 0) >= (
        rapid_threshold if item.get("engine") == "rapidocr" else 70)]
    return max(strong, key=lambda item: item["confidence"])["value"] if strong else None


def _tesseract_observation(crop, field: dict, directory: Path):
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=5, fy=5, interpolation=cv2.INTER_CUBIC)
    gray = cv2.copyMakeBorder(gray, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)
    path = directory / f"{field['key']}-tesseract.png"
    cv2.imwrite(str(path), gray)
    try:
        bundled = Path(sys.executable).with_name("tesseract")
        executable = shutil.which("tesseract") or (str(bundled) if bundled.is_file() else "tesseract")
        command = [executable, str(path), "stdout", "-l", "eng", "--psm", "7", "tsv"]
        result = subprocess.run(command, env={**os.environ, "OMP_THREAD_LIMIT": "1"},
                                capture_output=True, text=True, check=True, timeout=15)
        words = [row for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t")
                 if row.get("text", "").strip() and float(row.get("conf", -1)) >= 0]
        raw = " ".join(row["text"] for row in words)
        confidence = min((float(row["conf"]) for row in words), default=0)
        return {"engine": "tesseract", "raw": raw, "confidence": round(confidence, 1),
                "value": _extract_candidate(raw, field)}
    except Exception as exc:
        return {"engine": "tesseract", "raw": f"사용 불가: {exc}", "confidence": 0, "value": None}


def _rapid_observation(crop, field: dict):
    global _rapid_engine
    try:
        with _ensemble_lock:
            if _rapid_engine is None:
                from rapidocr import RapidOCR
                _rapid_engine = RapidOCR()
            result = _rapid_engine(crop, use_det=False, use_cls=False, use_rec=True)
        raw = " ".join(result.txts or ())
        confidence = min(result.scores or (), default=0) * 100
        return {"engine": "rapidocr", "raw": raw, "confidence": round(confidence, 1),
                "value": _extract_candidate(raw, field)}
    except Exception as exc:
        return {"engine": "rapidocr", "raw": f"사용 불가: {exc}", "confidence": 0, "value": None}


def _vision_observation(crop, field: dict):
    try:
        import Vision
        from Foundation import NSData
        ok, encoded = cv2.imencode(".png", crop)
        if not ok:
            raise RuntimeError("이미지 변환 실패")
        data = NSData.dataWithBytes_length_(encoded.tobytes(), len(encoded))
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(["en-US"])
        request.setUsesLanguageCorrection_(False)
        completed, error = handler.performRequests_error_([request], None)
        if not completed:
            raise RuntimeError(str(error))
        candidates = [item.topCandidates_(1)[0] for item in (request.results() or [])
                      if item.topCandidates_(1)]
        raw = " ".join(item.string() for item in candidates)
        confidence = min((float(item.confidence()) for item in candidates), default=0) * 100
        return {"engine": "apple_vision", "raw": raw, "confidence": round(confidence, 1),
                "value": _extract_candidate(raw, field)}
    except Exception as exc:
        return {"engine": "apple_vision", "raw": f"사용 불가: {exc}", "confidence": 0, "value": None}


def _read_beam_state(image, field: dict, directory: Path):
    """Read the main beam state from its green ON / blue OFF banner."""
    x, y, w, h = field["box"]
    crop = image[y:y+h, x:x+w]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    saturated = hsv[:, :, 1] >= 55
    green = float(((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] < 90) & saturated).mean())
    blue = float(((hsv[:, :, 0] >= 90) & (hsv[:, :, 0] <= 135) & saturated).mean())
    value = "ON" if green > .18 and green > blue * 1.4 else (
        "OFF" if blue > .18 and blue > green * 1.4 else None)
    cv2.imwrite(str(directory / f"{field['key']}-color.jpg"), crop)
    return {"key": field["key"], "label": field["label"], "unit": "", "decimals": 0,
            "value": value, "status": "ok" if value else "uncertain",
            "observations": [{"raw": f"green={green:.3f}, blue={blue:.3f}",
                              "confidence": round(max(green, blue) * 100, 1), "value": value}]}


def _read_element_parts(image, field: dict, directory: Path):
    """Read isotope mass, chemical symbol, and charge from separate baselines."""
    pieces = []
    observations = []
    for name, part in field["parts"].items():
        x, y, w, h = part["box"]
        crop = image[y:y+h, x:x+w]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=10, fy=10, interpolation=cv2.INTER_CUBIC)
        if part.get("binary"):
            gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        path = directory / f"{field['key']}-{name}.png"
        cv2.imwrite(str(path), gray)
        command = ["tesseract", str(path), "stdout", "-l", "eng", "--psm",
                   str(part.get("psm", 7)), "-c", f"tessedit_char_whitelist={part['whitelist']}"]
        completed = subprocess.run(command, env={**os.environ, "OMP_THREAD_LIMIT": "1"},
                                   capture_output=True, text=True, check=True, timeout=15)
        raw = re.sub(r"[^A-Za-z0-9]", "", completed.stdout)
        observations.append({"raw": f"{name}:{raw}", "confidence": 0, "value": raw or None})
        pieces.append(raw)
    value = pieces[0] + pieces[1].title() + pieces[2] + "+" if all(pieces) else None
    if value is None or not re.fullmatch(r"\d{1,3}[A-Z][a-z]?\d{1,2}\+", value):
        value = None
    fallback = field.get("fallback_value") if value is None else None
    return {"key": field["key"], "label": field["label"], "unit": "", "decimals": 0,
            "value": value or fallback, "status": "ok" if value else "uncertain",
            "observations": observations}


def _read_field(image, field: dict, directory: Path):
    x, y, w, h = field["box"]
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > image.shape[1] or y+h > image.shape[0]:
        raise PipelineError("판독 영역이 화면을 벗어났습니다")
    crop = image[y:y+h, x:x+w]
    color = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    plain = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
    gray = plain.copy()
    gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    sharp = cv2.addWeighted(gray, 1.7, cv2.GaussianBlur(gray, (0, 0), 1), -.7, 0)
    observations = []
    whitelist = {"state": "ONF", "number": "0123456789.",
                 "element": "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz+",
                 "attenuator": "ONF0123456789^-"}[field["kind"]]
    modes = (("plain", plain), ("color", color)) if field.get("preprocess") == "plain" else (
        ("gray", gray), ("binary", binary), ("sharp", sharp))
    for mode, processed in modes:
        processed = cv2.copyMakeBorder(processed, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
        path = directory / f"{field['key']}-{mode}.png"
        cv2.imwrite(str(path), processed)
        command = ["tesseract", str(path), "stdout", "-l", "eng", "--psm", str(field.get("psm", 7))]
        if not field.get("pattern"):
            command += ["-c", f"tessedit_char_whitelist={whitelist}"]
        result = subprocess.run(command + ["tsv"], env={**os.environ, "OMP_THREAD_LIMIT": "1"},
                                capture_output=True, text=True, check=True, timeout=15)
        words = [row for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t")
                 if row.get("text", "").strip() and float(row.get("conf", -1)) >= 0]
        raw = " ".join(row["text"] for row in words)
        confidence_words = words
        if field.get("pattern") and field["kind"] == "number":
            confidence_words = [row for row in words if re.search(r"\d", row["text"])]
        confidence = min((float(row["conf"]) for row in confidence_words), default=0)
        observations.append({"raw": raw, "confidence": round(confidence, 1), "value": parse_value(raw, field)})
    values = [obs["value"] for obs in observations if obs["value"] is not None]
    accepted = [obs["value"] for obs in observations if obs["value"] is not None
                and obs["confidence"] >= field.get("min_confidence", 30)]
    counts = Counter(accepted)
    winner, count = counts.most_common(1)[0] if counts else (None, 0)
    valid = count >= field.get("min_matches", 2) and (
        field.get("allow_majority", False) or len(counts) == 1)
    return {"key": field["key"], "label": field["label"], "unit": field.get("unit", ""),
            "decimals": field.get("decimals", 0),
            "value": winner if valid else None, "status": "ok" if valid else "uncertain",
            "observations": observations}
