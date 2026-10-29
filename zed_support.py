"""ZED integration on the recorder side: process protocol, settings, integrity audit and worker thread.

pyzed is imported only by zed_worker.py, which runs in .venv-zed as one process per camera.
This module uses the standard library and Pillow only, so it also runs in .venv-recorder.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
ZED_PYTHON = ROOT / '.venv-zed' / 'Scripts' / 'python.exe'
WORKER_SCRIPT = ROOT / 'zed_worker.py'
ZED_SDK = Path(os.environ.get('ZED_SDK_ROOT_DIR', r'C:\Program Files (x86)\ZED SDK'))

# Mode label -> (SDK resolution, nominal fps, per-eye size).
MODES = {
    'HD1080 @ 30': ('HD1080', 30, (1920, 1080)),
    'HD720 @ 60': ('HD720', 60, (1280, 720)),
    'HD720 @ 30': ('HD720', 30, (1280, 720)),
    'HD1080 @ 15': ('HD1080', 15, (1920, 1080)),
    'HD2K @ 15': ('HD2K', 15, (2208, 1242)),
    'VGA @ 100': ('VGA', 100, (672, 376)),
}
DEFAULT_MODE = 'HD1080 @ 30'
COMPRESSIONS = {'H.264 有损（默认）': 'H264', 'H.265 有损': 'H265', 'H.265 无损': 'H265_LOSSLESS', 'H.264 无损': 'H264_LOSSLESS'}
DEFAULT_COMPRESSION = 'H.264 有损（默认）'
FLIPS = {'自动（按 IMU 判断倒装）': 'AUTO', '不翻转': 'OFF', '翻转 180°': 'ON'}
DEFAULT_FLIP = '自动（按 IMU 判断倒装）'

ZED_METADATA_UNITS = dict(
    camera_timestamp_ns='ZED SDK image timestamp, Unix nanoseconds on the host SYSTEM_CLOCK; SVO2 keeps microseconds',
    camera_timestamp_us='camera_timestamp_ns // 1000; use this to match frames inside the SVO2',
    interval_ns='difference to the previous unique camera timestamp',
    host_receive_monotonic_ns='host monotonic nanoseconds after grab() returned; not exposure time',
    host_receive_unix_ns='host Unix nanoseconds after grab() returned',
    grab_ms='time spent inside grab(), including waiting for the next frame and SDK recording work',
    outside_grab_ms='time between the previous grab() return and this grab() call (application processing)',
    app_late='outside_grab_ms exceeded one nominal frame period',
    sdk_dropped_total='raw Camera.get_frame_dropped_count(); it can reset to 0 under load')


def safe_name(value):
    return re.sub(r'[^a-zA-Z0-9_-]', '_', str(value))[:80] or 'camera'


# Protocol helpers are shared with the MindVision process worker.
from subprocess_worker import (SubprocessWorker, write_message, read_message, read_exact,  # noqa: E402,F401
                               pack_preview, unpack_preview)


# ---------------------------------------------------------------- settings

@dataclass
class ZedSettings:
    """exposure/gain are 0-100 SDK units; None means automatic exposure and gain (AEC_AGC)."""
    exposure: int | None = None
    gain: int | None = None
    rotate180: bool = False  # interface compatibility only; orientation is the SDK flip mode

    @property
    def automatic(self):
        return self.exposure is None and self.gain is None

    def validate(self, fps=None):
        if (self.exposure is None) != (self.gain is None):
            raise ValueError('ZED 的曝光和增益同时自动或同时手动：两项都留空，或都填写 0–100')
        for name, value in (('曝光', self.exposure), ('增益', self.gain)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100):
                raise ValueError(f'ZED {name}须为 0–100 的整数，留空表示自动')

    def as_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(data.get('exposure'), data.get('gain'), bool(data.get('rotate180', False)))


# ---------------------------------------------------------------- integrity

class ZedAudit:
    """Per-frame bookkeeping. Camera timestamps are the reference; the SDK drop counter is advisory."""

    def __init__(self, nominal_fps):
        self.nominal_fps = nominal_fps
        self.period_ns = 1e9 / nominal_fps
        self.count = self.duplicates = self.app_late = 0
        self.app_late_max_ms = 0.0
        self.grab_max_ms = 0.0
        self.intervals = []
        self.last_us = self.last_ns = None
        self.first_ns = None
        self.first_us = self.last_unique_us = None
        self.sdk_base = self.sdk_last = None
        self.sdk_increments = self.sdk_resets = 0
        self.live_gaps = 0

    def observe(self, camera_ns, host_monotonic_ns, host_unix_ns, grab_ns, outside_ns, sdk_dropped_total):
        camera_us = camera_ns // 1000
        duplicate = camera_us == self.last_us
        interval = None
        late = outside_ns is not None and outside_ns > self.period_ns
        if not duplicate:
            if self.last_ns is not None:
                interval = camera_ns - self.last_ns
                if interval > 0:
                    # A gap right after the application was late is attributed to the application:
                    # the SDK keeps only the newest frame while grab() is not being called.
                    self.intervals.append((interval, late))
                    if interval > 1.5 * self.period_ns:
                        self.live_gaps += round(interval / self.period_ns) - 1
            if self.first_ns is None:
                self.first_ns, self.first_us = camera_ns, camera_us
            self.last_ns, self.last_us = camera_ns, camera_us
            self.last_unique_us = camera_us
            index = self.count
            self.count += 1
        else:
            self.duplicates += 1
            index = None
        if late:
            self.app_late += 1
        if outside_ns is not None:
            self.app_late_max_ms = max(self.app_late_max_ms, outside_ns / 1e6)
        self.grab_max_ms = max(self.grab_max_ms, grab_ns / 1e6)
        if sdk_dropped_total is not None:
            if self.sdk_last is None:
                self.sdk_base = sdk_dropped_total
            elif sdk_dropped_total >= self.sdk_last:
                self.sdk_increments += sdk_dropped_total - self.sdk_last
            else:
                self.sdk_resets += 1
                self.sdk_increments += sdk_dropped_total
            self.sdk_last = sdk_dropped_total
        return dict(receive_index=index, duplicate=duplicate, camera_timestamp_ns=camera_ns, camera_timestamp_us=camera_us,
                    interval_ns=interval, host_receive_monotonic_ns=host_monotonic_ns, host_receive_unix_ns=host_unix_ns,
                    grab_ms=round(grab_ns / 1e6, 4), outside_grab_ms=None if outside_ns is None else round(outside_ns / 1e6, 4),
                    app_late=late, sdk_dropped_total=sdk_dropped_total)

    def interval_summary(self):
        """Median interval and missing-frame estimates split into camera-side and application-side."""
        values = [interval for interval, _ in self.intervals]
        if not values:
            return None, [], dict(camera_gaps=0, camera_missing=0, app_gaps=0, app_missing=0)
        median = statistics.median(values)
        counts = dict(camera_gaps=0, camera_missing=0, app_gaps=0, app_missing=0)
        for interval, late in self.intervals:
            if interval > 1.5 * median:
                side = 'app' if late else 'camera'
                counts[side + '_gaps'] += 1
                counts[side + '_missing'] += max(0, round(interval / median) - 1)
        return median, values, counts

    def report(self, svo_check=None, errors=(), padding_us=None):
        """padding_us: an extra frame grabbed while stopping; SDK 5.5 may drop the file's last frame."""
        median, intervals, counts = self.interval_summary()
        missing, gaps = counts['camera_missing'], counts['camera_gaps']
        measured = 1e9 / median if median else None
        warnings = []
        if missing:
            warnings.append(f'相机时间戳缺口：估计缺 {missing} 帧（{gaps} 处缺口）。属于相机 / USB / SDK 侧，帧未到达程序')
        if self.sdk_increments:
            warnings.append(f'SDK 丢帧计数累计 {self.sdk_increments}' + (f'，期间计数清零 {self.sdk_resets} 次，仅供参考' if self.sdk_resets else ''))
        if self.duplicates:
            warnings.append(f'grab 重复返回同一时间戳 {self.duplicates} 次，已剔除，不计为新帧')
        if self.app_late:
            warnings.append(f'程序处理不足：{self.app_late} 次两次取帧之间超过一个周期（最长 {self.app_late_max_ms:.1f} ms），'
                            f'由此缺 {counts["app_missing"]} 帧')
        svo_ok = None
        if svo_check is not None:
            if svo_check.get('error'):
                warnings.append('SVO2 复核失败：' + svo_check['error'])
                svo_ok = False
            else:
                frames, last = svo_check.get('frames', 0), svo_check.get('last_us')
                # Accepted endings: all counted frames, optionally followed by the stop padding frame.
                accepted = {(self.count, self.last_unique_us)}
                if padding_us is not None:
                    accepted.add((self.count + 1, padding_us))
                svo_ok = (frames, last) in accepted and svo_check.get('first_us') == self.first_us
                if (frames, last) not in accepted:
                    warnings.append(f'SVO2 记录 {frames} 帧，程序计数 {self.count} 帧' +
                                    ('（另有 1 帧停止填充帧）' if padding_us is not None else '') + '，结尾与程序记录不一致')
                if svo_check.get('first_us') != self.first_us:
                    warnings.append('SVO2 第一帧时间戳与程序记录不一致')
        errors = list(errors)
        if errors or not self.count or svo_ok is False and svo_check.get('error'):
            status = 'failed'
        elif warnings:
            status = 'review'
        else:
            status = 'pass_observed'
        rate_note = None
        if measured:
            rate_note = f'实际 {measured:.3f} fps，标称 {self.nominal_fps} fps（{(measured/self.nominal_fps-1)*100:+.2f}%）'
        return dict(
            status=status, frames_unique=self.count, duplicates=self.duplicates,
            camera_missing_frames=missing, camera_gap_count=gaps,
            sdk_dropped_increments=self.sdk_increments, sdk_counter_resets=self.sdk_resets,
            app_late_count=self.app_late, app_missing_frames=counts['app_missing'], app_gap_count=counts['app_gaps'],
            app_late_max_ms=round(self.app_late_max_ms, 3), grab_max_ms=round(self.grab_max_ms, 3),
            mp4_repeated_frames=None, mp4_note='SVO2 不补帧；如另行导出定帧率 MP4，补帧在导出时单独计数',
            interval_ns=dict(median=median, mean=statistics.fmean(intervals) if intervals else None,
                             min=min(intervals) if intervals else None, max=max(intervals) if intervals else None,
                             count=len(intervals) if intervals else 0),
            measured_fps=measured, nominal_fps=self.nominal_fps, rate_note=rate_note,
            first_timestamp_us=self.first_us, last_timestamp_us=self.last_unique_us, stop_padding_us=padding_us,
            svo_check=svo_check, warnings=warnings, errors=errors,
            scope='仅核对观测到的计数与时间戳；不是硬件零丢帧或跨相机同步证明')


