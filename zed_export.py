"""Export a recorded ZED SVO2 to watchable MP4s next to the recording.

Runs in .venv-zed (live) or in any Python with Pillow (--simulate, for tests and the demo mode).
Output folder: <svo folder>/export (or export_<DEPTH_MODE>), written as *.partial and renamed when done.
  left.mp4 / right.mp4   rectified left and right eye, H.264
  depth.mp4              only with --depth NEURAL or NEURAL_LIGHT: pseudo-colour depth aligned to rectified left;
                         a visualisation, not metric values
  timestamps.csv         every MP4 frame: source SVO frame, camera timestamp, repeated flag
  export.json            settings, counts, timing and errors
The MP4 runs at the camera's measured frame rate (e.g. 29.855 fps, from the recording's timestamps), so its
duration matches the capture. Where the camera dropped frames the previous frame is repeated, marked in the CSV
and counted as repeated_frames; this is separate from the recording's camera-side loss statistics.
Progress: one JSON object per line on stdout ({"event": "progress", ...}, then {"event": "finished", ...}).
"""
from subprocess_worker import boost_process, protect_stdout
PROTO = protect_stdout()
PROCESS_BOOST = boost_process()  # windowless background work would otherwise land on efficiency cores

import argparse  # noqa: E402
import csv  # noqa: E402
from datetime import datetime  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import queue  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402
import shutil  # noqa: E402
import statistics  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402

from encoding_support import encoder_options  # noqa: E402
from capture_performance import StageTimings  # noqa: E402
from research_data import clean_json, software_info  # noqa: E402

DEPTH_MODES = ('NEURAL', 'NEURAL_LIGHT')


def emit(event, **data):
    PROTO.write((json.dumps(clean_json(dict(event=event, **data)), ensure_ascii=False, default=str) + '\n').encode('utf-8'))


