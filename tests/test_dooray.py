import csv
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from brio_ocr_monitor.dooray import (build_capture_payload, build_capture_payloads, current_records,
                                     snapshot_url, validate_https_url, webhook_profile)
from brio_ocr_monitor.pipeline import PipelineError


class DoorayTests(unittest.TestCase):
    def test_current_records_only_include_values_after_last_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "display-readings.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "current_ua", "current_ua_status"])
                writer.writeheader()
                writer.writerow({"timestamp": "2026-09-11T10:00:00+09:00", "current_ua": "1.0",
                                 "current_ua_status": "ok"})
                writer.writerow({"timestamp": "2026-09-11T10:01:00+09:00", "current_ua": "2.0",
                                 "current_ua_status": "ok"})
            records = current_records(path, "2026-09-11T10:00:00+09:00")
            self.assertEqual([record["value"] for record in records], ["2.0"])

    def test_payload_contains_current_history_and_selected_photo(self):
        url = snapshot_url("https://monitor.example", "/tmp/20260911/selected-20260911-100100-123456.jpg",
                           "private-token")
        payload = build_capture_payload(datetime.fromisoformat("2026-09-11T10:01:00+09:00"), [
            {"timestamp": "2026-09-11T10:00:30+09:00", "value": "9.177", "status": "ok"}
        ], url)
        self.assertEqual(payload["attachments"][0]["imageUrl"], url)
        self.assertIn("9.177 µA", payload["attachments"][0]["text"])
        self.assertTrue(url.endswith("?token=private-token"))

    def test_payload_without_public_url_sends_records_without_an_image(self):
        payload = build_capture_payload(datetime.fromisoformat("2026-09-11T10:01:00+09:00"), [
            {"timestamp": "2026-09-11T10:00:30+09:00", "value": "9.177", "status": "ok"}
        ])
        self.assertNotIn("imageUrl", payload["attachments"][0])
        self.assertIn("9.177 µA", payload["attachments"][0]["text"])

    def test_large_history_is_split_into_plain_text_messages(self):
        records = [{"timestamp": f"2026-09-11T10:{index:02d}:00+09:00", "value": str(index),
                    "status": "ok"} for index in range(10)]
        payloads = build_capture_payloads(
            datetime.fromisoformat("2026-09-11T11:00:00+09:00"), records, max_text_chars=45)
        self.assertGreater(len(payloads), 1)
        self.assertTrue(all("text" in payload for payload in payloads))
        self.assertTrue(all("attachments" not in payload for payload in payloads))
        self.assertIn("0 µA", "\n".join(payload["text"] for payload in payloads))

    def test_webhook_requires_https(self):
        with self.assertRaises(PipelineError):
            validate_https_url("http://example.test/hook", "Webhook", required=True)

    def test_profile_masks_webhook_secret(self):
        profile = webhook_profile("https://ibs.gov-dooray.com/services/123456789012/987654321098/secret-token")
        self.assertEqual(profile["host"], "ibs.gov-dooray.com")
        self.assertNotIn("secret-token", profile["masked_url"])
        self.assertIn("1234…9012", profile["masked_url"])


if __name__ == "__main__":
    unittest.main()
