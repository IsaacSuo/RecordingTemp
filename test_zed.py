"""Offline ZED tests: audit logic, protocol, and the real worker process in --simulate mode (no pyzed, no camera)."""
import io
import json
from pathlib import Path
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest

from zed_support import (ZedAudit, ZedSettings, ZedWorker, pack_preview, read_message, unpack_preview,
                         write_message, write_zed_report)

PERIOD = 33_495_000  # measured ZED 2i HD1080@30 interval


def feed(audit, stamps, late=()):
    rows = []
    for index, stamp in enumerate(stamps):
        outside = 40e6 if index in late else 1e6
        rows.append(audit.observe(stamp, index, index, 30e6, outside, None))
    return rows


class AuditTests(unittest.TestCase):
    def test_clean_stream_passes(self):
        audit = ZedAudit(30)
        feed(audit, [1_000_000_123 + i * PERIOD for i in range(100)])
        report = audit.report(dict(frames=100, first_us=audit.first_us, last_us=audit.last_unique_us))
        self.assertEqual(report['status'], 'pass_observed')
        self.assertEqual(report['camera_missing_frames'], 0)
        self.assertAlmostEqual(report['measured_fps'], 1e9 / PERIOD, places=3)

    def test_gaps_split_between_camera_and_application(self):
        audit = ZedAudit(30)
        stamps = [i * PERIOD for i in range(60) if i not in (10, 30, 31)]
        # Frame 32 is position 29 in the thinned list: the application was late before grabbing it.
        feed(audit, stamps, late={29})
        report = audit.report()
        self.assertEqual(report['camera_missing_frames'], 1)
        self.assertEqual(report['app_missing_frames'], 2)
        self.assertEqual(report['app_late_count'], 1)
        self.assertEqual(report['status'], 'review')

    def test_duplicates_are_not_new_frames(self):
        audit = ZedAudit(30)
        rows = feed(audit, [0, PERIOD, PERIOD + 400, 2 * PERIOD])  # same microsecond is a duplicate
        self.assertEqual(audit.count, 3)
        self.assertEqual(audit.duplicates, 1)
        self.assertIsNone(rows[2]['receive_index'])
        self.assertTrue(rows[2]['duplicate'])

    def test_sdk_counter_reset_is_accumulated(self):
        audit = ZedAudit(30)
        for i, total in enumerate([5, 7, 7, 0, 2, 3]):
            audit.observe(i * PERIOD, i, i, 1, 1, total)
        self.assertEqual(audit.sdk_increments, 2 + 2 + 1)
        self.assertEqual(audit.sdk_resets, 1)

    def test_svo_mismatch_and_empty_recording(self):
        audit = ZedAudit(30)
        feed(audit, [i * PERIOD for i in range(10)])
        self.assertEqual(audit.report(dict(frames=9, first_us=audit.first_us, last_us=8 * PERIOD // 1000))['status'], 'review')
        self.assertEqual(audit.report(dict(error='无法打开'))['status'], 'failed')
        self.assertEqual(ZedAudit(30).report()['status'], 'failed')

    def test_report_text_is_utf8(self):
        audit = ZedAudit(30)
        feed(audit, [i * PERIOD for i in range(5)])
        info = dict(serial='S', model='ZED 2i', mode_label='HD1080 @ 30', compression='H264', integrity=audit.report())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'r.txt'
            write_zed_report(path, info)
            self.assertIn('程序处理不足', path.read_text(encoding='utf-8'))


class OpenLockTests(unittest.TestCase):
    def test_second_holder_waits_then_times_out(self):
        from subprocess_worker import OpenLock
        name = 'Local\\recorder-test-lock-' + str(time.monotonic_ns())
        held, release = threading.Event(), threading.Event()

        def holder():
            with OpenLock(name):
                held.set()
                release.wait(5)
        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5)
        with self.assertRaises(TimeoutError):
            with OpenLock(name, timeout_s=0.3):
                pass
        release.set()
        thread.join(5)
        with OpenLock(name, timeout_s=2) as lock:  # free again once the holder released it
            self.assertLess(lock.waited_s, 2)


class ProtocolTests(unittest.TestCase):
    def test_round_trip(self):
        stream = io.BytesIO()
        write_message(stream, b'J', json.dumps(dict(event='x', text='中文')).encode())
        write_message(stream, b'P', pack_preview(dict(width=2, height=1), b'\xff\xd8jpeg'))
        stream.seek(0)
        kind, payload = read_message(stream)
        self.assertEqual((kind, json.loads(payload)['text']), (b'J', '中文'))
        kind, payload = read_message(stream)
        self.assertEqual(unpack_preview(payload), (dict(width=2, height=1), b'\xff\xd8jpeg'))
        self.assertIsNone(read_message(stream))

    def test_settings_validation(self):
        ZedSettings().validate()
        ZedSettings(50, 20).validate()
        for bad in (ZedSettings(50, None), ZedSettings(101, 1), ZedSettings(-1, 0), ZedSettings(True, 3)):
            with self.assertRaises(ValueError):
                bad.validate()


class WorkerProcessTests(unittest.TestCase):
    def make_folder(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)  # runs after the worker shutdown below
        return folder

    def start(self, folder, *extra):
        events = queue.Queue()
        worker = ZedWorker(dict(serial='SIM-ZED', model='sim', kind='zed', simulated=True, extra_args=list(extra)),
                           events, demo=True, python=sys.executable, log_folder=folder)
        worker.start()
        self.addCleanup(self.shutdown, worker)
        self.wait(events, 'connected')
        return worker, events

    def shutdown(self, worker):
        if worker.is_alive():
            worker.command('close')
            worker.join(20)

    def wait(self, events, kind, timeout=20, allow=()):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                serial, name, data = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if name in ('fault', 'prepare_failed') and name not in allow and name != kind:
                self.fail(f'{name}: {data}')
            if name == kind:
                return data
        self.fail(f'timeout waiting for {kind}')

    def record(self, worker, events, folder, seconds=1.5, start=True):
        gate = threading.Event()
        worker.command('prepare', folder=folder, settings=ZedSettings(), gate=gate, mode='mp4', experiment=dict(experiment_id='T'),
                       compression='H264')
        self.wait(events, 'prepared')
        if start:
            gate.set()
            self.wait(events, 'record_started')
            time.sleep(seconds)
        worker.command('stop')
        return self.wait(events, 'record_finished')['summary']

    def test_recording_with_injected_problems(self):
        folder = self.make_folder()
        worker, events = self.start(folder, '--sim-drop-every', '20', '--sim-dup-every', '25', '--sim-app-delay-every', '33',
                                    '--sim-reset-every', '40')
        self.wait(events, 'stats')
        time.sleep(0.3)
        self.assertIsNotNone(worker.take_preview())
        summary = self.record(worker, events, folder, seconds=2)
        integrity = summary['integrity']
        self.assertTrue(summary['complete'], summary['errors'])
        self.assertEqual(integrity['status'], 'review')
        self.assertGreater(integrity['camera_missing_frames'], 0)
        self.assertGreater(integrity['app_missing_frames'], 0)
        self.assertGreater(integrity['duplicates'], 0)
        self.assertGreater(integrity['sdk_counter_resets'], 0)
        self.assertIn(integrity['svo_check']['frames'], (summary['written_frames'], summary['written_frames'] + 1))
        self.assertIsNotNone(summary['stop_padding_us'])
        self.assertEqual(summary['repeated_frames'], 0)
        self.assertEqual(set(summary['locked_from_auto']), {'exposure', 'gain', 'wb_temperature'})
        rows = [json.loads(line) for line in (folder / 'SIM-ZED.received.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows), integrity['frames_unique'] + integrity['duplicates'] + 1)
        self.assertTrue(rows[-1]['stop_padding'])
        self.assertEqual(sum(r['receive_index'] is not None for r in rows), summary['written_frames'])
        saved = json.loads((folder / 'SIM-ZED.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['experiment']['experiment_id'], 'T')
        self.assertIn('zed_worker.py', saved['software']['source_sha256'])
        self.assertTrue((folder / 'SIM-ZED.integrity.txt').is_file())
        worker.command('close')
        self.wait(events, 'closed')
        worker.join(20)
        self.assertEqual(worker.process.returncode, 0)

    def test_settings_locked_while_recording(self):
        folder = self.make_folder()
        worker, events = self.start(folder)
        gate = threading.Event()
        worker.command('prepare', folder=folder, settings=ZedSettings(40, 10), gate=gate, mode='mp4', experiment={},
                       compression='H265')
        self.wait(events, 'prepared')
        gate.set()
        self.wait(events, 'record_started')
        worker.command('apply', settings=ZedSettings(10, 10))
        self.assertIn('锁定', self.wait(events, 'command_error')['text'])
        worker.command('stop')
        summary = self.wait(events, 'record_finished')['summary']
        self.assertEqual(summary['settings'], dict(exposure=40, gain=10, rotate180=False))
        self.assertEqual(summary['locked_from_auto'], dict(wb_temperature=4700))

    def test_stop_before_gate_is_incomplete(self):
        folder = self.make_folder()
        worker, events = self.start(folder)
        summary = self.record(worker, events, folder, start=False)
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['integrity']['status'], 'failed')

    def test_sdk_losing_last_frame_only_costs_the_padding(self):
        folder = self.make_folder()
        worker, events = self.start(folder, '--sim-lose-last')
        summary = self.record(worker, events, folder, seconds=0.8)
        self.assertEqual(summary['integrity']['status'], 'pass_observed', summary['integrity']['warnings'])
        self.assertEqual(summary['integrity']['svo_check']['frames'], summary['written_frames'])

    def test_svo_missing_a_counted_frame_needs_review(self):
        audit = ZedAudit(30)
        feed(audit, [i * PERIOD for i in range(10)])
        padding = 10 * PERIOD // 1000
        ok = audit.report(dict(frames=11, first_us=0, last_us=padding), padding_us=padding)
        self.assertEqual(ok['status'], 'pass_observed')
        short = audit.report(dict(frames=9, first_us=0, last_us=8 * PERIOD // 1000), padding_us=padding)
        self.assertEqual(short['status'], 'review')

    def test_advanced_controls_and_preset(self):
        folder = self.make_folder()
        worker, events = self.start(folder, '--sim-led-fail')
        worker.command('advanced_get', request='r1')
        controls = self.wait(events, 'advanced')['controls']
        self.assertFalse(controls['led']['supported'])
        self.assertFalse(controls['flip']['supported'])
        worker.command('advanced_apply', changes=dict(exposure=30, gain=5, wb_temperature=5230, saturation=6), request='r2')
        controls = self.wait(events, 'advanced')['controls']
        self.assertEqual((controls['aec_agc']['value'], controls['exposure']['value']), (False, 30))
        self.assertEqual((controls['wb_auto']['value'], controls['wb_temperature']['value']), (False, 5200))
        worker.command('advanced_apply', changes=dict(hue=99), request='r3')
        self.assertIn('范围', self.wait(events, 'advanced_failed')['text'])
        worker.command('save_preset', folder=folder, settings=ZedSettings(30, 5))
        self.wait(events, 'notice')
        worker.command('advanced_apply', changes=dict(saturation=1), request='r4')
        self.wait(events, 'advanced')
        worker.command('load_preset', folder=folder)
        self.wait(events, 'notice')
        worker.command('advanced_get', request='r5')
        self.assertEqual(self.wait(events, 'advanced')['controls']['saturation']['value'], 6)

    def test_process_death_reports_incomplete_recording(self):
        folder = self.make_folder()
        worker, events = self.start(folder)
        gate = threading.Event()
        worker.command('prepare', folder=folder, settings=ZedSettings(), gate=gate, mode='mp4', experiment={},
                       compression='H264')
        self.wait(events, 'prepared')
        gate.set()
        self.wait(events, 'record_started')
        worker.process.kill()
        self.wait(events, 'fault', allow=('fault',))
        summary = self.wait(events, 'record_finished')['summary']
        self.assertFalse(summary['complete'])
        self.wait(events, 'closed')


if __name__ == '__main__':
    unittest.main()