class Mp4Writer:
    """One ffmpeg encoder fed by its own thread, so decoding the next frame overlaps writing this one."""

    def __init__(self, ffmpeg, codec_args, path, width, height, fps):
        self.path = path
        self.error = None
        self.queue = queue.Queue(maxsize=4)
        self.log = path.with_suffix('.ffmpeg.log').open('wb')
        self.process = subprocess.Popen(
            [ffmpeg, '-hide_banner', '-loglevel', 'warning', '-nostdin', '-n', '-f', 'rawvideo', '-pixel_format', 'bgr24',
             '-video_size', f'{width}x{height}', '-framerate', f'{fps:.6f}', '-i', 'pipe:0', '-an', *codec_args,
             '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.thread = threading.Thread(target=self._feed, daemon=True, name='feed-' + path.stem)
        self.thread.start()

    def _feed(self):
        try:
            while True:
                data = self.queue.get()
                if data is None:
                    return
                self.process.stdin.write(data)
        except Exception as exc:
            self.error = exc

    def write(self, data):
        if self.error:
            raise RuntimeError(f'写入编码器失败：{self.error}')
        self.queue.put(data)

    def close(self):
        try:
            self.queue.put(None)
            self.thread.join()
            if self.error:
                raise RuntimeError(f'写入编码器失败：{self.error}')
            self.process.stdin.close()
            code = self.process.wait(timeout=120)
        finally:
            self.log.close()
        if code:
            raise RuntimeError(f'FFmpeg 编码失败（{code}），详见 {self.path.with_suffix(".ffmpeg.log").name}')


def slots(interval, period):
    """Output frames one source interval occupies: 1 normally, n where n-1 camera frames are missing."""
    return max(1, round(interval / period))


def recorded_timestamps(svo):
    """Counted frames from the recorder's per-frame log, used for the frame rate before decoding."""
    log = svo.with_suffix('.received.jsonl')
    stamps = []
    if log.is_file():
        with log.open(encoding='utf-8') as stream:
            for line in stream:
                row = json.loads(line)
                if row.get('receive_index') is not None:
                    stamps.append(row['camera_timestamp_ns'])
    return stamps


class LiveSource:
    def __init__(self, svo, depth, timings):
        self.timings = timings
        os.add_dll_directory(str(Path(os.environ.get('ZED_SDK_ROOT_DIR', r'C:\Program Files (x86)\ZED SDK')) / 'bin'))
        import pyzed.sl as sl
        self.sl = sl
        self.camera = sl.Camera()
        init = sl.InitParameters()
        init.set_from_svo_file(str(svo))
        init.svo_real_time_mode = False
        init.depth_mode = getattr(sl.DEPTH_MODE, depth) if depth else sl.DEPTH_MODE.NONE
        init.coordinate_units = sl.UNIT.METER
        code = self.camera.open(init)
        if code != sl.ERROR_CODE.SUCCESS:
            raise RuntimeError(f'无法打开 SVO2：{code}')
        info = self.camera.get_camera_information()
        config = info.camera_configuration
        self.width, self.height, self.nominal_fps = config.resolution.width, config.resolution.height, config.fps
        self.serial = info.serial_number
        self.total = self.camera.get_svo_number_of_frames()
        self.runtime = sl.RuntimeParameters()
        self.views = [('left', sl.VIEW.LEFT_BGR), ('right', sl.VIEW.RIGHT_BGR)] + ([('depth', sl.VIEW.DEPTH_BGR)] if depth else [])
        self.mats = {name: sl.Mat() for name, _ in self.views}
        self.sdk_version = sl.Camera.get_sdk_version()

    def frames(self):
        sl = self.sl
        measured = self.timings.call
        while True:
            code = measured('grab_decode', self.camera.grab, self.runtime)
            if code == sl.ERROR_CODE.END_OF_SVOFILE_REACHED:
                return
            if code != sl.ERROR_CODE.SUCCESS:
                raise RuntimeError(f'解码失败：{code}')
            stamp = self.camera.get_timestamp(sl.TIME_REFERENCE.IMAGE).get_nanoseconds()
            images = {}
            for name, view in self.views:
                code = measured('retrieve_' + name, self.camera.retrieve_image, self.mats[name], view)
                if code != sl.ERROR_CODE.SUCCESS:
                    raise RuntimeError(f'取图失败 {name}：{code}')
                data = self.mats[name].get_data()
                if data.shape != (self.height, self.width, 3):
                    raise RuntimeError(f'图像格式异常 {name}：{data.shape}')
                images[name] = measured('copy_' + name, data.tobytes)
            yield self.camera.get_svo_position(), stamp, images

    def close(self):
        self.camera.close()


class SimulatedSource:
    """Reads the simulated recorder's .svo2 (one microsecond timestamp per line) and draws frames."""

    def __init__(self, svo, depth, timings):
        lines = svo.read_text(encoding='utf-8').splitlines()
        self.stamps = [int(line) * 1000 for line in lines[1:]]
        self.width, self.height, self.nominal_fps = 320, 180, 30
        self.serial = svo.stem
        self.total = len(self.stamps)
        self.views = ['left', 'right'] + (['depth'] if depth else [])
        self.sdk_version = 'simulated'

    def frames(self):
        from PIL import Image, ImageDraw
        for position, stamp in enumerate(self.stamps):
            images = {}
            for index, name in enumerate(self.views):
                image = Image.new('RGB', (self.width, self.height), (30 + 60 * index, 40, 60))
                ImageDraw.Draw(image).text((10, 10), f'{name} {position}', fill='white')
                images[name] = image.tobytes()  # channel order is irrelevant for synthetic frames
            yield position, stamp, images

    def close(self):
        pass


def export(svo, output, depth, simulate, ffmpeg, max_frames=None):
    started = time.monotonic()
    timings = StageTimings()
    partial = output.with_name(output.name + '.partial')
    if output.exists():
        raise RuntimeError(f'{output} 已存在，未覆盖；如需重新导出请先移走该文件夹')
    for attempt in range(20):  # left by an interrupted export; its encoders may still be closing files
        if not partial.exists():
            break
        try:
            shutil.rmtree(partial)
        except OSError:
            if attempt == 19:
                raise
            time.sleep(0.5)
    partial.mkdir(parents=True)
    source = (SimulatedSource if simulate else LiveSource)(svo, depth, timings)
    opened = time.monotonic()
    stamps = recorded_timestamps(svo)
    intervals = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
    period = statistics.median(intervals) if intervals else 1e9 / source.nominal_fps
    # Output slots per source interval: 1, or more where the camera dropped frames. The rate is chosen so
    # the MP4 spans the same time as the capture; every source frame is written exactly once.
    planned = sum(slots(interval, period) for interval in intervals)
    fps = planned * 1e9 / (stamps[-1] - stamps[0]) if planned and stamps[-1] > stamps[0] else 1e9 / period
    codec_args, codec = encoder_options(ffmpeg)
    writers = {name: Mp4Writer(ffmpeg, codec_args, partial / f'{name}.mp4', source.width, source.height, fps)
               for name in (source.views if simulate else [name for name, _ in source.views])}
    summary = dict(schema='zed-export/1', status='running', source=str(svo), serial=str(source.serial), depth_mode=depth or 'none',
                   depth_note='depth.mp4 is the SDK pseudo-colour visualisation aligned to rectified left, not metric depth' if depth else None,
                   images='Rectified LEFT and RIGHT eye', width=source.width, height=source.height,
                   nominal_fps=source.nominal_fps, output_fps=fps, period_ns=period, encoder=codec,
                   sdk_version=source.sdk_version, software=software_info(), started_at=datetime.now().astimezone().isoformat(),
                   files=sorted(f'{name}.mp4' for name in writers) + ['timestamps.csv', 'export.json'])
    source_frames = output_frames = repeated = skipped = 0
    first = previous = None
    try:
        with (partial / 'timestamps.csv').open('w', newline='', encoding='utf-8') as handle:
            table = csv.writer(handle)
            table.writerow(['output_frame', 'mp4_time_s', 'source_frame', 'camera_timestamp_ns', 'camera_timestamp_us', 'repeated'])
            last_report = 0
            for position, stamp, images in source.frames():
                if max_frames and source_frames >= max_frames:
                    break
                source_frames += 1
                if first is None:
                    first = stamp
                if previous is not None and stamp <= previous[1]:  # duplicate or out-of-order timestamp
                    skipped += 1
                    continue
                target = output_frames + (slots(stamp - previous[1], period) - 1 if previous else 0)
                while output_frames < target:  # camera gap: repeat the last image
                    for name, writer in writers.items():
                        timings.call('pipe_' + name, writer.write, previous[2][name])
                    table.writerow([output_frames, f'{output_frames / fps:.6f}', previous[0], previous[1], previous[1] // 1000, 1])
                    output_frames += 1
                    repeated += 1
                for name, writer in writers.items():
                    timings.call('pipe_' + name, writer.write, images[name])
                table.writerow([output_frames, f'{output_frames / fps:.6f}', position, stamp, stamp // 1000, 0])
                output_frames += 1
                previous = (position, stamp, images)
                now = time.monotonic()
                if now - last_report >= 0.5:
                    emit('progress', done=source_frames, total=source.total, elapsed_s=round(now - started, 1))
                    last_report = now
        closing = time.monotonic()
        for writer in writers.values():
            writer.close()
        if not source_frames:
            raise RuntimeError('SVO2 中没有可解码的帧')
        summary.update(status='completed', source_frames=source_frames, sdk_reported_frames=source.total,
                       output_frames=output_frames, repeated_frames=repeated, skipped_frames=skipped,
                       mp4_duration_s=output_frames / fps,
                       capture_span_s=((previous[1] - first) / 1e9 + period / 1e9) if previous else 0,
                       elapsed_s=round(time.monotonic() - started, 2), ended_at=datetime.now().astimezone().isoformat(),
                       performance=dict(open_s=round(opened - started, 2), frames_s=round(closing - opened, 2),
                                        encoder_finish_s=round(time.monotonic() - closing, 2),
                                        frames_per_second=round(source_frames / max(closing - opened, 1e-6), 2),
                                        stages_ms=timings.summary(),
                                        process_boost=PROCESS_BOOST,
                                        note='stage times in ms; pipe_* is time waiting for a free slot in an encoder feed queue'),
                       partial_export=bool(max_frames))
        (partial / 'export.json').write_text(json.dumps(clean_json(summary), ensure_ascii=False, indent=2), encoding='utf-8')
        partial.rename(output)
        summary['folder'] = str(output)
        return summary
    except BaseException:
        for writer in writers.values():
            if writer.process.poll() is None:
                writer.process.kill()
            writer.log.close()
        raise
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('svo', type=Path)
    parser.add_argument('--depth', choices=DEPTH_MODES, help='also export depth.mp4 with this ZED depth mode (default: no depth)')
    parser.add_argument('--output', type=Path, help='output folder (default: export or export_<DEPTH> next to the SVO2)')
    parser.add_argument('--simulate', action='store_true')
    parser.add_argument('--max-frames', type=int, help='stop after this many source frames (speed tests)')
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'), help='ffmpeg.exe (default: from PATH)')
    args = parser.parse_args()
    if not args.ffmpeg:
        emit('finished', status='failed', error='找不到 FFmpeg：请把 ffmpeg 的 bin 文件夹加入 PATH，或用 --ffmpeg 指定 ffmpeg.exe')
        sys.stderr.flush()
        os._exit(1)
    output = args.output or args.svo.parent / ('export' + (f'_{args.depth}' if args.depth else ''))
    try:
        emit('finished', **export(args.svo.resolve(), output.resolve(), args.depth, args.simulate, args.ffmpeg, args.max_frames))
        code = 0
    except Exception as exc:
        traceback.print_exc()
        emit('finished', status='failed', error=str(exc), source=str(args.svo))
        code = 1
    sys.stderr.flush()
    os._exit(code)


if __name__ == '__main__':
    main()
