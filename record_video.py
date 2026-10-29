"""Record MindVision processed video at 30 or 60 FPS without changing playback speed.

Uses the local mvsdk package and FFmpeg; no numpy/OpenCV required. Close other
camera applications first. The camera may acquire faster than the output rate.
Frame timestamps select output frames; missing time slots repeat the last image
and are explicitly counted and logged. This is not a hardware frame-rate setter.
"""
import argparse
import csv
import ctypes
import json
import math
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time


class Timeline:
    """Map 0.1 ms camera ticks to CFR bins; tolerate 32-bit timestamp wrap."""
    def __init__(self, fps, limit):
        self.fps, self.limit = fps, limit
        self.previous = None
        self.elapsed = 0
        self.next_index = 0
        self.duplicates = 0
        self.last = None

    def add(self, tick, payload):
        if self.previous is not None:
            delta = (tick - self.previous) & 0xffffffff
            if delta == 0 or delta > 20000:
                raise RuntimeError('Invalid/stalled camera timestamp; recording stopped')
            self.elapsed += delta
        self.previous = tick
        # Use integer arithmetic to avoid floating point boundary drift.
        slot = self.elapsed * self.fps // 10000
        if slot < self.next_index:
            return []
        emitted = []
        while self.next_index < min(slot, self.limit):
            if self.last is None:
                raise RuntimeError('No frame available for timestamp gap')
            emitted.append((self.next_index, self.last, True))
            self.next_index += 1
            self.duplicates += 1
        if self.next_index < self.limit:
            emitted.append((self.next_index, payload, False))
            self.next_index += 1
            self.last = payload
        return emitted


