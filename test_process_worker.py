"""Offline tests of the per-process MindVision worker (mv_worker.py) with simulated cameras. No SDK, no camera."""
import json
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from multi_camera_core import MindVisionProcessWorker, Settings
from research_data import verify_dataset


class ProcessWorkerTests(unittest.TestCase):
    def make_folder(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)  # runs after the worker shutdown
        return folder

    def start(self, folder, serial='DEMO-01'):
        events = queue.Queue()
        worker = MindVisionProcessWorker(dict(serial=serial, model='模拟相机'), events, demo=True, log_folder=folder)
        worker.start()
        self.addCleanup(self.shutdown, worker)
        connected = self.wait(events, 'connected')
        return worker, events, connected

    def shutdown(self, worker):
        if worker.is_alive():
            worker.command('close')
            worker.join(30)

    def wait(self, events, kind, timeout=30, allow=()):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                _, name, data = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if name in ('fault', 'prepare_failed', 'command_error') and name != kind and name not in allow:
                self.fail(f'{name}: {data}')
            if name == kind:
                return data
        self.fail(f'timeout waiting for {kind}')

    def record(self, worker, events, folder, mode='mp4', seconds=1.5, fps=30):
        gate = threading.Event()
        worker.command('prepare', folder=folder, fps=fps, settings=Settings(10, 2, True), gate=gate, mode=mode,
                       experiment=dict(experiment_id='PROC-1'))
        self.wait(events, 'prepared')
        gate.set()
        self.wait(events, 'record_started')
        time.sleep(seconds)
        worker.command('stop')
        return self.wait(events, 'record_finished')['summary']

    def probe_frames(self, path):
        output = subprocess.check_output([shutil.which('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-count_frames',
                                          '-show_entries', 'stream=nb_read_frames', '-of', 'json', str(path)],
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return int(json.loads(output)['streams'][0]['nb_read_frames'])

    def test_connect_preview_settings_and_advanced(self):
        folder = self.make_folder()
        worker, events, connected = self.start(folder)
        self.assertIsInstance(connected['settings'], Settings)
        self.assertIsInstance(connected['gain_range'], tuple)
        stats = self.wait(events, 'stats')
        self.assertEqual(stats['shape'], (640, 400))
        self.assertEqual(worker.shape, (640, 400))
        deadline = time.monotonic() + 5
        image = None
        while image is None and time.monotonic() < deadline:
            image = worker.take_preview()
            time.sleep(0.05)
        self.assertIsNotNone(image)
        worker.full_preview.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            image = worker.take_preview()
            if image is not None and image.size == worker.shape:
                break
            time.sleep(0.05)
        self.assertEqual(image.size, worker.shape)
        worker.full_preview.clear()
        worker.command('apply', settings=Settings(12.5, 3, True))
        settings = self.wait(events, 'settings')['settings']
        self.assertEqual((settings.exposure_ms, settings.gain), (12.5, 3))
        worker.command('advanced_get', request='a1')
        controls = self.wait(events, 'advanced')['controls']
        self.assertIn('rotate180', controls)
        worker.command('advanced_apply', changes=dict(mirror_vertical=True, saturation=120), request='a2')
        controls = self.wait(events, 'advanced')['controls']
        self.assertTrue(controls['mirror_vertical']['value'])

    def test_mp4_recording_in_process(self):
        folder = self.make_folder()
        worker, events, _ = self.start(folder)
        self.wait(events, 'stats')
        summary = self.record(worker, events, folder)
        self.assertTrue(summary['complete'], summary['errors'])
        self.assertEqual(summary['repeated_frames'], 0)
        self.assertEqual(self.probe_frames(summary['video']), summary['written_frames'])
        saved = json.loads((folder / 'DEMO-01.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['experiment']['experiment_id'], 'PROC-1')
        self.assertIn('mv_worker.py', saved['software']['source_sha256'])

    def test_research_raw_recording_in_process(self):
        folder = self.make_folder()
        worker, events, _ = self.start(folder)
        self.wait(events, 'stats')
        summary = self.record(worker, events, folder, mode='raw', seconds=1)
        self.assertTrue(summary['complete'], summary['errors'])
        self.assertTrue(verify_dataset(summary['data_path'])['valid'])

    def test_camera_fault_finishes_recording_and_closes(self):
        folder = self.make_folder()
        worker, events, _ = self.start(folder)
        self.wait(events, 'stats')
        gate = threading.Event()
        worker.command('prepare', folder=folder, fps=30, settings=Settings(10, 2, True), gate=gate, mode='mp4', experiment={})
        self.wait(events, 'prepared')
        gate.set()
        self.wait(events, 'record_started')
        time.sleep(0.5)
        worker.command('inject_fault')
        self.assertIn('injected', self.wait(events, 'fault', allow=('fault',))['text'])
        summary = self.wait(events, 'record_finished', allow=('fault',))['summary']
        self.assertFalse(summary['complete'])
        self.assertGreater(summary['written_frames'], 0)
        self.wait(events, 'closed', allow=('fault',))
        worker.join(20)
        self.assertFalse(worker.is_alive())

    def test_two_cameras_in_separate_processes(self):
        folder = self.make_folder()
        first, first_events, _ = self.start(folder, 'DEMO-01')
        second, second_events, _ = self.start(folder, 'DEMO-02')
        self.assertNotEqual(first.process.pid, second.process.pid)
        for worker, events in ((first, first_events), (second, second_events)):
            worker.command('close')
            self.wait(events, 'closed')
            worker.join(20)
            self.assertEqual(worker.process.returncode, 0)


if __name__ == '__main__':
    unittest.main()
