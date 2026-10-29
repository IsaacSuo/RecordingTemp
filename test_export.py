"""Offline tests of ZED export (zed_export.py --simulate) and the background export queue. No pyzed, no camera."""
import csv
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from export_queue import ExportQueue, completed_export

ROOT = Path(__file__).resolve().parent
PERIOD = 33_495_000
T0 = 1_792_000_000_000_000_000


def fake_recording(folder, frames=60, gaps=(20, 40, 41), serial='SIM', stamps=None):
    if stamps is None:
        stamps = [T0 + i * PERIOD for i in range(frames) if i not in gaps]
    svo = folder / f'{serial}.svo2'
    svo.write_text('SIMULATED-SVO2 H264\n' + ''.join(f'{t // 1000}\n' for t in stamps), encoding='utf-8')
    with svo.with_suffix('.received.jsonl').open('w', encoding='utf-8') as stream:
        for index, stamp in enumerate(stamps):
            stream.write(json.dumps(dict(receive_index=index, camera_timestamp_ns=stamp)) + '\n')
    (folder / 'batch.json').write_text(json.dumps(dict(recordings={}, exports={})), encoding='utf-8')
    return svo, stamps


def run_export(svo, *extra):
    output = subprocess.run([sys.executable, '-B', str(ROOT / 'zed_export.py'), str(svo), '--simulate', *extra],
                            capture_output=True, timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return output.returncode, json.loads(output.stdout.decode('utf-8').strip().splitlines()[-1])


def probe_frames(path):
    output = subprocess.check_output([shutil.which('ffprobe'), '-v', 'error', '-count_frames', '-select_streams', 'v:0',
                                      '-show_entries', 'stream=nb_read_frames', '-of', 'json', str(path)],
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return int(json.loads(output)['streams'][0]['nb_read_frames'])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def test_gaps_are_filled_marked_and_counted(self):
        svo, stamps = fake_recording(self.folder)
        code, result = run_export(svo)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((result['source_frames'], result['repeated_frames'], result['output_frames']), (57, 3, 60))
        self.assertAlmostEqual(result['output_fps'], 1e9 / PERIOD, places=3)
        self.assertAlmostEqual(result['mp4_duration_s'], result['capture_span_s'], places=6)
        export = self.folder / 'export'
        self.assertEqual(sorted(p.name for p in export.glob('*.mp4')), ['left.mp4', 'right.mp4'])
        for name in ('left.mp4', 'right.mp4'):
            self.assertEqual(probe_frames(export / name), 60)
        with (export / 'timestamps.csv').open(encoding='utf-8') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 60)
        self.assertEqual(sum(int(r['repeated']) for r in rows), 3)
        self.assertEqual([int(r['camera_timestamp_ns']) for r in rows if r['repeated'] == '0'], stamps)
        saved = json.loads((export / 'export.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['depth_mode'], 'none')

    def test_drifting_intervals_never_drop_a_source_frame(self):
        import math
        stamps, t = [], T0
        for i in range(1800):  # interval wanders around the median like the real camera; no frame is missing
            stamps.append(t)
            t += round(PERIOD * (1 + 0.004 * math.sin(i / 37)) + (i % 3 - 1) * 300_000)
        svo, _ = fake_recording(self.folder, stamps=stamps)
        code, result = run_export(svo)
        self.assertEqual(code, 0, result)
        self.assertEqual((result['source_frames'], result['output_frames'], result['repeated_frames'], result['skipped_frames']),
                         (1800, 1800, 0, 0))
        self.assertLess(abs(result['mp4_duration_s'] - result['capture_span_s']), 2 / result['output_fps'])

    def test_depth_option_and_no_overwrite(self):
        svo, _ = fake_recording(self.folder, frames=20, gaps=())
        code, result = run_export(svo, '--depth', 'NEURAL_LIGHT')
        self.assertEqual(code, 0, result)
        folder = self.folder / 'export_NEURAL_LIGHT'
        self.assertTrue((folder / 'depth.mp4').is_file())
        self.assertEqual(json.loads((folder / 'export.json').read_text(encoding='utf-8'))['depth_mode'], 'NEURAL_LIGHT')
        code, result = run_export(svo, '--depth', 'NEURAL_LIGHT')
        self.assertEqual((code, result['status']), (1, 'failed'))
        self.assertIn('已存在', result['error'])

    def test_interrupted_partial_output_is_replaced(self):
        svo, _ = fake_recording(self.folder, frames=10, gaps=())
        partial = self.folder / 'export.partial'
        partial.mkdir()
        (partial / 'left.mp4').write_bytes(b'broken')
        code, result = run_export(svo)
        self.assertEqual(code, 0, result)
        self.assertFalse(partial.exists())
        self.assertEqual(probe_frames(self.folder / 'export' / 'left.mp4'), 10)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.events = queue.Queue()
        self.exports = ExportQueue(self.events, demo=True, log_folder=self.folder / 'logs')
        self.addCleanup(self.exports.close)

    def wait(self, kind, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                _, name, data = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            if name == kind:
                return data
        self.fail(f'timeout waiting for {kind}')

    def job(self, svo):
        return dict(serial='SIM', name='正面', svo=str(svo), depth=None, batch_folder=str(self.folder), simulated=True)

    def test_finished_export_is_recorded_in_batch(self):
        svo, _ = fake_recording(self.folder, frames=30, gaps=(5,))
        self.exports.add(self.job(svo))
        result = self.wait('export_finished')['result']
        self.assertEqual(result['status'], 'completed')
        batch = json.loads((self.folder / 'batch.json').read_text(encoding='utf-8'))
        self.assertEqual(batch['exports']['SIM']['status'], 'completed')
        self.assertEqual(batch['exports']['SIM']['repeated_frames'], 1)
        self.assertFalse(self.exports.busy)

    def test_pause_restarts_export_after_resume(self):
        svo, _ = fake_recording(self.folder, frames=2400, gaps=())
        self.exports.add(self.job(svo))
        self.wait('export_progress')
        time.sleep(1.0)
        self.assertTrue(self.exports.pause())
        self.wait('export_paused')
        self.assertFalse((self.folder / 'export').exists())
        self.exports.resume()
        result = self.wait('export_finished', timeout=300)['result']
        self.assertEqual((result['status'], result['output_frames']), ('completed', 2400))
        self.assertFalse((self.folder / 'export.partial').exists())

    def test_export_finished_before_pause_is_not_redone(self):
        svo, _ = fake_recording(self.folder, frames=30, gaps=())
        job = self.job(svo)
        self.assertIsNone(completed_export(job, None))
        self.exports.add(dict(job))
        self.assertEqual(self.wait('export_finished')['result']['status'], 'completed')
        # pause killed the process after export.partial was renamed but before it reported
        done = completed_export(job, None)
        self.assertEqual((done['status'], done['output_frames'], done['folder']), ('completed', 30, str(self.folder / 'export')))
        self.assertIsNone(completed_export(dict(job, depth='NEURAL'), None))

    def test_parallel_exports_pause_together_and_resume(self):
        jobs = []
        for serial in ('A', 'B'):
            folder = self.folder / serial
            folder.mkdir()
            svo, _ = fake_recording(folder, frames=6000, gaps=(), serial=serial)  # long enough to be paused mid-way
            jobs.append(dict(serial=serial, name=serial, svo=str(svo), depth=None, batch_folder=str(folder), simulated=True))
        for job in jobs:
            self.exports.add(job)
        deadline = time.monotonic() + 60
        while len(self.exports.running) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.exports.running), 2)  # both run at once
        time.sleep(0.5)
        self.assertTrue(self.exports.pause())
        paused = {self.wait('export_paused')['job']['serial'], self.wait('export_paused')['job']['serial']}
        self.assertEqual(paused, {'A', 'B'})
        self.assertEqual(self.exports.pending(), 2)
        self.exports.resume()
        finished = {}
        for _ in range(2):
            data = self.wait('export_finished', timeout=300)
            finished[data['job']['serial']] = data['result']
        for serial in ('A', 'B'):
            self.assertEqual((finished[serial]['status'], finished[serial]['output_frames']), ('completed', 6000))
            batch = json.loads((self.folder / serial / 'batch.json').read_text(encoding='utf-8'))
            self.assertEqual(batch['exports'][serial]['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
