"""One ZED camera per process. Commands: JSON lines on stdin. Events and previews: framed binary on stdout.

Run by zed_support.ZedWorker with .venv-zed. `--list` prints the device list as one JSON line.
`--simulate` needs only Pillow and is used by offline tests and the demo mode.
"""
import os
import sys

# The ZED SDK prints log lines to stdout; keep the protocol on a private handle before importing it.
from subprocess_worker import OpenLock, boost_process, protect_stdout  # noqa: E402
PROTO = protect_stdout()
PROCESS_BOOST = boost_process()

import argparse  # noqa: E402
from datetime import datetime  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
from pathlib import Path  # noqa: E402
import queue  # noqa: E402
import shutil  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402

from zed_support import (MODES, ZedAudit, ZedSettings, ZED_METADATA_UNITS, ZED_SDK, pack_preview, safe_name,  # noqa: E402
                         write_message, write_zed_report)
from camera_controls import control, validate_changes  # noqa: E402
from encoding_support import FrameJournal  # noqa: E402
from research_data import clean_json, software_info, utc_now, write_json  # noqa: E402

SIZES = {resolution: size for resolution, _, size in MODES.values()}
SETTING_NAMES = dict(brightness='BRIGHTNESS', contrast='CONTRAST', hue='HUE', saturation='SATURATION',
                     sharpness='SHARPNESS', gamma='GAMMA', gain='GAIN', exposure='EXPOSURE', aec_agc='AEC_AGC',
                     wb_auto='WHITEBALANCE_AUTO', wb_temperature='WHITEBALANCE_TEMPERATURE', led='LED_STATUS')
# ZED 2 / 2i documented ranges (SDK units).
RANGES = dict(brightness=(0, 8), contrast=(0, 8), hue=(0, 11), saturation=(0, 8), sharpness=(0, 8), gamma=(1, 9),
              gain=(0, 100), exposure=(0, 100), wb_temperature=(2800, 6500))
LABELS = dict(aec_agc=('自动曝光 / 增益', '曝光与方向', 'bool'), exposure=('曝光（帧周期 %）', '曝光与方向', 'int'),
              gain=('增益（SDK 0–100）', '曝光与方向', 'int'), wb_auto=('自动白平衡', '色彩与亮度', 'bool'),
              wb_temperature=('白平衡色温（K，100 步进）', '色彩与亮度', 'int'), brightness=('亮度', '色彩与亮度', 'int'),
              contrast=('对比度', '色彩与亮度', 'int'), hue=('色调', '色彩与亮度', 'int'), saturation=('饱和度', '色彩与亮度', 'int'),
              gamma=('Gamma', '色彩与亮度', 'int'), sharpness=('锐度', '处理与速度', 'int'), led=('前面板 LED', '处理与速度', 'bool'))