def write_zed_report(path, info):
    integrity = info['integrity']
    lines = ['ZED 采集完整性报告', f'相机：{info["serial"]}（{info["model"]}）', f'模式：{info["mode_label"]}，压缩 {info["compression"]}',
             f'结果：{integrity["status"]}', integrity['scope'],
             f'收到并写入 SVO2 的帧：{integrity["frames_unique"]}',
             f'1. 相机 / SDK 缺帧（按相机时间戳缺口估算）：{integrity["camera_missing_frames"]} 帧，{integrity["camera_gap_count"]} 处',
             f'   SDK 丢帧计数累计：{integrity["sdk_dropped_increments"]}（清零 {integrity["sdk_counter_resets"]} 次；仅供参考）',
             f'2. 程序处理不足：取帧不及时 {integrity["app_late_count"]} 次，因此缺 {integrity["app_missing_frames"]} 帧'
             f'（两次取帧间最长 {integrity["app_late_max_ms"]} ms）',
             f'3. MP4 补帧：不适用（{integrity["mp4_note"]}）',
             f'重复返回的帧（已剔除）：{integrity["duplicates"]}']
    if integrity.get('rate_note'):
        lines.append('帧率：' + integrity['rate_note'])
    check = integrity.get('svo_check')
    if check:
        lines.append('SVO2 复核：' + json.dumps({k: v for k, v in check.items() if k != 'note'}, ensure_ascii=False))
    lines.append('注意：ZED 时间戳为主机系统时钟；不同相机之间未硬件同步。')
    lines += ['注意：' + w for w in integrity['warnings']]
    lines += ['错误：' + e for e in integrity['errors']]
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


