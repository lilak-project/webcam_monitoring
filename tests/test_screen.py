import tempfile
import time
import unittest
import csv
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import json

from brio_ocr_monitor.pipeline import PipelineError
from brio_ocr_monitor.screen import (
    ScreenTracker, _choose_ensemble_value, _extract_candidate,
    parse_value, read_field, trim_screen_border,
)
from brio_ocr_monitor.web import CameraStream
from brio_ocr_monitor.web import DashboardServer
from brio_ocr_monitor.monitor import (DisplayMonitor, display_record_columns,
                                      ensure_display_csv_schema, save_capture_pair)


class ScreenTests(unittest.TestCase):
    def test_display_csv_schema_migrates_rows_written_with_new_field_order(self):
        config = {"fields": [{"key": "beam"}, {"key": "isol_element"},
                             {"key": "current_ua"}]}
        columns = display_record_columns(config)
        old_columns = ["timestamp", "status", "error", "tracking_shift_px", "beam", "beam_status",
                       "current_ua", "current_ua_status"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "display-readings.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(old_columns)
                writer.writerow(["old", "ok", "", "1", "ON", "ok", "8.5", "ok"])
                writer.writerow(["new", "ok", "", "2", "ON", "ok", "22Na5+", "ok", "9.2", "ok"])
            ensure_display_csv_schema(path, columns)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["current_ua"], "8.5")
            self.assertEqual(rows[1]["isol_element"], "22Na5+")
            self.assertEqual(rows[1]["current_ua"], "9.2")

    def test_ensemble_extracts_screen_values_and_prefers_agreement(self):
        current = {'kind': 'number', 'decimals': 3, 'min': 0, 'max': 1000}
        self.assertEqual(_extract_candidate('Current 9.177uA', current), 9.177)
        element = {'kind': 'element'}
        self.assertEqual(_extract_candidate('22Na5+', element), '22Na5+')
        observations = [
            {'engine': 'tesseract', 'value': 9.177, 'confidence': 45},
            {'engine': 'rapidocr', 'value': 9.177, 'confidence': 99},
            {'engine': 'easyocr', 'value': 79.177, 'confidence': 20},
        ]
        self.assertEqual(_choose_ensemble_value(observations, current), 9.177)

    def test_thin_dark_bezel_is_trimmed_and_resized(self):
        image = np.zeros((600, 1400, 3), np.uint8)
        image[20:590, 18:1380] = 180
        corrected, bounds = trim_screen_border(image)
        self.assertEqual(corrected.shape, image.shape)
        self.assertEqual(bounds, [18, 20, 1380, 590])
        self.assertGreater(corrected[0, 0].mean(), 150)

    def test_independent_schedule_validation_and_persistence_values(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = DisplayMonitor(None, {}, Path(directory), Path(directory))
            status = monitor.update_schedule({
                'capture_enabled': True, 'capture_interval_seconds': 7,
                'analysis_enabled': False, 'analysis_interval_seconds': 19,
            })
            self.assertTrue(status['capture_enabled'])
            self.assertEqual(status['capture_interval_seconds'], 7)
            self.assertFalse(status['analysis_enabled'])
            self.assertEqual(status['analysis_interval_seconds'], 19)
            with self.assertRaises(PipelineError):
                monitor.update_schedule({'capture_interval_seconds': 1})
            self.assertTrue(monitor.capture_enabled)
            self.assertEqual(monitor.capture_interval, 7)
            with self.assertRaises(PipelineError):
                monitor.update_schedule({'analysis_interval_seconds': 86401})

    def test_slow_analysis_does_not_block_periodic_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = DisplayMonitor(None, {
                'capture_enabled': True, 'capture_interval_seconds': 2,
                'analysis_enabled': True, 'analysis_interval_seconds': 2,
            }, Path(directory), Path(directory))
            events = []
            monitor.capture = lambda: events.append('capture')
            def slow_run():
                events.append('analysis-start')
                time.sleep(.8)
                events.append('analysis-end')
            monitor.run = slow_run
            monitor.thread.start()
            time.sleep(.4)
            self.assertIn('capture', events)
            self.assertIn('analysis-start', events)
            self.assertNotIn('analysis-end', events)
            monitor.stop_event.set()
            monitor.thread.join(2)
            monitor.analysis_worker.join(2)

    def test_manual_run_is_queued_while_analysis_is_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = DisplayMonitor(None, {}, Path(directory), Path(directory))
            monitor.busy = True
            result = monitor.request_run()
            self.assertTrue(result['queued'])
            self.assertTrue(monitor.manual_requested)

    def test_voice_alert_only_announces_confirmed_transitions(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = DisplayMonitor(None, {'voice_alerts_enabled': True}, Path(directory), Path(directory))
            spoken = []
            monitor._speak = spoken.append
            def fields(beam, current):
                return [
                    {'key': 'beam', 'status': 'ok', 'value': beam},
                    {'key': 'current_ua', 'status': 'ok', 'value': current},
                ]
            monitor._announce_transitions(fields('OFF', 0))
            self.assertEqual(spoken, [])
            monitor._announce_transitions(fields('ON', 1.2))
            self.assertEqual(spoken, ['빔이 켜졌습니다. 커런트가 켜졌습니다'])
            monitor._announce_transitions([
                {'key': 'beam', 'status': 'uncertain', 'value': None},
                {'key': 'current_ua', 'status': 'uncertain', 'value': None},
            ])
            self.assertEqual(len(spoken), 1)

    def test_hourly_voice_announces_once_for_new_hour(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = DisplayMonitor(None, {'voice_hourly_enabled': True}, Path(directory), Path(directory))
            spoken = []
            monitor._record_and_speak = spoken.append
            monitor.last_hour_key = 'old-hour'
            monitor._announce_hour()
            monitor._announce_hour()
            self.assertEqual(len(spoken), 1)
            self.assertRegex(spoken[0], r'^(자정|정오|오전 .+ 시|오후 .+ 시)입니다$')

    def test_hour_message_uses_native_korean_numbers(self):
        self.assertEqual(DisplayMonitor._hour_message(0), '자정입니다')
        self.assertEqual(DisplayMonitor._hour_message(10), '오전 열 시입니다')
        self.assertEqual(DisplayMonitor._hour_message(11), '오전 열한 시입니다')
        self.assertEqual(DisplayMonitor._hour_message(12), '정오입니다')
        self.assertEqual(DisplayMonitor._hour_message(13), '오후 한 시입니다')
        self.assertEqual(DisplayMonitor._hour_message(23), '오후 열한 시입니다')

    def test_periodic_photo_uses_unique_dated_path(self):
        camera = type('Camera', (), {'snapshot': lambda self: b'jpeg'})()
        with tempfile.TemporaryDirectory() as directory:
            monitor = DisplayMonitor(camera, {}, Path(directory), Path(directory))
            first = monitor.capture()
            second = monitor.capture()
            self.assertNotEqual(first['path'], second['path'])
            self.assertEqual(Path(first['path']).read_bytes(), b'jpeg')
            self.assertIn('snapshots', first['path'])
            self.assertRegex(Path(first['path']).parent.name, r'^\d{8}$')

    def test_original_and_perspective_selection_are_saved_as_pair(self):
        image = np.full((300, 500, 3), 180, np.uint8)
        cv2.putText(image, 'AREA', (120, 160), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
        ok, encoded = cv2.imencode('.jpg', image)
        config = {'corners': [[50, 40], [450, 30], [470, 270], [30, 280]], 'size': [400, 200]}
        with tempfile.TemporaryDirectory() as directory:
            paths = save_capture_pair(encoded.tobytes(), config, Path(directory), '20260911-091234-123456')
            self.assertEqual(Path(paths['original_path']).name, 'full-20260911-091234-123456.jpg')
            self.assertEqual(Path(paths['selected_path']).name, 'selected-20260911-091234-123456.jpg')
            selected = cv2.imread(paths['selected_path'])
            self.assertEqual(selected.shape[:2], (200, 400))

    def test_perspective_preview_and_invalid_order(self):
        image = np.full((480, 640, 3), 220, np.uint8)
        cv2.putText(image, 'SCREEN', (160, 240), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 4)
        ok, encoded = cv2.imencode('.jpg', image)
        camera = type('Camera', (), {'snapshot': lambda self: encoded.tobytes()})()
        with tempfile.TemporaryDirectory() as directory:
            server = DashboardServer.__new__(DashboardServer)
            server.camera = camera
            server.config = {'storage': {'directory': directory},
                             'display_monitor': {'size': [400, 200]}}
            server.config_path = Path(directory) / 'config.json'
            server.config_path.write_text(json.dumps(server.config))
            server.config_lock = __import__('threading').Lock()
            server.perspective_preview = None
            server.monitor = None
            result = server.perspective({'points': [[100, 100], [540, 80], [570, 400], [80, 420]]})
            self.assertEqual(result['size'], [400, 200])
            decoded = cv2.imdecode(np.frombuffer(server.perspective_preview, np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(decoded.shape[:2], (200, 400))
            with self.assertRaises(PipelineError):
                server.perspective({'points': [[100, 100], [80, 420], [570, 400], [540, 80]]})

    def test_decimal_point_and_state_are_not_guessed(self):
        field = {'kind': 'number', 'decimals': 3, 'min': 0, 'max': 1000}
        self.assertEqual(parse_value('9.250', field), 9.25)
        for text in ('9250', '9.25', '9.25O', '-9.250', '1001.000'):
            self.assertIsNone(parse_value(text, field))
        self.assertIsNone(parse_value('0FF', {'kind': 'state'}))
        self.assertEqual(parse_value('OFF', {'kind': 'state'}), 'OFF')

    def test_disagreement_and_low_confidence_are_rejected(self):
        def output(value, confidence=90):
            return type('Result', (), {'stdout': f'text\tconf\n{value}\t{confidence}\n'})()
        field = {'key': 'current', 'label': 'Current', 'box': [0, 0, 50, 30], 'kind': 'number', 'decimals': 3}
        image = np.full((40, 60, 3), 200, np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            for outputs in ([output('9.250'),output('9.250'),output('8.250')],
                            [output('9.250',0)]*3):
                with patch('brio_ocr_monitor.screen.subprocess.run', side_effect=outputs):
                    result = read_field(image, field, Path(directory))
                    self.assertIsNone(result['value'])
                    self.assertEqual(result['status'], 'uncertain')

    def test_configured_majority_accepts_repeated_numeric_value(self):
        def output(value, confidence=90):
            return type('Result', (), {'stdout': f'text\tconf\n{value}\t{confidence}\n'})()
        field = {'key': 'energy', 'label': 'Energy', 'box': [0, 0, 50, 30],
                 'kind': 'number', 'decimals': 2, 'allow_majority': True,
                 'min_confidence': 0}
        image = np.full((40, 60, 3), 200, np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            with patch('brio_ocr_monitor.screen.subprocess.run', side_effect=[
                    output('10.00', 0), output('20.00', 0), output('10.00', 0)]):
                result = read_field(image, field, Path(directory))
        self.assertEqual(result['value'], 10.0)
        self.assertEqual(result['status'], 'ok')

    def test_main_beam_uses_banner_color(self):
        field = {'key': 'beam', 'label': 'Beam', 'box': [0, 0, 200, 40], 'kind': 'beam_state'}
        with tempfile.TemporaryDirectory() as directory:
            green = np.full((40, 200, 3), (20, 190, 20), np.uint8)
            blue = np.full((40, 200, 3), (190, 90, 20), np.uint8)
            self.assertEqual(read_field(green, field, Path(directory))['value'], 'ON')
            self.assertEqual(read_field(blue, field, Path(directory))['value'], 'OFF')

    def test_shift_rotation_tracking_and_blank_rejection(self):
        rng = np.random.default_rng(42)
        image = np.full((600, 1000, 3), 220, np.uint8)
        for _ in range(200):
            x, y = rng.integers([160, 150], [830, 450])
            cv2.putText(image, str(rng.integers(100)), (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX, .5, (20,20,20), 1)
        corners = np.float32([[100,100],[900,100],[900,500],[100,500]])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'ref.jpg'; cv2.imwrite(str(path), image)
            tracker = ScreenTracker(path, corners.tolist())
            matrix = cv2.getRotationMatrix2D((500,300), 1.2, 1.01)
            matrix[:,2] += [18,-12]
            moved = cv2.warpAffine(image,matrix,(1000,600))
            _, info = tracker.align(moved)
            expected = cv2.transform(corners[None], matrix)[0]
            self.assertLess(np.max(np.linalg.norm(np.array(info['corners'])-expected,axis=1)), 3)
            with self.assertRaises(PipelineError):
                tracker.align(np.zeros_like(image))
            with self.assertRaises(PipelineError):
                tracker.align(cv2.warpAffine(image,np.float32([[1,0,170],[0,1,0]]),(1000,600)))

    def test_stale_frame_is_not_returned(self):
        camera = CameraStream({})
        camera.frame = b'old'
        camera.frame_time = time.time()-10
        with self.assertRaises(PipelineError):
            camera.snapshot()

    def test_stale_camera_creates_failure_without_values(self):
        camera = CameraStream({'device':'test','width':1920,'height':1080})
        with tempfile.TemporaryDirectory() as directory:
            monitor = DisplayMonitor(camera,{'fields':[{'key':'current'}]},Path(directory),Path(directory))
            result = monitor.run()
            self.assertEqual(result['status'],'error')
            self.assertEqual(result['fields'],[])
            self.assertIn('unavailable',(Path(directory)/'display-readings.csv').read_text())

    def test_concurrent_run_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=DisplayMonitor(None,{},Path(directory),Path(directory))
            monitor.lock.acquire()
            with self.assertRaises(PipelineError):
                monitor.run()
            monitor.lock.release()

if __name__ == '__main__':
    unittest.main()
