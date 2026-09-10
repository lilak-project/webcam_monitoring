import tempfile
import unittest
import json
from pathlib import Path

from brio_ocr_monitor.pipeline import Reading, append_csv, build_video_filter, validate
from brio_ocr_monitor.web import DashboardServer


class PipelineTests(unittest.TestCase):
    def test_filter_contains_crop_scale_and_preprocessing(self):
        config = {
            "roi": {"x": 10, "y": 20, "width": 300, "height": 190, "scale": 4},
            "preprocess": {"grayscale": True, "contrast": 1.5, "brightness": 0, "sharpen": True},
        }
        value = build_video_filter(config)
        self.assertIn("crop=300:190:10:20", value)
        self.assertIn("scale=iw*4:ih*4:flags=lanczos", value)
        self.assertIn("format=gray", value)

    def test_validation_extracts_number(self):
        config = {"validation": {"pattern": r"[-+]?\d+(?:\.\d+)?"}}
        self.assertEqual(validate(config, " temperature: 17.5 C "), ("ok", "17.5"))
        self.assertEqual(validate(config, "no value"), ("ocr_failed", ""))

    def test_csv_header_is_written_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readings.csv"
            reading = Reading("2026-01-01T00:00:00+09:00", "ok", "7", "7", "a", "b")
            append_csv(path, reading)
            append_csv(path, reading)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("timestamp,status,value"))

    def test_web_roi_is_validated_and_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            server = DashboardServer.__new__(DashboardServer)
            server.config_path = config_path
            server.config = {
                "camera": {"width": 1920, "height": 1080},
                "roi": {"x": 0, "y": 0, "width": 100, "height": 100, "scale": 4},
            }
            roi = server.update_roi({"x": 945, "y": 420, "width": 390, "height": 240})
            self.assertEqual(roi["scale"], 4)
            self.assertEqual(json.loads(config_path.read_text())["roi"]["x"], 945)


if __name__ == "__main__":
    unittest.main()