class VideoSink:
    """Bounded writer queue. Queue overload stops capture instead of hiding loss."""
    def __init__(self, ffmpeg, output, width, height, fps):
        self.log = output.with_suffix('.ffmpeg.log').open('xb')
        self.process = subprocess.Popen(
            [ffmpeg, '-hide_banner', '-loglevel', 'warning', '-n', '-f', 'rawvideo',
             '-pixel_format', 'bgr24', '-video_size', f'{width}x{height}',
             '-framerate', str(fps), '-i', 'pipe:0', '-an', '-c:v', 'libx264',
             '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
             '-movflags', '+faststart', str(output)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log)
        self.queue = queue.Queue(maxsize=16)
        self.error = None
        self.written = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while True:
                data = self.queue.get()
                if data is None:
                    break
                self.process.stdin.write(data)
                self.written += 1
            self.process.stdin.close()
            if self.process.wait(timeout=30) != 0:
                raise RuntimeError('FFmpeg failed; inspect the .ffmpeg.log file')
        except Exception as exc:
            self.error = exc

    def put(self, data):
        if self.error:
            raise RuntimeError(f'Encoder failed: {self.error}')
        try:
            self.queue.put(data, timeout=0.5)
        except queue.Full:
            raise RuntimeError('Encoding/writing cannot keep up; queue is full')

    def finish(self):
        try:
            deadline = time.monotonic() + 10
            while self.thread.is_alive() and not self.error:
                try:
                    self.queue.put(None, timeout=0.2)
                    break
                except queue.Full:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Encoder queue did not drain')
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError('Encoder did not finish')
            if self.error:
                raise RuntimeError(str(self.error))
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self.thread.join(timeout=5)
            self.log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fps', type=int, choices=(30, 60), default=30)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path, help='SDK .config exported after tuning color')
    parser.add_argument('--serial', default='046011323028')
    parser.add_argument('--exposure-ms', type=float,
                        help='Default: 20 ms at 30 FPS, 10 ms at 60 FPS')
    parser.add_argument('--gain', type=float, help='Analog gain multiplier; default preserves loaded gain')
    parser.add_argument('--rotate180', action='store_true', help='Override SDK rotation with 180 degrees')
    args = parser.parse_args()
    exposure = args.exposure_ms if args.exposure_ms is not None else (20 if args.fps == 30 else 10)
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 86400:
        parser.error('seconds must be positive and no greater than one day')
    if not math.isfinite(exposure) or not 0 < exposure < 1000 / args.fps:
        parser.error('Exposure must be positive and shorter than one output frame')
    if args.gain is not None and (not math.isfinite(args.gain) or args.gain <= 0):
        parser.error('Gain must be positive')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        parser.error('ffmpeg was not found on PATH')
    if args.config and not args.config.is_file():
        parser.error('SDK configuration file not found')
    output = args.output.resolve()
    if output.suffix.lower() != '.mp4':
        parser.error('Output must have an .mp4 extension')
    suffixes = ['.mp4', '.json', '.frames.csv', '.ffmpeg.log', '.before.config', '.used.config']
    if any(output.with_suffix(s).exists() for s in suffixes):
        parser.error('Output or its sidecar files already exist; choose a new filename')
    output.parent.mkdir(parents=True, exist_ok=True)

    import mvsdk as sdk

    def check(code):
        if code != sdk.CAMERA_STATUS_SUCCESS:
            raise sdk.CameraException(code)

    def get(fn, *values):
        result = fn(*values)
        check(sdk.GetLastError())
        return result

    check(sdk.CameraSdkInit(1))
    devices = get(sdk.CameraEnumerateDevice)
    matches = [d for d in devices if d.GetSn() == args.serial]
    if not matches:
        raise RuntimeError('Target camera not found; verify --serial and driver')
    if get(sdk.CameraIsOpened, matches[0]):
        raise RuntimeError('Camera is in use. Close the other camera application first.')

    h, buffer, sink, ledger = None, None, None, None
    backup_saved = False
    timeline = Timeline(args.fps, math.ceil(args.seconds * args.fps))
    summary = dict(target_fps=args.fps, requested_seconds=args.seconds,
                   timing='frame.uiTimeStamp, 100 microseconds per tick; first frame per CFR bin',
                   color='Native CameraImageProcess using loaded SDK configuration',
                   config=str(args.config.resolve()) if args.config else 'SDK previously saved configuration',
                   errors=[], received_frames=0, timeouts=0)
    fatal = None
    started = time.monotonic()
    try:
        h = sdk.CameraInit(matches[0], -1, -1)
        check(sdk.CameraSaveParameterToFile(h, str(output.with_suffix('.before.config'))))
        backup_saved = True
        if args.config:
            check(sdk.CameraReadParameterFromFile(h, str(args.config.resolve())))
        cap = get(sdk.CameraGetCapability, h)
        check(sdk.CameraSetTriggerMode(h, 0))
        check(sdk.CameraSetAeState(h, 0))
        check(sdk.CameraSetExposureTime(h, exposure * 1000))
        speeds = [cap.pFrameSpeedDesc[i].iIndex for i in range(cap.iFrameSpeedDesc)]
        if 1 not in speeds:
            raise RuntimeError('Expected Normal speed mode 1 is not available')
        check(sdk.CameraSetFrameSpeed(h, 1))
        if args.gain is not None:
            step = cap.sExposeDesc.fAnalogGainStep
            if step <= 0:
                raise RuntimeError('Invalid gain step reported by camera')
            value = round(args.gain / step)
            if not cap.sExposeDesc.uiAnalogGainMin <= value <= cap.sExposeDesc.uiAnalogGainMax:
                raise ValueError('Gain is outside the device range')
            check(sdk.CameraSetAnalogGain(h, value))
        if args.rotate180:
            check(sdk.CameraSetRotate(h, 2))
        check(sdk.CameraSetIspOutFormat(h, sdk.CAMERA_MEDIA_TYPE_BGR8))
        actual_exposure = get(sdk.CameraGetExposureTime, h)
        if actual_exposure >= 1_000_000 / args.fps:
            raise RuntimeError('Actual camera exposure is too long for the requested FPS')
        summary.update(model=matches[0].GetProductName(), serial=matches[0].GetSn(),
                       exposure_us=actual_exposure,
                       gain_multiplier=get(sdk.CameraGetAnalogGain, h) * cap.sExposeDesc.fAnalogGainStep)
        # This recorder intentionally uses the current configured resolution.
        resolution = get(sdk.CameraGetImageResolution, h)
        if any(getattr(resolution, k) > 0 for k in ('iWidthZoomSw', 'iHeightZoomSw')):
            raise RuntimeError('Disable software zoom in the SDK configuration before recording')
        capacity = cap.sResolutionRange.iWidthMax * cap.sResolutionRange.iHeightMax * 3
        buffer = sdk.CameraAlignMalloc(capacity, 16)
        if not buffer:
            raise MemoryError('Could not allocate ISP output buffer')
        check(sdk.CameraSaveParameterToFile(h, str(output.with_suffix('.used.config'))))
        ledger = output.with_suffix('.frames.csv').open('x', newline='', encoding='utf-8')
        csv_out = csv.writer(ledger)
        csv_out.writerow(['output_frame', 'output_seconds', 'source_camera_tick_100us', 'duplicate'])
        check(sdk.CameraPlay(h))
        # Discard startup frames, retaining continuous ISP processing.
        warmup_until = time.monotonic() + 0.5
        last_frame_at = time.monotonic()
        started = last_frame_at
        next_report = started + 1
        shape = None
        print(f'Recording {args.fps} FPS, exposure {actual_exposure/1000:.3f} ms; Ctrl+C to stop.', flush=True)
        while timeline.next_index < timeline.limit:
            if time.monotonic() - started > args.seconds + 10:
                raise RuntimeError('Recording exceeded its wall-clock deadline')
            try:
                raw, head = sdk.CameraGetImageBuffer(h, 500)
            except sdk.CameraException as exc:
                if exc.error_code != sdk.CAMERA_STATUS_TIME_OUT:
                    raise
                summary['timeouts'] += 1
                if time.monotonic() - last_frame_at > 2:
                    raise RuntimeError('No camera frames for two seconds')
                continue
            try:
                tick = head.uiTimeStamp
                last_frame_at = time.monotonic()
                check(sdk.CameraImageProcess(h, raw, buffer, head))
                if time.monotonic() < warmup_until:
                    continue
                current_shape = (head.iWidth, head.iHeight)
                if min(current_shape) <= 0 or head.iWidth * head.iHeight * 3 > capacity:
                    raise RuntimeError('Invalid image dimensions')
                if shape is None:
                    shape = current_shape
                    if any(n % 2 for n in shape):
                        raise RuntimeError('H.264 yuv420 requires even image width/height')
                    summary.update(width=shape[0], height=shape[1])
                    sink = VideoSink(ffmpeg, output, *shape, args.fps)
                elif current_shape != shape:
                    raise RuntimeError('Resolution changed during recording')
                summary['received_frames'] += 1
                data = ctypes.string_at(buffer, head.iWidth * head.iHeight * 3)
            finally:
                check(sdk.CameraReleaseImageBuffer(h, raw))
            for index, (pixels, source_tick), duplicate in timeline.add(tick, (data, tick)):
                sink.put(pixels)
                csv_out.writerow([index, f'{index/args.fps:.9f}', source_tick, int(duplicate)])
            if time.monotonic() >= next_report:
                print(f'Received={summary["received_frames"]}, video={timeline.next_index}, repeated={timeline.duplicates}', flush=True)
                next_report = time.monotonic() + 1
        stats = get(sdk.CameraGetFrameStatistic, h)
        summary['sdk_counters'] = dict(total=stats.iTotal, valid=stats.iCapture, lost=stats.iLost)
    except KeyboardInterrupt:
        summary['stopped_by_user'] = True
    except Exception as exc:
        fatal = exc
        summary['errors'].append(str(exc))
    finally:
        # Release camera before waiting for the encoder to finish.
        if h is not None:
            try:
                check(sdk.CameraPause(h))
                if backup_saved:
                    check(sdk.CameraReadParameterFromFile(h, str(output.with_suffix('.before.config'))))
                    summary['configuration_restored'] = True
            except Exception as exc:
                summary['errors'].append(f'Configuration restoration: {exc}')
            finally:
                try:
                    check(sdk.CameraUnInit(h))
                except Exception as exc:
                    summary['errors'].append(f'Camera close: {exc}')
        if buffer:
            sdk.CameraAlignFree(buffer)
        if sink:
            try:
                sink.finish()
            except Exception as exc:
                summary['errors'].append(f'Encoder: {exc}')
        if ledger:
            ledger.close()
        summary.update(written_frames=sink.written if sink else 0,
                       repeated_frames=timeline.duplicates,
                       video_seconds=(sink.written if sink else 0)/args.fps,
                       measured_source_fps=((summary['received_frames']-1)*10000/timeline.elapsed
                                            if timeline.elapsed else None))
        summary['complete'] = not summary['errors'] and summary['written_frames'] == timeline.limit
        summary['all_output_frames_unique'] = timeline.duplicates == 0
        output.with_suffix('.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if fatal or summary['errors']:
        raise SystemExit(1)
    if timeline.duplicates:
        print('WARNING: repeated frames were necessary; see .frames.csv. This is not fully unique-frame capture.')


if __name__ == '__main__':
    main()
