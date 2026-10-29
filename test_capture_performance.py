"""Offline regression tests for the real capture bottleneck; no hardware opened."""
import queue
import tempfile
import threading
import time
import unittest
import io
import subprocess
import shutil
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from camera_controls import orient_rgb
from capture_performance import PreviewProcessor
from multi_camera_core import CameraWorker, DemoSystem, Settings, Encoder
from encoding_support import FrameJournal


class PerformanceTests(unittest.TestCase):
    def test_encoder_full_queue_rejects_without_waiting(self):
        encoder = Encoder.__new__(Encoder)
        encoder.error = None
        encoder.queue = queue.Queue(maxsize=1)
        encoder.queue.put('occupied')
        encoder.queue_peak = 1
        from capture_performance import StageTimings
        encoder.enqueue_performance = StageTimings()
        # A blocking queue.put would need a consumer; full put_nowait fails immediately.
        with self.assertRaisesRegex(RuntimeError, '队列已满'):
            encoder.put('next frame')

    def test_encoder_orientation_matches_preview_pixels(self):
        source = Image.new('RGB', (320, 240))
        source.paste((220, 30, 30), (0, 0, 160, 120))
        source.paste((30, 220, 30), (160, 0, 320, 120))
        source.paste((30, 30, 220), (0, 120, 160, 240))
        source.paste((220, 220, 30), (160, 120, 320, 240))
        view = dict(rotate180=True, mirror_horizontal=True, mirror_vertical=False)
        preview = PreviewProcessor.convert(source.tobytes(), source.size, True, view)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'orientation.mp4'
            encoder = Encoder(output, source.size, 60, view=view)
            try:
                for index in range(8):
                    encoder.put((index, (source.tobytes(), index, index), False))
            finally:
                encoder.finish()
            raw = subprocess.check_output([shutil.which('ffmpeg'), '-v', 'error', '-i', str(output), '-frames:v', '1',
                                           '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'],
                                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            decoded = Image.open(io.BytesIO(raw)).convert('RGB')
            for point in ((80, 60), (240, 60), (80, 180), (240, 180)):
                self.assertTrue(all(abs(a-b)<12 for a,b in zip(preview.getpixel(point), decoded.getpixel(point))))

    def test_metadata_writer_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = FrameJournal(Path(folder)/'frames.jsonl')
            journal.write(dict(nonserializable=object()))
            with self.assertRaisesRegex(RuntimeError, '元数据写入失败'):
                journal.close()
            self.assertFalse(journal.thread.is_alive())

    def test_fast_orientation_matches_all_pixel_transforms(self):
        source = Image.frombytes('RGB', (7, 5), bytes(range(105)))
        for rotation in (False, True):
            for horizontal in (False, True):
                for vertical in (False, True):
                    expected = source
                    if rotation:
                        expected = expected.transpose(Image.Transpose.ROTATE_180)
                    if horizontal:
                        expected = expected.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                    if vertical:
                        expected = expected.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                    self.assertEqual(orient_rgb(source.tobytes(), source.size, rotation, horizontal, vertical), expected.tobytes())

    def test_blocked_preview_does_not_block_capture_or_record_stop(self):
        # Isolate preview blocking from GPU discovery startup and simulated clock catch-up.
        from encoding_support import encoder_options
        encoder_options(shutil.which('ffmpeg'))
        entered, release = threading.Event(), threading.Event()
        convert = PreviewProcessor.convert
        def slow_preview(*args):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test preview barrier timed out')
            return convert(*args)
        events, system = queue.Queue(), DemoSystem()
        worker = CameraWorker(system, system.scan()[0], events)
        def wait(kind):
            end = time.monotonic()+8
            while time.monotonic() < end:
                try:
                    serial, event, data = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if event in ('fault', 'prepare_failed', 'command_error'):
                    self.fail(str(data))
                if event == kind:
                    return data
            self.fail('timed out: '+kind)
        with patch.object(PreviewProcessor, 'convert', staticmethod(slow_preview)), tempfile.TemporaryDirectory() as directory:
            try:
                worker.start()
                self.assertTrue(entered.wait(3))
                wait('stats')
                gate = threading.Event()
                worker.command('prepare', folder=directory, fps=30, settings=Settings(13), gate=gate)
                wait('prepared')
                gate.set()
                wait('record_started')
                time.sleep(0.5)
                worker.command('stop')
                result = wait('record_finished')['summary']
                self.assertTrue(result['complete'], result)
                self.assertGreater(result['source_frames_observed'], 15)
                self.assertGreater(result['written_frames'], 8)
                self.assertIn('camera_read_total', result['capture_performance']['worker'])
                self.assertIn('encoder_enqueue_wait', result['encoder_performance']['producer'])
                self.assertFalse(release.is_set())
            finally:
                release.set()
                worker.command('close')
                worker.join(10)
                self.assertFalse(worker.is_alive())
                self.assertFalse(worker.preview_processor.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