def plain(value, depth=0):
    """JSON view of pyzed structures; enums and unknown objects become strings."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, 'tolist'):
        return plain(value.tolist(), depth)
    if isinstance(value, (list, tuple)):
        return [plain(v, depth+1) for v in value]
    if isinstance(value, dict):
        return {str(k): plain(v, depth+1) for k, v in value.items()}
    if depth >= 5 or type(value).__module__ != 'pyzed.sl' or (hasattr(value, 'name') and hasattr(value, 'value')):
        return str(value)
    result = {}
    for name in dir(value):
        if name.startswith('_'):
            continue
        try:
            item = getattr(value, name)
        except Exception as exc:
            result[name] = f'<error {exc}>'
            continue
        if not callable(item):
            result[name] = plain(item, depth+1)
    return result


# ---------------------------------------------------------------- backends

class LiveCamera:
    simulated = False

    def __init__(self, args):
        os.add_dll_directory(str(ZED_SDK / 'bin'))
        import pyzed.sl as sl
        self.sl = sl
        self.camera = sl.Camera()
        init = sl.InitParameters()
        init.set_from_serial_number(int(args.serial))
        init.camera_resolution = getattr(sl.RESOLUTION, args.resolution)
        init.camera_fps = args.fps
        init.depth_mode = sl.DEPTH_MODE.NONE
        # Standard grab path; async recovery stalled with several cameras in the reference project.
        init.async_grab_camera_recovery = False
        init.open_timeout_sec = 5
        init.camera_image_flip = getattr(sl.FLIP_MODE, args.flip)
        self.init_parameters = plain(init)
        # One ZED at a time: open it and wait for its first frame before the next process may open.
        with OpenLock() as lock:
            self.open_wait_s = round(lock.waited_s, 2)
            code = self.camera.open(init)
            if code > sl.ERROR_CODE.SUCCESS:
                raise RuntimeError(f'ZED 打开失败：{code}。请检查 USB 连接，并关闭 ZED Explorer 等占用相机的程序')
            deadline = time.monotonic() + 5
            while self.camera.grab() != sl.ERROR_CODE.SUCCESS:
                if time.monotonic() > deadline:
                    self.camera.close()
                    raise RuntimeError('ZED 已打开但 5 秒内没有图像，请检查 USB 连接或带宽')
                time.sleep(0.01)
        info = self.camera.get_camera_information()
        config = info.camera_configuration
        self.size = (config.resolution.width, config.resolution.height)
        if self.size != SIZES[args.resolution] or abs(config.fps - args.fps) > 0.01:
            self.camera.close()
            raise RuntimeError(f'ZED 实际模式 {self.size[0]}x{self.size[1]}@{config.fps} 与请求不符，拒绝静默降级')
        self.serial = str(info.serial_number)
        self.model = str(info.camera_model)
        self.firmware = dict(camera=config.firmware_version, sensors=info.sensors_configuration.firmware_version)
        self.information = plain(info)
        self.sdk_version = sl.Camera.get_sdk_version()
        self.timestamp_clock = str(sl.Camera.get_timestamp_clock())
        self.runtime = sl.RuntimeParameters()
        self.mat = sl.Mat()
        self.small = sl.Mat()

    def grab(self):
        code = self.camera.grab(self.runtime)
        return code == self.sl.ERROR_CODE.SUCCESS, str(code)

    def timestamp_ns(self):
        return self.camera.get_timestamp(self.sl.TIME_REFERENCE.IMAGE).get_nanoseconds()

    def dropped_total(self):
        return self.camera.get_frame_dropped_count()

    def preview(self, full):
        width, height = self.size if full else (640, round(640 * self.size[1] / self.size[0]))
        code = self.camera.retrieve_image(self.mat, self.sl.VIEW.LEFT, self.sl.MEM.CPU, self.sl.Resolution(width, height))
        if code != self.sl.ERROR_CODE.SUCCESS:
            raise RuntimeError(f'预览取图失败：{code}')
        return width, height, self.mat.get_data().tobytes(), 'BGRX'

    def color_ratios(self):
        """(R/G, B/G, usable fraction) of a small left image, ignoring near-black and clipped pixels."""
        import numpy as np
        code = self.camera.retrieve_image(self.small, self.sl.VIEW.LEFT_BGR, self.sl.MEM.CPU, self.sl.Resolution(160, 90))
        if code != self.sl.ERROR_CODE.SUCCESS:
            return None
        pixels = self.small.get_data().reshape(-1, 3).astype(np.float32)
        brightest = pixels.max(axis=1)
        keep = (brightest > 20) & (brightest < 245)
        if keep.mean() < 0.2:
            return None
        b, g, r = pixels[keep].mean(axis=0)
        return (float(r / g), float(b / g), float(keep.mean())) if g > 0 else None

    def get(self, key):
        code, value = self.camera.get_camera_settings(getattr(self.sl.VIDEO_SETTINGS, SETTING_NAMES[key]))
        if code != self.sl.ERROR_CODE.SUCCESS:
            raise RuntimeError(str(code))
        return value

    def set(self, key, value):
        code = self.camera.set_camera_settings(getattr(self.sl.VIDEO_SETTINGS, SETTING_NAMES[key]), int(value))
        if code is not None and code != self.sl.ERROR_CODE.SUCCESS:
            raise RuntimeError(f'设置 {key} 失败：{code}')

    def enable_recording(self, path, compression):
        params = self.sl.RecordingParameters(str(path), getattr(self.sl.SVO_COMPRESSION_MODE, compression))
        code = self.camera.enable_recording(params)
        if code != self.sl.ERROR_CODE.SUCCESS:
            raise RuntimeError(f'无法开始 SVO2 录制：{code}')

    def pause_recording(self, paused):
        self.camera.pause_recording(paused)

    def recording_status(self):
        status = self.camera.get_recording_status()
        return dict(status=status.status, is_paused=status.is_paused, frames_ingested=status.number_frames_ingested,
                    frames_encoded=status.number_frames_encoded, average_compression_ms=status.average_compression_time,
                    average_compression_ratio=status.average_compression_ratio)

    def disable_recording(self):
        self.camera.disable_recording()

    def calibration_files(self):
        path = Path(os.environ.get('PROGRAMDATA', r'C:\ProgramData')) / 'Stereolabs' / 'settings' / f'SN{self.serial}.conf'
        return [path] if path.is_file() else []

    def verify_svo(self, path):
        """Light check that scales to long files: frame count plus first/last timestamps."""
        sl = self.sl
        camera = sl.Camera()
        init = sl.InitParameters()
        init.set_from_svo_file(str(path))
        init.svo_real_time_mode = False
        init.depth_mode = sl.DEPTH_MODE.NONE
        code = camera.open(init)
        if code != sl.ERROR_CODE.SUCCESS:
            return dict(error=f'无法打开：{code}')
        try:
            frames = camera.get_svo_number_of_frames()
            result = dict(frames=frames, first_us=None, last_us=None, decodable_frames=None)
            if frames <= 0:
                result['error'] = 'SVO2 中没有图像帧'
                return result
            if camera.grab() == sl.ERROR_CODE.SUCCESS:
                result['first_us'] = camera.get_timestamp(sl.TIME_REFERENCE.IMAGE).get_nanoseconds() // 1000
            for position in (frames - 1, frames - 2):
                if position < 0:
                    break
                camera.set_svo_position(position)
                if camera.grab() == sl.ERROR_CODE.SUCCESS:
                    result['last_us'] = camera.get_timestamp(sl.TIME_REFERENCE.IMAGE).get_nanoseconds() // 1000
                    result['decodable_frames'] = position + 1
                    if position != frames - 1:
                        result['note'] = 'SDK 报告的最后一帧无法解码（SDK 5.5 文件末尾索引项）'
                        result['frames'] = position + 1
                    break
            return result
        finally:
            camera.close()

    def close(self):
        self.camera.close()


class SimCamera:
    """Deterministic stand-in with injectable camera drops, duplicates, stalls and counter resets."""
    simulated = True

    def __init__(self, args):
        if args.sim_open_delay:
            time.sleep(args.sim_open_delay)  # like Camera.open() stuck in the SDK; no commands are read meanwhile
        self.args = args
        self.size = SIZES[args.resolution]
        self.serial = str(args.serial)
        self.model = 'ZED 2i（模拟）'
        self.firmware = dict(camera='SIM', sensors='SIM')
        self.information = dict(simulated=True, serial_number=self.serial)
        self.init_parameters = dict(simulated=True, resolution=args.resolution, fps=args.fps, flip=args.flip)
        self.sdk_version = 'simulated'
        self.timestamp_clock = 'SIMULATED_SYSTEM_CLOCK'
        self.period_ns = round(1e9 / (args.sim_fps or args.fps * 0.9952))
        self.origin_mono = time.monotonic_ns()
        self.origin_unix = time.time_ns() // 1000 * 1000 + 123  # sub-microsecond remainder like the live SDK
        self.next_index = 0
        self.index = None
        self.grabs = 0
        self.dropped = 0
        self.values = dict(brightness=4, contrast=4, hue=0, saturation=4, sharpness=4, gamma=5, gain=50, exposure=50,
                           aec_agc=1, wb_auto=1, wb_temperature=4700, led=1)
        self.recording = None
        self.paused = False

    def grab(self):
        self.grabs += 1
        a = self.args
        if a.sim_freeze_after and time.monotonic_ns() - self.origin_mono > a.sim_freeze_after * 1e9:
            time.sleep(3600)  # like grab() stuck in SDK connection recovery
        if a.sim_stall_every and self.grabs % a.sim_stall_every == 0:
            time.sleep(self.period_ns * 2.6 / 1e9)  # SDK-side stall: frames are lost inside grab
        if a.sim_dup_every and self.index is not None and self.grabs % a.sim_dup_every == 0:
            return True, 'SUCCESS'
        target = self.origin_mono + self.next_index * self.period_ns
        now = time.monotonic_ns()
        if now < target:
            time.sleep((target - now) / 1e9)
        else:
            behind = (now - target) // self.period_ns
            self.next_index += behind
            self.dropped += behind
        if a.sim_drop_every and self.next_index and self.next_index % a.sim_drop_every == 0:
            self.next_index += 1
            self.dropped += 1
        self.index = self.next_index
        self.next_index += 1
        if a.sim_reset_every and self.grabs % a.sim_reset_every == 0:
            self.dropped = 0
        if self.recording is not None and not self.paused:
            self.recording.write(f'{self.timestamp_ns() // 1000}\n')
        return True, 'SUCCESS'

    def timestamp_ns(self):
        return self.origin_unix + self.index * self.period_ns

    def dropped_total(self):
        return self.dropped

    def preview(self, full):
        from PIL import Image, ImageDraw
        width, height = self.size if full else (640, round(640 * self.size[1] / self.size[0]))
        image = Image.new('RGB', (width, height), (24, 30, 40))
        draw = ImageDraw.Draw(image)
        x = (self.index or 0) * 7 % max(1, width - 60)
        draw.rectangle((x, height // 2 - 30, x + 60, height // 2 + 30), fill=(220, 160, 60))
        draw.text((20, 20), f'SIMULATED ZED {self.serial}  frame {self.index}', fill='white')
        return width, height, image.tobytes(), 'RGB'

    def color_ratios(self):
        """Scene lit at --sim-light K. Auto WB renders it neutral; a manual setting T tints it warm (T above the
        light) or cool (T below), as a real camera does. Like the ZED, the reported temperature does not follow
        auto white balance."""
        import math
        if self.args.sim_dark:
            return None
        cast = 0 if self.values['wb_auto'] else 0.25 * (self.values['wb_temperature'] - self.args.sim_light) / 1000
        return math.exp(cast), math.exp(-cast), 1.0

    def get(self, key):
        if key == 'led' and self.args.sim_led_fail:
            raise RuntimeError('FAILURE')
        return self.values[key]

    def set(self, key, value):
        value = int(value)
        if key in RANGES and not RANGES[key][0] <= value <= RANGES[key][1]:
            raise RuntimeError(f'设置 {key} 失败：超出范围')
        if key in ('exposure', 'gain'):
            self.values['aec_agc'] = 0
        if key == 'wb_temperature':
            self.values['wb_auto'] = 0
            value = round(value / 100) * 100
        if key == 'aec_agc' and value:
            self.values['exposure'] = 60
        self.values[key] = value

    def enable_recording(self, path, compression):
        self.recording = Path(path).open('x', encoding='utf-8')
        self.recording.write(f'SIMULATED-SVO2 {compression}\n')

    def pause_recording(self, paused):
        self.paused = paused

    def recording_status(self):
        return dict(status=self.recording is not None, simulated=True)

    def disable_recording(self):
        if self.recording:
            self.recording.close()
            self.recording = None

    def calibration_files(self):
        return []

    def verify_svo(self, path):
        lines = Path(path).read_text(encoding='utf-8').splitlines()[1:]
        stamps = [int(line) for line in lines]
        if self.args.sim_lose_last and stamps:
            stamps = stamps[:-1]
        if not stamps:
            return dict(frames=0, error='SVO2 中没有图像帧')
        return dict(frames=len(set(stamps)), first_us=stamps[0], last_us=stamps[-1], decodable_frames=len(set(stamps)))

    def close(self):
        self.disable_recording()


# ---------------------------------------------------------------- worker

class PreviewSender(threading.Thread):
    """Latest-only JPEG encoding so preview never delays grab()."""

    def __init__(self, lock):
        super().__init__(daemon=True, name='zed-preview')
        self.lock = lock
        self.condition = threading.Condition()
        self.pending = None
        self.closed = False

    def submit(self, item):
        with self.condition:
            self.pending = item
            self.condition.notify()

    def run(self):
        from PIL import Image
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                (width, height, pixels, rawmode), full = self.pending
                self.pending = None
            image = Image.frombuffer('RGB', (width, height), pixels, 'raw', rawmode, 0, 1)
            buffer = io.BytesIO()
            image.save(buffer, 'JPEG', quality=90 if full else 80)
            write_message(PROTO, b'P', pack_preview(dict(width=width, height=height, full=full), buffer.getvalue()), self.lock)

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify()


class Worker:
    def __init__(self, args, camera):
        self.args, self.camera = args, camera
        self.lock = threading.Lock()
        self.commands = queue.Queue()
        self.running = True
        self.session = None
        self.active = False
        self.full_preview = False
        self.last_wb_calibration = None
        self.original = self.read_values()

    # -- messaging
    def emit(self, event, **data):
        payload = json.dumps(clean_json(dict(event=event, **data)), ensure_ascii=False, default=str).encode('utf-8')
        write_message(PROTO, b'J', payload, self.lock)

    def stdin_reader(self):
        for line in sys.stdin.buffer:
            if line.strip():
                try:
                    self.commands.put(json.loads(line))
                except ValueError:
                    pass
        self.commands.put(dict(command='close'))

    # -- settings
    def read_values(self):
        values, errors = {}, {}
        for key in SETTING_NAMES:
            try:
                values[key] = self.camera.get(key)
            except Exception as exc:
                errors[key] = str(exc)
        return dict(values=values, unavailable=errors)

    def settings(self):
        if self.camera.get('aec_agc'):
            return ZedSettings()
        return ZedSettings(self.camera.get('exposure'), self.camera.get('gain'))

    def apply_settings(self, settings):
        settings.validate()
        if settings.automatic:
            self.camera.set('aec_agc', 1)
        else:
            self.camera.set('aec_agc', 0)
            self.camera.set('exposure', settings.exposure)
            self.camera.set('gain', settings.gain)
        return self.settings()

    def controls(self):
        fields = {}
        for key, (label, group, kind) in LABELS.items():
            try:
                value = self.camera.get(key)
                value = bool(value) if kind == 'bool' else int(value)
                low, high = RANGES.get(key, (None, None))
                fields[key] = control(label, group, value, kind, low, high)
            except Exception as exc:
                fields[key] = control(label, group, kind=kind, error=str(exc))
        fields['flip'] = control('画面翻转（SDK）', '曝光与方向', kind='int',
                                 error=f'当前 {self.args.flip}；断开后在相机卡片上修改，重新连接生效')
        return fields

    def apply_controls(self, changes):
        merged = validate_changes(self.controls(), changes)
        backup = self.read_values()['values']
        try:
            for key in ('brightness', 'contrast', 'hue', 'saturation', 'sharpness', 'gamma', 'led'):
                if key in changes:
                    self.camera.set(key, int(changes[key]))
            if 'wb_auto' in changes or 'wb_temperature' in changes:
                if merged['wb_auto'] and 'wb_temperature' not in changes:
                    self.camera.set('wb_auto', 1)
                else:
                    self.camera.set('wb_auto', 0)
                    self.camera.set('wb_temperature', round(int(merged['wb_temperature']) / 100) * 100)
            if {'aec_agc', 'exposure', 'gain'} & changes.keys():
                if merged['aec_agc'] and not {'exposure', 'gain'} & changes.keys():
                    self.camera.set('aec_agc', 1)
                else:
                    self.camera.set('aec_agc', 0)
                    self.camera.set('exposure', int(merged['exposure']))
                    self.camera.set('gain', int(merged['gain']))
        except Exception as exc:
            try:
                self.restore(backup)
            except Exception as rollback:
                raise RuntimeError(f'应用失败：{exc}；回滚也失败：{rollback}') from exc
            raise
        return self.controls()

    def restore(self, values):
        for key in ('brightness', 'contrast', 'hue', 'saturation', 'sharpness', 'gamma', 'led'):
            if key in values:
                self.camera.set(key, values[key])
        if 'wb_auto' in values:
            if values['wb_auto']:
                self.camera.set('wb_auto', 1)
            elif 'wb_temperature' in values:
                self.camera.set('wb_auto', 0)
                self.camera.set('wb_temperature', values['wb_temperature'])
        if 'aec_agc' in values:
            if values['aec_agc']:
                self.camera.set('aec_agc', 1)
            elif 'exposure' in values and 'gain' in values:
                self.camera.set('aec_agc', 0)
                self.camera.set('exposure', values['exposure'])
                self.camera.set('gain', values['gain'])

    def measure_color(self, frames=4, samples=1):
        """Average colour ratios after letting `frames` new frames through; None if the image cannot be measured."""
        readings = []
        for _ in range(samples):
            for _ in range(frames):
                self.camera.grab()
            reading = self.camera.color_ratios()
            if reading is None:
                return None
            readings.append(reading)
        return tuple(sum(r[i] for r in readings) / len(readings) for i in range(2))

    def calibrate_white_balance(self, settle_s=1.5):
        """Find the manual colour temperature whose image matches what auto white balance shows, and lock it.

        With auto white balance on, the ZED SDK keeps reporting the last manual value (4700 K by default), not
        the temperature auto white balance is using, so that reading cannot be locked. Instead: let auto white
        balance settle, measure the image colour, then search the manual range for the closest match.
        If the image cannot be measured, auto white balance stays on rather than locking a wrong value."""
        started = time.monotonic()
        camera = self.camera
        camera.set('wb_auto', 1)
        while time.monotonic() - started < settle_s:
            camera.grab()
        target = self.measure_color(frames=3, samples=3)
        if target is None:
            camera.set('wb_auto', 1)
            return dict(locked=False, method='match auto white balance image colour',
                        reason='画面过暗或过曝，无法测量颜色；保持自动白平衡（录制中颜色可能随场景变化）')
        tried = {}

        def distance(kelvin):
            if kelvin not in tried:
                camera.set('wb_temperature', kelvin)  # also turns auto white balance off
                tried[kelvin] = self.measure_color()
            measured = tried[kelvin]
            if measured is None:
                return float('inf')
            return (measured[0] - target[0]) ** 2 + (measured[1] - target[1]) ** 2

        low, high = RANGES['wb_temperature']
        best = min(range(low, high + 1, 900), key=distance)          # 2800, 3700, 4600, 5500, 6400
        for step in (300, 100):                                     # refine around the best so far
            best = min((k for k in range(best - 3 * step, best + 3 * step + 1, step) if low <= k <= high), key=distance)
        camera.set('wb_temperature', best)
        achieved = self.measure_color()
        residual = distance(best)
        return dict(locked=True, kelvin=best, method='match auto white balance image colour',
                    target_ratios=dict(r_over_g=round(target[0], 4), b_over_g=round(target[1], 4)),
                    achieved_ratios=dict(r_over_g=round(achieved[0], 4), b_over_g=round(achieved[1], 4)) if achieved else None,
                    residual=round(residual, 6), temperatures_tried=len(tried), seconds=round(time.monotonic() - started, 2),
                    note='ZED 在自动白平衡时报告的色温不随画面变化，因此按画面颜色匹配出手动色温')

    def lock_automatic(self):
        locked, calibration = {}, None
        if self.camera.get('aec_agc'):
            exposure, gain = self.camera.get('exposure'), self.camera.get('gain')
            self.camera.set('aec_agc', 0)
            self.camera.set('exposure', exposure)
            self.camera.set('gain', gain)
            locked.update(exposure=exposure, gain=gain)
        if self.camera.get('wb_auto'):
            calibration = self.calibrate_white_balance()
            if calibration['locked']:
                locked.update(wb_temperature=calibration['kelvin'])
            else:
                self.emit('notice', text=calibration['reason'])
        for _ in range(5):  # let the new manual settings reach the stream before recording
            self.camera.grab()
        return locked, calibration

    def snapshot(self):
        values = self.read_values()
        return dict(values=values['values'], unsupported_or_failed=values['unavailable'], flip=self.args.flip,
                    mode_label=self.args.mode_label, resolution=self.args.resolution, nominal_fps=self.args.fps,
                    note='ZED SDK units; null/missing means unavailable. Settings are read only before and after recording.')

    # -- white balance button: auto white balance, then lock the matching manual temperature
    def white_balance_button(self, request):
        self.emit('notice', text='ZED 正在测量白平衡（约 3–5 秒）；请让中性白 / 灰目标充满画面')
        result = self.calibrate_white_balance()
        self.last_wb_calibration = result
        if result['locked']:
            self.emit('notice', text=f'白平衡已按画面锁定为 {result["kelvin"]} K')
        else:
            self.emit('notice', text=result['reason'])
        if request:
            self.emit('advanced', controls=self.controls(), request=request, applied=False)

    # -- recording
    def prepare(self, command):
        if self.session:
            raise RuntimeError('相机正在录像')
        if self.args.simulate and self.args.sim_hang_prepare:
            time.sleep(3600)  # like a ZED stuck in SDK connection recovery: no further commands are processed
        folder = Path(command['folder'])
        name = safe_name(self.camera.serial)
        settings = ZedSettings.from_dict(command['settings'])
        applied = self.apply_settings(settings)
        locked, wb_calibration = self.lock_automatic()
        self.emit('settings', settings=self.settings().as_dict())
        snapshot = self.snapshot()
        calibration = []
        for source in self.camera.calibration_files():
            target = folder / f'{name}.calibration{source.suffix}'
            shutil.copyfile(source, target)
            calibration.append(dict(source=str(source), copy=target.name))
        svo = folder / f'{name}.svo2'
        if svo.exists():
            raise RuntimeError(f'{svo} 已存在，拒绝覆盖')
        self.camera.enable_recording(svo, command['compression'])
        try:
            self.camera.pause_recording(True)
            journal = FrameJournal(folder / f'{name}.received.jsonl')
        except Exception:
            self.camera.disable_recording()
            raise
        info = dict(serial=self.camera.serial, model=self.camera.model, kind='zed', simulated=self.camera.simulated,
                    schema='zed-recording/1', mode='svo2', batch_mode=command.get('mode'), mode_label=self.args.mode_label,
                    resolution=self.args.resolution, width=self.camera.size[0], height=self.camera.size[1],
                    nominal_fps=self.args.fps, target_fps=self.args.fps, compression=command['compression'], flip=self.args.flip,
                    settings=applied.as_dict(), locked_from_auto=locked, firmware=self.camera.firmware,
                    white_balance_calibration=wb_calibration or getattr(self, 'last_wb_calibration', None),
                    sdk_version=self.camera.sdk_version, timestamp_clock=self.camera.timestamp_clock,
                    init_parameters=self.camera.init_parameters, camera_information=self.camera.information,
                    calibration_files=calibration, video=str(svo), received_jsonl=journal.stream.name,
                    experiment=command.get('experiment', {}), software=software_info(), metadata_units=ZED_METADATA_UNITS,
                    snapshot_start=snapshot, errors=[], started_at=None,
                    timing='SVO2 holds each grabbed frame once; no frame repetition or resampling')
        self.session = dict(info=info, journal=journal, audit=ZedAudit(self.args.fps), svo=svo, folder=folder, name=name)
        self.emit('prepared')

    def start(self):
        if not self.session or self.active:
            return
        self.camera.pause_recording(False)
        self.active = True
        self.session['info']['started_at'] = datetime.now().astimezone().isoformat()
        self.emit('record_started')

    def stop(self, error=None):
        if not self.session:
            self.emit('record_finished', summary=None)
            return
        session, self.session = self.session, None
        was_active, self.active = self.active, False
        info, audit = session['info'], session['audit']
        if error:
            info['errors'].append(str(error))
        if not was_active:
            info['errors'].append('批次未开始录制即停止')
        padding_us = None
        if was_active and not error:
            # SDK 5.5 can drop the last frame of an SVO2 when recording stops. One extra, uncounted
            # frame is recorded so that every counted frame stays in the file; it is logged as padding.
            try:
                for _ in range(4):  # grab can return the previous frame again; the padding must be a new one
                    ok, _ = self.camera.grab()
                    stamp = self.camera.timestamp_ns() if ok else None
                    if stamp is not None and stamp // 1000 != audit.last_us:
                        padding_us = stamp // 1000
                        session['journal'].write(dict(receive_index=None, duplicate=False, stop_padding=True,
                                                      camera_timestamp_ns=stamp, camera_timestamp_us=padding_us,
                                                      host_receive_monotonic_ns=time.monotonic_ns(), host_receive_unix_ns=time.time_ns()))
                        break
            except Exception as exc:
                info['errors'].append(f'停止填充帧失败：{exc}')
        try:
            info['recording_status'] = self.camera.recording_status()
        except Exception as exc:
            info['recording_status'] = dict(error=str(exc))
        try:
            self.camera.disable_recording()
        except Exception as exc:
            info['errors'].append(f'SVO2 结束失败：{exc}')
        try:
            session['journal'].close()
        except Exception as exc:
            info['errors'].append(str(exc))
        expected_rows = audit.count + audit.duplicates + (padding_us is not None)
        if session['journal'].written != expected_rows:
            info['errors'].append(f'逐帧元数据保存不完整：{session["journal"].written}/{expected_rows}')
        try:
            info['snapshot_end'] = self.snapshot()
        except Exception as exc:
            info['snapshot_end'] = dict(unavailable=str(exc))
        svo_check = None
        if audit.count:
            try:
                svo_check = self.camera.verify_svo(session['svo'])
            except Exception as exc:
                svo_check = dict(error=str(exc))
        if not audit.count:
            info['errors'].append('没有录到任何帧')
        integrity = audit.report(svo_check, info['errors'], padding_us)
        median = integrity['interval_ns']['median']
        info.update(ended_at=datetime.now().astimezone().isoformat(), integrity=integrity, stop_padding_us=padding_us,
                    stop_padding_note='停止时多录 1 帧作填充（received.jsonl 中 stop_padding=true，不计入帧数），'
                                      '用于抵消 SDK 5.5 可能丢弃文件最后一帧；该帧可能在也可能不在 SVO2 中',
                    written_frames=audit.count, repeated_frames=0, camera_missing_frames=integrity['camera_missing_frames'],
                    source_frames_observed=audit.count + audit.duplicates, source_fps=integrity['measured_fps'],
                    video_seconds=((audit.last_ns - audit.first_ns) / 1e9 + (median or 0) / 1e9) if audit.count else 0,
                    all_frames_unique=audit.duplicates == 0,
                    complete=not info['errors'] and integrity['status'] != 'failed')
        try:
            write_zed_report(session['folder'] / f'{session["name"]}.integrity.txt', info)
            write_json(session['folder'] / f'{session["name"]}.json', info)
        except Exception as exc:
            info['errors'].append(f'结果文件保存失败：{exc}')
            info['complete'] = False
        self.emit('record_finished', summary=info)

    def save_preset(self, command):
        folder = Path(command['folder'])
        if 'settings' in command:
            self.apply_settings(ZedSettings.from_dict(command['settings']))
        write_json(folder / f'{safe_name(self.camera.serial)}.preset.json',
                   dict(schema='zed-preset/1', serial=self.camera.serial, simulated=self.camera.simulated,
                        values=self.read_values()['values'], mode_label=self.args.mode_label, flip=self.args.flip,
                        created_at=utc_now()))
        self.emit('notice', text=f'预设已保存：{folder}')

    def load_preset(self, command):
        description = json.loads((Path(command['folder']) / f'{safe_name(self.camera.serial)}.preset.json').read_text(encoding='utf-8'))
        if description.get('serial') != self.camera.serial or description.get('simulated') != self.camera.simulated:
            raise ValueError('预设的相机序列号或模拟 / 实机模式不匹配')
        self.restore(description['values'])
        note = '' if (description.get('mode_label'), description.get('flip')) == (self.args.mode_label, self.args.flip) else \
            f'；预设模式 {description.get("mode_label")} / 翻转 {description.get("flip")} 与当前连接不同，需断开后在卡片上修改'
        self.emit('notice', text='ZED 预设已加载' + note)

    # -- main loop
    def handle(self, command):
        name = command.get('command')
        request = command.get('request')
        try:
            if name == 'close':
                self.running = False
                self.stop()
            elif name == 'stop':
                self.stop()
            elif name == 'start':
                self.start()
            elif name == 'prepare':
                try:
                    self.prepare(command)
                except Exception as exc:
                    self.emit('prepare_failed', text=str(exc))
            elif name == 'full_preview':
                self.full_preview = bool(command.get('enabled'))
            elif self.session:
                raise RuntimeError('采集期间 ZED 参数已锁定，请先停止采集')
            elif name == 'apply':
                self.emit('settings', settings=self.apply_settings(ZedSettings.from_dict(command['settings'])).as_dict())
            elif name == 'wb':
                self.white_balance_button(request)
            elif name in ('advanced_get', 'advanced_apply'):
                changes = command.get('changes', {})
                controls = self.apply_controls(changes) if name == 'advanced_apply' else self.controls()
                self.emit('advanced', controls=controls, request=request, applied=name == 'advanced_apply',
                          readback_differences=[controls[k]['label'] for k, v in changes.items() if controls[k]['value'] != v])
                self.emit('settings', settings=self.settings().as_dict())
            elif name == 'save_preset':
                self.save_preset(command)
            elif name == 'load_preset':
                self.load_preset(command)
                self.emit('settings', settings=self.settings().as_dict())
        except Exception as exc:
            if name in ('advanced_get', 'advanced_apply') or (name == 'wb' and request):
                self.emit('advanced_failed', text=str(exc), request=request)
            self.emit('command_error', text=str(exc))

    def run(self):
        threading.Thread(target=self.stdin_reader, daemon=True, name='zed-stdin').start()
        sender = PreviewSender(self.lock)
        sender.start()
        width, height = self.camera.size
        self.emit('connected', settings=self.settings().as_dict(), gain_range=(0, 100), shape=(width, height),
                  model=self.camera.model, firmware=self.camera.firmware, sdk_version=self.camera.sdk_version,
                  flip=self.args.flip, mode_label=self.args.mode_label, simulated=self.camera.simulated)
        last_good = last_preview = last_report = time.monotonic()
        last_return = None
        received = total_good = 0
        try:
            while self.running:
                while True:
                    try:
                        command = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    self.handle(command)
                    if not self.running:
                        break
                if not self.running:
                    break
                called = time.perf_counter_ns()
                ok, code = self.camera.grab()
                returned = time.perf_counter_ns()
                outside = called - last_return if last_return is not None else None
                last_return = returned
                now = time.monotonic()
                if not ok:
                    if now - last_good > 3:
                        raise RuntimeError(f'ZED 连续 3 秒没有图像（{code}），请检查连接')
                    continue
                last_good = now
                received += 1
                total_good += 1
                if self.args.simulate and self.args.sim_app_delay_every and total_good % self.args.sim_app_delay_every == 0:
                    time.sleep(2.5 / self.args.fps)  # injected application-side delay
                if self.active:
                    row = self.session['audit'].observe(self.camera.timestamp_ns(), time.monotonic_ns(), time.time_ns(),
                                                        returned - called, outside, self.camera.dropped_total())
                    self.session['journal'].write(row)
                if now - last_preview >= 0.1:
                    sender.submit((self.camera.preview(self.full_preview), self.full_preview))
                    last_preview = now
                if now - last_report >= 0.5:
                    audit = self.session['audit'] if self.active else None
                    self.emit('stats', fps=received / (now - last_report), shape=(width, height),
                              frames=audit.count if audit else 0, repeats=0, missing=audit.live_gaps if audit else 0,
                              seconds=((audit.last_ns - audit.first_ns) / 1e9) if audit and audit.count else 0,
                              raw_bytes=width * height * 2 * 2)
                    received, last_report = 0, now
        except Exception as exc:
            traceback.print_exc()
            self.emit('fault', text=str(exc))
            if self.session:
                self.stop(exc)
        finally:
            sender.close()
            errors = []
            try:
                self.restore(self.original['values'])
            except Exception as exc:
                errors.append(f'恢复连接前参数失败：{exc}')
            try:
                self.camera.close()
            except Exception as exc:
                errors.append(str(exc))
            for text in errors:
                self.emit('command_error', text=text)
            self.emit('closed')


def list_devices():
    os.add_dll_directory(str(ZED_SDK / 'bin'))
    import pyzed.sl as sl
    return [dict(serial=str(d.serial_number), model=str(d.camera_model), state=str(d.camera_state), simulated=False)
            for d in sl.Camera.get_device_list()]


def verify_full(svo, received):
    """Decode every SVO2 frame and match it to the recorder's per-frame log by microsecond timestamp."""
    os.add_dll_directory(str(ZED_SDK / 'bin'))
    import pyzed.sl as sl
    logged, padding = [], set()
    with Path(received).open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if row.get('stop_padding'):
                padding.add(row['camera_timestamp_us'])
            elif not row['duplicate']:
                logged.append(row['camera_timestamp_us'])
    camera = sl.Camera()
    init = sl.InitParameters()
    init.set_from_svo_file(str(svo))
    init.svo_real_time_mode = False
    init.depth_mode = sl.DEPTH_MODE.NONE
    code = camera.open(init)
    if code != sl.ERROR_CODE.SUCCESS:
        return dict(valid=False, error=f'无法打开：{code}')
    stamps, stop = [], None
    try:
        reported = camera.get_svo_number_of_frames()
        while True:
            code = camera.grab()
            if code == sl.ERROR_CODE.END_OF_SVOFILE_REACHED:
                break
            if code != sl.ERROR_CODE.SUCCESS:
                stop = str(code)
                break
            stamps.append(camera.get_timestamp(sl.TIME_REFERENCE.IMAGE).get_nanoseconds() // 1000)
    finally:
        camera.close()
    saved, expected = set(stamps), set(logged)
    missing = sorted(expected - saved)
    extra = saved - expected - padding
    return dict(valid=not stop and not missing and not extra and len(stamps) == len(saved) and stamps == sorted(stamps),
                sdk_reported_frames=reported, decoded_frames=len(stamps), logged_frames=len(logged),
                duplicated_in_svo=len(stamps) - len(saved), logged_but_not_in_svo=len(missing),
                in_svo_but_not_logged=len(extra), stop_padding_in_svo=bool(padding & saved),
                order_ok=stamps == sorted(stamps), decode_stop=stop,
                note='每个计数帧都必须在 SVO2 中；停止填充帧可有可无')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--verify-svo', type=Path, help='SVO2 file to decode completely')
    parser.add_argument('--received', type=Path, help='matching <serial>.received.jsonl')
    parser.add_argument('--serial', default='0')
    parser.add_argument('--resolution', default='HD720', choices=list(SIZES))
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--flip', default='AUTO', choices=['AUTO', 'ON', 'OFF'])
    parser.add_argument('--mode-label', default='HD720 @ 30')
    parser.add_argument('--simulate', action='store_true')
    parser.add_argument('--sim-fps', type=float)
    for name in ('drop', 'dup', 'stall', 'reset'):
        parser.add_argument(f'--sim-{name}-every', type=int, default=0)
    parser.add_argument('--sim-app-delay-every', type=int, default=0)
    parser.add_argument('--sim-lose-last', action='store_true')
    parser.add_argument('--sim-hang-prepare', action='store_true', help='simulate a camera stuck inside the SDK (tests)')
    parser.add_argument('--sim-freeze-after', type=float, default=0, help='grab() blocks forever after this many seconds (tests)')
    parser.add_argument('--sim-open-delay', type=float, default=0, help='opening the camera blocks this many seconds (tests)')
    parser.add_argument('--sim-light', type=int, default=4700, help='simulated scene colour temperature in K (tests)')
    parser.add_argument('--sim-dark', action='store_true', help='image too dark to measure white balance (tests)')
    parser.add_argument('--sim-led-fail', action='store_true')
    args = parser.parse_args()
    if args.verify_svo:
        result = verify_full(args.verify_svo, args.received or args.verify_svo.with_suffix('.received.jsonl'))
        PROTO.write((json.dumps(result, ensure_ascii=False) + '\n').encode('utf-8'))
        return
    if args.list:
        try:
            PROTO.write((json.dumps(list_devices(), ensure_ascii=False) + '\n').encode('utf-8'))
        except Exception as exc:
            PROTO.write((json.dumps(dict(error=str(exc)), ensure_ascii=False) + '\n').encode('utf-8'))
        return
    try:
        camera = SimCamera(args) if args.simulate else LiveCamera(args)
    except Exception as exc:
        traceback.print_exc()
        lock = threading.Lock()
        for event, data in (('fault', dict(text=str(exc))), ('closed', {})):
            write_message(PROTO, b'J', json.dumps(dict(event=event, **data), ensure_ascii=False).encode('utf-8'), lock)
        return
    Worker(args, camera).run()
    sys.stderr.flush()
    os._exit(0)  # see mv_worker.py: avoid teardown crashes from daemon threads in native code


if __name__ == '__main__':
    main()
