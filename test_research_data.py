import ctypes
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from multi_camera_core import CameraWorker, DemoSystem, SDKCamera, Settings
from research_data import FrameAudit, ResearchRecording, verify_dataset


def metadata(index, size=64):
    return dict(camera_tick_100us=index*1000, camera_frame_id=index+1,
                camera_timestamp_us=index*100000, host_receive_monotonic_ns=1000000000+index*100000000,
                host_receive_unix_ns=1800000000000000000+index*100000000,
                raw_header=dict(uBytes=size, uiMediaType=0x01080001, iWidth=8, iHeight=8,
                                uiTimeStamp=index*1000, uiExpTime=10000, fAnalogGain=1))


class ResearchTests(unittest.TestCase):
    def test_disk_failure_marks_dataset_incomplete_and_releases_writer(self):
        import os
        native_fsync = os.fsync
        with tempfile.TemporaryDirectory() as folder:
            record = ResearchRecording(folder, 'CAM', dict(serial='CAM', model='test'), Settings(), {}, {}, True)
            def fail_writer_sync(handle):
                if threading.current_thread().name == 'RAW-writer':
                    raise OSError('injected disk failure')
                return native_fsync(handle)
            with patch('research_data.os.fsync', side_effect=fail_writer_sync):
                record.add_raw(bytes(64), record.observe(metadata(0)))
                result = record.finish()
            self.assertFalse(result['complete'])
            self.assertFalse(record.encoder.thread.is_alive())
            self.assertIn('injected disk failure', str(result['errors']))
            self.assertFalse(verify_dataset(record.output)['valid'])

    def test_raw_segments_exact_bytes_metadata_and_corruption_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            record = ResearchRecording(folder, 'CAM', dict(serial='CAM', model='test'), Settings(),
                                       dict(experiment_id='EXP-001'), dict(values={}), True)
            record.encoder.segment_limit = 192
            record.sdk_start = dict(total=100, captured=100, lost=0)
            expected = []
            for n in range(10):
                pixels = bytes([n])*64
                expected.append(pixels)
                record.add_raw(pixels, record.observe(metadata(n)))
            result = record.finish(sdk_stats=dict(total=110, captured=110, lost=0))
            self.assertTrue(result['complete'])
            self.assertEqual(result['integrity']['sdk_delta']['captured'], 10)
            self.assertEqual(result['integrity']['received_frames'], 10)
            self.assertEqual(result['integrity']['intentionally_skipped_frames'], 0)
            self.assertEqual(result['repeated_frames'], 0)
            self.assertEqual(len(result['segments']), 4)
            self.assertEqual(verify_dataset(record.output)['valid'], True)
            rows = [json.loads(line) for line in (record.output/'frames.jsonl').read_text().splitlines()]
            for n, row in enumerate(rows):
                with (record.output/row['segment']).open('rb') as stream:
                    stream.seek(row['offset'])
                    self.assertEqual(stream.read(row['byte_count']), expected[n])
                self.assertEqual(row['raw_header']['uiExpTime'], 10000)
                self.assertEqual(row['camera_relative_ns'], n*100000000)
            # Same-size tampering must be detected, not only truncation.
            first = record.output/result['segments'][0]['file']
            with first.open('r+b') as stream:
                stream.write(b'X')
            self.assertFalse(verify_dataset(record.output)['valid'])

    def test_partial_dataset_reports_readable_frames_without_claiming_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            record = ResearchRecording(folder, 'CAM', dict(serial='CAM', model='test'), Settings(), {}, {}, True)
            record.add_raw(bytes(64), record.observe(metadata(0)))
            result = record.finish('injected disconnection')
            self.assertFalse(result['complete'])
            verification = verify_dataset(record.output)
            self.assertFalse(verification['valid'])
            self.assertEqual(verification['readable_frames'], 1)
            with (record.output/'frames.jsonl').open('ab') as stream:
                stream.write(b'{"partial":')
            self.assertTrue(verify_dataset(record.output)['errors'])

    def test_timestamp_wrap_and_id_gap_are_not_rewritten(self):
        audit = FrameAudit()
        first, second = metadata(0), metadata(1)
        first['camera_tick_100us'] = 0xfffffff0
        second['camera_tick_100us'] = 20
        second['camera_frame_id'] = 5
        audit.observe(first)
        row = audit.observe(second)
        self.assertEqual(row['camera_tick_100us'], 20)
        self.assertEqual(row['camera_relative_ns'], 36*100000)
        report = audit.report(dict(total=1, captured=1, lost=0), dict(total=6, captured=3, lost=3), 2)
        self.assertEqual(report['camera_frame_id_gap_count'], 3)
        self.assertEqual(report['status'], 'review')
        row = audit.observe(second)
        self.assertIsNone(row['camera_relative_ns'])

    def test_sdk_raw_copy_and_header_precede_mutating_isp(self):
        class Head(ctypes.Structure):
            _fields_ = [('uiTimeStamp', ctypes.c_uint), ('uBytes', ctypes.c_uint), ('uiMediaType', ctypes.c_uint),
                        ('iWidth', ctypes.c_int), ('iHeight', ctypes.c_int), ('uiExpTime', ctypes.c_uint)]
        head = Head(500, 4, 0x01080001, 2, 2, 10000)
        raw = ctypes.create_string_buffer(b'ABCD', 4)
        rgb = ctypes.create_string_buffer(12)
        released = []

        def process(h, src, dst, header):
            ctypes.memmove(src, b'XXXX', 4)
            ctypes.memmove(dst, b'R'*12, 12)
            header.uBytes = 12
            header.uiMediaType = 0x02180014
            return 0

        sdk = SimpleNamespace(CameraException=RuntimeError, CAMERA_STATUS_TIME_OUT=-12,
                              CAMERA_MEDIA_TYPE_RGB8=0x02180014,
                              CameraGetImageBuffer=lambda h, timeout: (ctypes.addressof(raw), head),
                              CameraImageProcess=process,
                              CameraReleaseImageBuffer=lambda h, p: released.append(p) or 0)
        camera = SDKCamera.__new__(SDKCamera)
        camera.s, camera.h, camera.buffer = sdk, 1, ctypes.addressof(rgb)
        camera.capacity = 12
        camera.capture_metadata = camera.capture_raw = True
        camera.optional_frame_errors = {}
        camera.sys = SimpleNamespace(check=lambda code: None, get=lambda *args: None)
        frame = camera.read(lambda tick: True)
        self.assertEqual(camera.raw_bytes, b'ABCD')
        self.assertEqual(camera.frame_metadata['raw_header']['uBytes'], 4)
        self.assertEqual(camera.frame_metadata['raw_header']['uiMediaType'], 0x01080001)
        self.assertEqual(frame[2], b'R'*12)
        self.assertEqual(len(released), 1)

    def test_worker_raw_all_frames_and_preset_roundtrip(self):
        events, system = queue.Queue(), DemoSystem()
        worker = CameraWorker(system, system.scan()[0], events)
        worker.start()

        def wait(kind):
            until = time.monotonic()+10
            while time.monotonic() < until:
                try:
                    serial, event, data = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if event in ('fault', 'command_error', 'prepare_failed'):
                    self.fail(str(data))
                if event == kind:
                    return data
            self.fail(f'Timed out: {kind}')

        try:
            wait('stats')
            with tempfile.TemporaryDirectory() as folder:
                presets = Path(folder)/'preset'
                presets.mkdir()
                worker.command('save_preset', folder=presets, settings=Settings(7, 2, True))
                wait('notice')
                worker.command('apply', settings=Settings(5, 1, False))
                worker.command('load_preset', folder=presets)
                wait('notice')
                self.assertEqual(worker.camera.settings(), Settings(7, 2, True))
                gate = threading.Event()
                worker.command('prepare', folder=folder, fps=30, mode='raw', settings=Settings(7, 2, True),
                               experiment=dict(experiment_id='test'), gate=gate)
                wait('prepared')
                gate.set()
                wait('record_started')
                time.sleep(0.6)
                worker.command('stop')
                result = wait('record_finished')['summary']
                self.assertTrue(result['complete'], result)
                self.assertGreater(result['written_frames'], 30)  # All 90 FPS frames, not a 30 FPS selection.
                self.assertEqual(result['source_frames_observed'], result['written_frames'])
                self.assertTrue(verify_dataset(result['data_path'])['valid'])
                self.assertTrue((Path(folder)/'DEMO-01.end.config').exists())
        finally:
            worker.command('close')
            worker.join(10)
            self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main(verbosity=2)
