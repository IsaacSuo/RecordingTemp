"""Offline timing and encoder tests; never load the camera SDK."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from record_video import Timeline, VideoSink


class TimingTests(unittest.TestCase):
    def test_100fps_source_to_30_and_60(self):
        for fps in (30, 60):
            t = Timeline(fps, fps * 2)
            frames = []
            for i in range(201):
                frames.extend(t.add(i * 100, i))
            self.assertEqual(len(frames), fps * 2)
            self.assertEqual(t.duplicates, 0)
            self.assertEqual([x[0] for x in frames], list(range(fps * 2)))
            for slot, source, _ in frames:
                self.assertLess(abs(source / 100 - slot / fps), 0.011)

    def test_insufficient_source_counts_repeats(self):
        t = Timeline(60, 120)
        for i in range(101):
            t.add(i * 200, i)
        self.assertEqual(t.next_index, 120)
        self.assertEqual(t.duplicates, 20)

    def test_wrap_and_invalid_timestamps(self):
        t = Timeline(30, 10)
        t.add(0xffffff00, 'first')
        t.add((0xffffff00 + 400) & 0xffffffff, 'second')
        self.assertEqual(t.elapsed, 400)
        self.assertEqual(t.next_index, 2)
        with self.assertRaises(RuntimeError):
            t.add(t.previous, 'invalid')
        with self.assertRaises(RuntimeError):
            t.add((t.previous - 1) & 0xffffffff, 'backwards')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class EncoderTests(unittest.TestCase):
    def test_real_mp4_at_both_rates(self):
        with tempfile.TemporaryDirectory(prefix='mindvision-encoder-') as directory:
            for fps in (30, 60):
                output = Path(directory) / f'{fps}.mp4'
                sink = VideoSink(shutil.which('ffmpeg'), output, 160, 120, fps)
                for i in range(fps * 2):
                    sink.put(bytes((i % 256, 90, 170)) * (160 * 120))
                sink.finish()
                self.assertEqual(sink.written, fps * 2)
                info = json.loads(subprocess.check_output([
                    shutil.which('ffprobe'), '-v', 'error', '-count_frames',
                    '-select_streams', 'v:0', '-show_entries',
                    'stream=nb_read_frames,duration,avg_frame_rate', '-of', 'json', str(output)]))
                stream = info['streams'][0]
                self.assertEqual(int(stream['nb_read_frames']), fps * 2)
                self.assertEqual(stream['avg_frame_rate'], f'{fps}/1')
                self.assertAlmostEqual(float(stream['duration']), 2, places=3)


if __name__ == '__main__':
    unittest.main()
