"""Offline verification: no camera or SDK is accessed by these tests."""
import csv
import json
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import gc
from types import SimpleNamespace

from multi_camera_core import CameraWorker, DemoSystem, DemoCamera, Recording, Settings, SDKCamera


class RecordingTests(unittest.TestCase):
    def probe(self, path):
        return json.loads(subprocess.check_output([
            shutil.which('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-count_frames',
            '-show_entries', 'stream=width,height,r_frame_rate,nb_read_frames,duration', '-of', 'json', str(path)
        ], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))['streams'][0]

    def test_settings_reject_invalid_exposure_and_gain(self):
        for settings in (Settings(0), Settings(float('nan')), Settings(10, float('inf'))):
            with self.assertRaises(ValueError):
                settings.validate()
        with self.assertRaises(ValueError):
            Settings(20).validate(60)
        Settings(10).validate(60)

    def test_missing_frames_preserve_duration_and_are_logged(self):
        with tempfile.TemporaryDirectory() as folder:
            info = dict(serial='TEST', model='synthetic')
            record = Recording(folder, info, (160, 120), 60, Settings(10), demo=True)
            # Exactly two seconds of 50 FPS source at camera timestamps, encoded at 60 FPS.
            pixels = bytes([130, 90, 40]) * (160*120)
            try:
                for tick in range(0, 20000, 200):
                    # This fixture feeds faster than real time; production capture never waits.
                    while record.encoder.queue.qsize() > record.encoder.queue.maxsize-4:
                        if record.encoder.error:
                            raise record.encoder.error
                        time.sleep(0.001)
                    if record.wants(tick):
                        record.add(tick, (160, 120), pixels)
            finally:
                summary = record.finish()
            self.assertTrue(summary['complete'])
            self.assertEqual(summary['source_fps'], 50)
            self.assertGreater(summary['repeated_frames'], 0)
            stream = self.probe(record.output)
            self.assertEqual(int(stream['nb_read_frames']), summary['written_frames'])
            self.assertLess(abs(float(stream['duration'])-2), 1/60+0.001)
            with record.output.with_suffix('.frames.csv').open(encoding='utf-8') as ledger:
                rows = list(csv.DictReader(ledger))
            self.assertEqual(sum(int(row['repeated']) for row in rows), summary['repeated_frames'])

    def test_timestamp_wrap_and_jump(self):
        with tempfile.TemporaryDirectory() as folder:
            record = Recording(folder, dict(serial='WRAP', model='synthetic'), (160, 120), 30, Settings(), demo=True)
            pixels = bytes(160*120*3)
            for tick in (0xfffffff0, 400):
                if record.wants(tick):
                    record.add(tick, (160, 120), pixels)
            self.assertEqual(record.elapsed_ticks, 416)
            with self.assertRaises(RuntimeError):
                record.wants(400)
            summary = record.finish('injected timestamp failure')
            self.assertFalse(summary['complete'])
            self.assertEqual(int(self.probe(record.output)['nb_read_frames']), summary['written_frames'])

    def wait_events(self, events, predicate, timeout=10):
        deadline = time.monotonic()+timeout
        observed = []
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=0.1)
            except queue.Empty:
                continue
            observed.append(event)
            if event[1] == 'fault':
                self.fail(str(event))
            if predicate(observed):
                return observed
        self.fail(f'Timed out: {observed[-8:]}')

    def test_two_cameras_start_stop_repeat_and_shutdown(self):
        system = DemoSystem()
        events = queue.Queue()
        workers = [CameraWorker(system, info, events) for info in system.scan()]
        for worker in workers:
            worker.start()
        try:
            self.wait_events(events, lambda rows: len({s for s, k, d in rows if k == 'stats' and d['shape']}) == 2)
            with tempfile.TemporaryDirectory() as folder:
                for fps in (30, 60):
                    output = Path(folder) / str(fps)
                    output.mkdir()
                    gate = threading.Event()
                    for worker in workers:
                        worker.command('prepare', folder=output, fps=fps, settings=Settings(10), gate=gate)
                    prepared = self.wait_events(events, lambda rows: sum(k == 'prepared' for _, k, _ in rows) == 2)
                    self.assertFalse(any(k == 'record_started' for _, k, _ in prepared))
                    gate.set()
                    time.sleep(1.3)
                    for worker in workers:
                        worker.command('stop')
                    results = self.wait_events(events, lambda rows: sum(k == 'record_finished' for _, k, _ in rows) == 2)
                    summaries = [d['summary'] for _, k, d in results if k == 'record_finished']
                    for summary in summaries:
                        self.assertTrue(summary['complete'], summary)
                        self.assertEqual(summary['repeated_frames'], 0, summary)
                        stream = self.probe(summary['video'])
                        self.assertEqual(stream['r_frame_rate'], f'{fps}/1')
                        self.assertEqual(int(stream['nb_read_frames']), summary['written_frames'])
                        self.assertGreater(float(stream['duration']), 1)
        finally:
            for worker in workers:
                worker.command('close')
            for worker in workers:
                worker.join(10)
                self.assertFalse(worker.is_alive())

    def test_prepare_failure_keeps_preview_available(self):
        system, events = DemoSystem(), queue.Queue()
        worker = CameraWorker(system, system.scan()[0], events)
        worker.start()
        try:
            self.wait_events(events, lambda rows: any(k == 'stats' and d['shape'] for _, k, d in rows))
            with tempfile.TemporaryDirectory() as folder:
                worker.command('prepare', folder=folder, fps=60, settings=Settings(20), gate=threading.Event())
                self.wait_events(events, lambda rows: any(k == 'prepare_failed' for _, k, _ in rows))
                self.assertTrue(worker.is_alive())
                self.assertIsNone(worker.session)
                self.assertFalse(list(Path(folder).glob('*.mp4')))
        finally:
            worker.command('close')
            worker.join(10)

    def test_sdk_raw_buffer_is_released_on_isp_error(self):
        released = []
        sdk = SimpleNamespace(CameraException=RuntimeError, CAMERA_STATUS_TIME_OUT=-12,
                              CameraGetImageBuffer=lambda h, t: (1234, SimpleNamespace(uiTimeStamp=100, uBytes=4)),
                              CameraImageProcess=lambda *args: -7,
                              CameraReleaseImageBuffer=lambda h, p: released.append(p) or 0)

        def check(code):
            if code:
                raise RuntimeError('injected ISP failure')

        camera = SDKCamera.__new__(SDKCamera)
        camera.s, camera.h, camera.buffer = sdk, 1, 5678
        camera.sys = SimpleNamespace(check=check)
        with self.assertRaisesRegex(RuntimeError, 'injected ISP'):
            camera.read(lambda tick: True)
        self.assertEqual(released, [1234])

    def test_gui_camera_fault_stops_other_camera_and_saves(self):
        import tkinter as tk
        from multi_camera_app import RecorderApp
        fail = threading.Event()

        class FaultCamera(DemoCamera):
            def read(self, wanted):
                if fail.is_set() and self.info['native'] == 0:
                    raise OSError('injected camera disconnection')
                return super().read(wanted)

        class FaultSystem(DemoSystem):
            def open(self, info):
                return FaultCamera(info)

        root = tk.Tk()
        root.withdraw()
        # The simulated ZED's auto-export is killed by close(); its encoder logs may stay open for a moment.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            app = RecorderApp(root, demo=True, output=folder)
            app.isolate_mindvision = False  # the injected fault lives in this process
            app.system = FaultSystem()

            def pump(predicate, seconds=10):
                until = time.monotonic()+seconds
                while time.monotonic() < until:
                    root.update()
                    if predicate():
                        return
                    time.sleep(0.01)
                self.fail('UI condition timed out')

            try:
                app.scan()
                pump(lambda: app.scan_complete)
                app.connect_selected()
                pump(lambda: all(c.ready for c in app.cards.values()))
                app.start_recording()
                pump(lambda: app.phase == 'recording')
                mindvision = [c for c in app.cards.values() if c.kind == 'mindvision']
                pump(lambda: all(c.worker.session and c.worker.session.encoder.written > 10 for c in mindvision))
                fail.set()
                pump(lambda: app.phase == 'idle')
                self.assertEqual(len(app.completed_batches), 1)
                results = app.completed_batches[0]['summaries']
                self.assertFalse(results['DEMO-01']['complete'])
                self.assertTrue(results['DEMO-02']['complete'])
                # The simulated ZED in the same batch is stopped by the MindVision fault and still saved.
                self.assertGreater(results['DEMO-ZED-01']['written_frames'], 0)
                self.assertTrue(Path(results['DEMO-ZED-01']['video']).is_file())
                for serial in ('DEMO-01', 'DEMO-02'):
                    self.assertGreater(int(self.probe(results[serial]['video'])['nb_read_frames']), 0)
                self.assertIn('injected camera disconnection', app.last_error)
            finally:
                app.close()
                # poll() destroys the window after workers release their encoders.
                until = time.monotonic()+10
                while time.monotonic() < until:
                    try:
                        root.update()
                        if not root.winfo_exists():
                            break
                    except tk.TclError:
                        break
                    time.sleep(0.02)
                until = time.monotonic()+10
                while app.exports.busy and time.monotonic() < until:
                    time.sleep(0.05)
                self.assertTrue(all(not c.worker.is_alive() for c in app.cards.values()))
            # Tk objects must be finalized on this main thread before later tests
            # allocate enough objects for a worker-thread garbage collection.
            del pump
            del app
            del root
            gc.collect()

    def leftover_workers(self, marker):
        """Camera processes still running whose command line contains marker (both launcher and interpreter)."""
        script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*" + marker + "*' -and "
                  "$_.Name -match '^pythonw?\\.exe$' } | ForEach-Object { $_.ProcessId }")
        output = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True, text=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
        return [line for line in output.split() if line.strip()]

    def test_disconnect_and_close_while_zed_is_still_opening(self):
        import tkinter as tk
        from multi_camera_app import RecorderApp
        marker = '--sim-open-delay 3601.5'
        for action in ('disconnect', 'close'):
            root = tk.Tk()
            root.withdraw()
            with tempfile.TemporaryDirectory() as folder:
                app = RecorderApp(root, demo=True, output=folder)

                def pump(predicate, seconds=30):
                    until = time.monotonic() + seconds
                    while time.monotonic() < until:
                        try:
                            root.update()
                            if predicate():
                                return True
                        except tk.TclError:  # the window has been destroyed
                            return True
                        time.sleep(0.01)
                    return False
                app.scan()
                self.assertTrue(pump(lambda: app.scan_complete))
                zed = app.cards['DEMO-ZED-01']
                zed.info['extra_args'] = marker.split()
                app.connect_selected()
                self.assertTrue(pump(lambda: all(c.ready for k, c in app.cards.items() if k != 'DEMO-ZED-01')))
                self.assertTrue(pump(lambda: len(self.leftover_workers(marker)) > 0, 20))
                self.assertFalse(zed.connected)  # still "opening"
                started = time.monotonic()
                if action == 'disconnect':
                    app.disconnect_all()
                    self.assertTrue(pump(lambda: not app.disconnecting, 15), 'disconnect stayed busy')
                    self.assertLess(time.monotonic() - started, 15)
                    self.assertEqual(str(app.scan_button.cget('state')), 'normal')  # UI usable again
                    app.close()
                else:
                    app.close()
                self.assertTrue(pump(lambda: not root.winfo_exists(), 20), 'window did not close')
                self.assertLess(time.monotonic() - started, 20)
                self.assertTrue(pump(lambda: not self.leftover_workers(marker), 10), 'camera process left running')
                del app, pump
            gc.collect()

    def test_gui_marks_camera_without_images(self):
        import tkinter as tk
        from multi_camera_app import RecorderApp
        root = tk.Tk()
        root.withdraw()
        with tempfile.TemporaryDirectory() as folder:
            app = RecorderApp(root, demo=True, output=folder)
            app.NO_IMAGE_WARN_S = 2

            def pump(predicate, seconds=40):
                until = time.monotonic() + seconds
                while time.monotonic() < until:
                    root.update()
                    if predicate():
                        return
                    time.sleep(0.01)
                self.fail('UI condition timed out')
            try:
                app.scan()
                pump(lambda: app.scan_complete)
                zed = app.cards['DEMO-ZED-01']
                zed.info['extra_args'] = ['--sim-freeze-after', '3']
                app.connect_selected()
                pump(lambda: all(c.ready for c in app.cards.values()))
                pump(lambda: getattr(zed, 'stalled', False), 20)
                self.assertEqual(zed.state.cget('text'), '无画面')
                self.assertIn('USB', app.last_error)
                self.assertFalse(getattr(app.cards['DEMO-01'], 'stalled', False))
            finally:
                app.CLOSE_TIMEOUT_S = 3
                app.close()
                until = time.monotonic() + 30
                while time.monotonic() < until:
                    try:
                        root.update()
                        if not root.winfo_exists():
                            break
                    except tk.TclError:
                        break
                    time.sleep(0.02)
            del app, root, pump
            gc.collect()

    def test_gui_gives_up_on_camera_stuck_in_prepare(self):
        import tkinter as tk
        from multi_camera_app import RecorderApp
        root = tk.Tk()
        root.withdraw()
        with tempfile.TemporaryDirectory() as folder:
            app = RecorderApp(root, demo=True, output=folder)
            app.PREPARE_TIMEOUT_S, app.STOP_TIMEOUT_S = 3, 3

            def pump(predicate, seconds=40):
                until = time.monotonic() + seconds
                while time.monotonic() < until:
                    root.update()
                    if predicate():
                        return
                    time.sleep(0.01)
                self.fail('UI condition timed out')
            try:
                app.scan()
                pump(lambda: app.scan_complete)
                app.cards['DEMO-ZED-01'].info['extra_args'] = ['--sim-hang-prepare']
                app.connect_selected()
                pump(lambda: all(c.ready for c in app.cards.values()))
                app.start_recording()
                pump(lambda: app.phase == 'idle' and app.completed_batches, 60)
                batch = app.completed_batches[-1]
                self.assertTrue(any('没有准备好' in e for e in batch['errors']), batch['errors'])
                zed = batch['summaries']['DEMO-ZED-01']
                self.assertFalse(zed['complete'])
                self.assertIn('已结束该相机进程', zed['errors'][0])
                for serial in ('DEMO-01', 'DEMO-02'):  # the other cameras were stopped cleanly and kept their files
                    self.assertIsNotNone(batch['summaries'][serial])
                    card = app.cards[serial]  # once the batch is over, warning borders return to normal
                    self.assertIsNone(card.issue)
                    self.assertNotIn(str(card.frame.cget('highlightbackground')).lower(), ('#f2c06b', '#ff7b86'))
                pump(lambda: not app.cards['DEMO-ZED-01'].worker.is_alive(), 20)
            finally:
                app.close()
                until = time.monotonic() + 30
                while time.monotonic() < until:
                    try:
                        root.update()
                        if not root.winfo_exists():
                            break
                    except tk.TclError:
                        break
                    time.sleep(0.02)
            del app, root, pump
            gc.collect()


if __name__ == '__main__':
    unittest.main(verbosity=2)