# ---------------------------------------------------------------- discovery

def worker_python(simulate=False):
    if ZED_PYTHON.is_file():
        return str(ZED_PYTHON)
    if simulate:
        return sys.executable
    raise RuntimeError('未找到 .venv-zed，ZED 不可用')


def list_devices(demo=False, python=None, timeout=30):
    """Return (devices, note). Never raises: ZED problems must not block MindVision use."""
    if demo:
        return [dict(serial='DEMO-ZED-01', model='ZED 2i（模拟）', kind='zed', simulated=True)], None
    if not ZED_PYTHON.is_file() and python is None:
        return [], None
    if not ZED_SDK.is_dir():
        return [], '未安装 ZED SDK，已跳过 ZED 搜索'
    try:
        result = subprocess.run([python or str(ZED_PYTHON), '-B', str(WORKER_SCRIPT), '--list'], capture_output=True,
                                timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        lines = [line for line in result.stdout.decode('utf-8', 'replace').splitlines() if line.startswith('[') or line.startswith('{')]
        if not lines:
            detail = result.stderr.decode('utf-8', 'replace').strip().splitlines()[-1:] or [f'退出代码 {result.returncode}']
            return [], 'ZED 搜索失败：' + detail[0]
        devices = json.loads(lines[-1])
        if isinstance(devices, dict):
            return [], 'ZED 搜索失败：' + devices.get('error', '未知错误')
        for device in devices:
            device['kind'] = 'zed'
        return devices, None
    except Exception as exc:
        return [], f'ZED 搜索失败：{exc}'


# ---------------------------------------------------------------- app-side worker

class ZedWorker(SubprocessWorker):
    """Drop-in counterpart of CameraWorker for one ZED, backed by a zed_worker.py process in .venv-zed."""
    settings_class = ZedSettings
    label = 'ZED '
    log_prefix = 'zed'

    def __init__(self, info, events, demo=False, python=None, log_folder=None):
        super().__init__(info, events, log_folder)
        self.demo, self.python = demo, python

    def command_line(self):
        resolution, fps, _ = MODES[self.info.get('mode', DEFAULT_MODE)]
        args = [self.python or worker_python(self.demo), '-B', str(WORKER_SCRIPT), '--serial', str(self.info['serial']),
                '--resolution', resolution, '--fps', str(fps), '--flip', FLIPS[self.info.get('flip', DEFAULT_FLIP)],
                '--mode-label', self.info.get('mode', DEFAULT_MODE)]
        if self.demo or self.info.get('simulated'):
            args.append('--simulate')
        args += self.info.get('extra_args', [])
        return args
