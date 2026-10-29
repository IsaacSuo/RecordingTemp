"""Multi-camera capture workers. SDK calls stay on each camera's own thread."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime
import csv
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image, ImageDraw
from record_video import Timeline
from capture_performance import StageTimings, PreviewProcessor
from encoding_support import encoder_options, orientation_filters, FrameJournal
from research_data import (FrameAudit, ResearchRecording, clean_json, write_json,
                           write_report, software_info, utc_now, METADATA_UNITS)
from camera_controls import orient_rgb, orientation, sdk_controls, apply_sdk_controls, demo_controls, validate_changes
from subprocess_worker import SubprocessWorker


def header_dict(head):
    return clean_json({name: getattr(head, name) for name, *_ in head._fields_})


@dataclass
class Settings:
    exposure_ms: float = 20.0
    gain: float = 1.0
    rotate180: bool = True

    def validate(self, fps=None):
        if not math.isfinite(self.exposure_ms) or self.exposure_ms <= 0:
            raise ValueError('曝光时间必须大于 0')
        if not math.isfinite(self.gain) or self.gain <= 0:
            raise ValueError('增益必须大于 0')
        if fps is not None and self.exposure_ms >= 1000 / fps:
            raise ValueError(f'{fps} FPS 的曝光须小于 {1000/fps:.2f} ms，建议使用 {20 if fps == 30 else 10} ms')


def safe_name(value):
    return re.sub(r'[^a-zA-Z0-9_-]', '_', value)[:80] or 'camera'


class SDKSystem:
    def __init__(self):
        import mvsdk
        self.sdk = mvsdk
        self.check(mvsdk.CameraSdkInit(1))

    def check(self, code):
        if code != self.sdk.CAMERA_STATUS_SUCCESS:
            raise self.sdk.CameraException(code)

    def get(self, fn, *args):
        value = fn(*args)
        self.check(self.sdk.GetLastError())
        return value

    def scan(self):
        return [dict(serial=d.GetSn(), model=d.GetProductName(), native=d)
                for d in self.get(self.sdk.CameraEnumerateDevice)]

    def open(self, info):
        return SDKCamera(self, info)


class SDKCamera:
    def __init__(self, system, info):
        self.sys, self.s = system, system.sdk
        self.h = self.buffer = None
        self.backup = None
        self.capture_metadata = self.capture_raw = False
        self.frame_metadata = self.raw_bytes = None
        self.optional_frame_errors = {}
        self.view_rotate180 = True
        self.mirror_horizontal = True
        self.mirror_vertical = False
        self.temp = tempfile.TemporaryDirectory(prefix='mvsdk_recorder_')
        try:
            if system.get(self.s.CameraIsOpened, info['native']):
                raise RuntimeError('相机被其他程序占用，请先在官方软件中断开此相机')
            self.h = self.s.CameraInit(info['native'], -1, -1)
            backup = Path(self.temp.name) / 'before.config'
            system.check(self.s.CameraSaveParameterToFile(self.h, str(backup)))
            self.backup = backup
            self.cap = system.get(self.s.CameraGetCapability, self.h)
            self.gain_step = self.cap.sExposeDesc.fAnalogGainStep
            self.gain_min = self.cap.sExposeDesc.uiAnalogGainMin * self.gain_step
            self.gain_max = self.cap.sExposeDesc.uiAnalogGainMax * self.gain_step
            if self.gain_step <= 0:
                raise RuntimeError('相机返回了无效的增益步长')
            self.exposure_range = system.get(self.s.CameraGetExposureTimeRange, self.h)
            resolution = system.get(self.s.CameraGetImageResolution, self.h)
            if resolution.iWidthZoomSw > 0 or resolution.iHeightZoomSw > 0:
                raise RuntimeError('请在官方软件中关闭软件缩放后重新连接；本程序保留当前分辨率')
            self.capacity = self.cap.sResolutionRange.iWidthMax * self.cap.sResolutionRange.iHeightMax * 3
            self.buffer = self.s.CameraAlignMalloc(self.capacity, 16)
            if not self.buffer:
                raise MemoryError('无法分配图像缓冲区')
            system.check(self.s.CameraSetTriggerMode(self.h, 0))
            system.check(self.s.CameraSetAeState(self.h, 0))
            speeds = [self.cap.pFrameSpeedDesc[i].iIndex for i in range(self.cap.iFrameSpeedDesc)]
            if 1 in speeds:
                system.check(self.s.CameraSetFrameSpeed(self.h, 1))
            system.check(self.s.CameraSetIspOutFormat(self.h, self.s.CAMERA_MEDIA_TYPE_RGB8))
            system.check(self.s.CameraSetRotate(self.h, 0))
            # Keep existing color processing, including WB, gamma and digital gains.
            settings = self.settings()
            self.apply(Settings(min(settings.exposure_ms, 20), settings.gain, settings.rotate180))
            system.check(self.s.CameraPlay(self.h))
        except Exception:
            self.close()
            raise

    def settings(self):
        get, s, h = self.sys.get, self.s, self.h
        return Settings(get(s.CameraGetExposureTime, h) / 1000,
                        get(s.CameraGetAnalogGain, h) * self.gain_step,
                        self.view_rotate180)

    def apply(self, settings):
        settings.validate()
        low, high, _ = self.exposure_range
        if not low <= settings.exposure_ms * 1000 <= high:
            raise ValueError(f'本机曝光范围为 {low/1000:.4f}–{high/1000:.2f} ms')
        if not self.gain_min <= settings.gain <= self.gain_max:
            raise ValueError(f'本机增益范围为 {self.gain_min:g}–{self.gain_max:g} 倍')
        check, s, h = self.sys.check, self.s, self.h
        check(s.CameraSetAeState(h, 0))
        check(s.CameraSetExposureTime(h, settings.exposure_ms * 1000))
        check(s.CameraSetAnalogGain(h, round(settings.gain / self.gain_step)))
        self.view_rotate180 = bool(settings.rotate180)
        return self.settings()

    def advanced(self):
        return sdk_controls(self)

    def apply_advanced(self, changes):
        return apply_sdk_controls(self, changes)

    def white_balance(self):
        if self.cap.sIspCapacity.bMonoSensor:
            raise ValueError('黑白相机无需白平衡')
        self.sys.check(self.s.CameraSetWbMode(self.h, 0))
        self.sys.check(self.s.CameraSetOnceWB(self.h))

    def save_config(self, path):
        # The vendor API accepts GBK paths. Use an ASCII leaf in the temp directory
        # and Python for the destination copy, including Unicode output paths.
        staging = Path(self.temp.name) / 'used.config'
        self.sys.check(self.s.CameraSaveParameterToFile(self.h, str(staging)))
        shutil.copyfile(staging, path)

    def lock_automatic(self):
        self.sys.check(self.s.CameraSetAeState(self.h, 0))
        if not self.cap.sIspCapacity.bMonoSensor:
            self.sys.check(self.s.CameraSetWbMode(self.h, 0))

    def snapshot(self):
        values, unavailable = {}, {}
        functions = dict(exposure_us='CameraGetExposureTime', analog_gain_steps='CameraGetAnalogGain',
                         auto_exposure='CameraGetAeState', auto_white_balance='CameraGetWbMode',
                         rgb_digital_gain_percent='CameraGetGain', gamma_sdk='CameraGetGamma',
                         contrast_sdk='CameraGetContrast', saturation_sdk='CameraGetSaturation',
                         sharpness_sdk='CameraGetSharpness', black_level_sdk='CameraGetBlackLevel',
                         lut_mode='CameraGetLutMode', denoise_2d='CameraGetNoiseFilterState',
                         denoise_3d='CameraGetDenoise3DParams', dead_pixel_correction='CameraGetCorrectDeadPixel',
                         flat_field='CameraFlatFieldingCorrectGetEnable', rotation='CameraGetRotate',
                         flat_field_parameter_state='CameraFlatFieldingCorrectGetParameterState',
                         isp_processor='CameraGetIspProcessor', lut_preset='CameraGetLutPresetSel',
                         frame_speed_mode='CameraGetFrameSpeed', trigger_mode='CameraGetTriggerMode',
                         raw_media_descriptor_index='CameraGetMediaType', isp_output_format='CameraGetIspOutFormat',
                         firmware='CameraGetFirmwareVersion', interface_version='CameraGetInerfaceVersion')
        for key, name in functions.items():
            try:
                values[key] = self.sys.get(getattr(self.s, name), self.h)
            except Exception as exc:
                values[key] = None
                unavailable[key] = str(exc)
        for key, name, argument in (('mirror_horizontal', 'CameraGetMirror', 0), ('mirror_vertical', 'CameraGetMirror', 1),
                                     ('bayer_algorithm_software', 'CameraGetBayerDecAlgorithm', 0),
                                     ('bayer_algorithm_hardware', 'CameraGetBayerDecAlgorithm', 1)):
            try:
                values[key] = self.sys.get(getattr(self.s, name), self.h, argument)
            except Exception as exc:
                values[key] = None
                unavailable[key] = str(exc)
        try:
            resolution = self.sys.get(self.s.CameraGetImageResolution, self.h)
            values['resolution'] = {name: getattr(resolution, name) for name, *_ in resolution._fields_
                                    if isinstance(getattr(resolution, name), (int, float))}
            values['sdk_version'] = self.sys.get(self.s.CameraSdkGetVersionString)
        except Exception as exc:
            unavailable['device_description'] = str(exc)
        values['raw_media_formats'] = [dict(index=self.cap.pMediaTypeDesc[i].iIndex,
                                            code=self.cap.pMediaTypeDesc[i].iMediaType,
                                            description=self.cap.pMediaTypeDesc[i].GetDescription())
                                      for i in range(self.cap.iMediaTypeDesc)]
        return clean_json(dict(values=values, unsupported_or_failed=unavailable, application_orientation=orientation(self),
                               analog_gain_step=self.gain_step, frame_optional_errors=self.optional_frame_errors.copy(),
                               note='SDK units retained; null means unavailable, not zero. No synchronized camera clocks.'))

    def load_config(self, path):
        staging = Path(self.temp.name)/'preset.config'
        rollback = Path(self.temp.name)/'rollback.config'
        self.sys.check(self.s.CameraSaveParameterToFile(self.h, str(rollback)))
        shutil.copyfile(path, staging)
        self.sys.check(self.s.CameraPause(self.h))
        try:
            self.sys.check(self.s.CameraReadParameterFromFile(self.h, str(staging)))
            resolution = self.sys.get(self.s.CameraGetImageResolution, self.h)
            if resolution.iWidthZoomSw > 0 or resolution.iHeightZoomSw > 0:
                raise ValueError('预设包含软件缩放，本版不支持')
            self.sys.check(self.s.CameraSetTriggerMode(self.h, 0))
            self.sys.check(self.s.CameraSetIspOutFormat(self.h, self.s.CAMERA_MEDIA_TYPE_RGB8))
            self.sys.check(self.s.CameraSetRotate(self.h, 0))
            self.lock_automatic()
        except Exception:
            self.sys.check(self.s.CameraReadParameterFromFile(self.h, str(rollback)))
            raise
        finally:
            self.sys.check(self.s.CameraPlay(self.h))
        return self.settings()

    def read(self, wanted):
        s, h, check = self.s, self.h, self.sys.check
        if not hasattr(self, 'performance'):
            self.performance = StageTimings()
        measured = self.performance.call
        try:
            raw, head = measured('get_buffer_including_wait', s.CameraGetImageBuffer, h, 200)
        except s.CameraException as exc:
            if exc.error_code == s.CAMERA_STATUS_TIME_OUT:
                return None
            raise
        try:
            tick = int(head.uiTimeStamp)
            self.last_raw_size = int(head.uBytes)
            self.raw_bytes = self.frame_metadata = None
            if getattr(self, 'capture_metadata', False):
                self.frame_metadata = dict(camera_tick_100us=tick,
                                           host_receive_monotonic_ns=time.monotonic_ns(),
                                           host_receive_unix_ns=time.time_ns(), raw_header=header_dict(head))
                for key, name in (('camera_frame_id', 'CameraGetFrameID'), ('camera_timestamp_us', 'CameraGetFrameTimeStamp')):
                    value = None
                    if key not in self.optional_frame_errors:
                        try:
                            value = measured(name, self.sys.get, getattr(s, name), h)
                        except Exception as exc:
                            self.optional_frame_errors[key] = str(exc)
                    self.frame_metadata[key] = value
                self.frame_metadata['frame_optional_errors'] = self.optional_frame_errors.copy()
            if getattr(self, 'capture_raw', False):
                if not 0 < head.uBytes <= self.capacity*4:
                    raise RuntimeError('SDK 原始帧字节数异常')
                self.raw_bytes = measured('raw_copy', ctypes.string_at, raw, head.uBytes)
            if not wanted(tick):
                return tick, None, None
            check(measured('sdk_isp', s.CameraImageProcess, h, raw, self.buffer, head))
            shape = (head.iWidth, head.iHeight)
            count = shape[0] * shape[1] * 3
            if min(shape) <= 0 or count > self.capacity or head.uiMediaType != s.CAMERA_MEDIA_TYPE_RGB8:
                raise RuntimeError('相机返回了不支持的图像尺寸或格式')
            pixels = measured('rgb_copy', ctypes.string_at, self.buffer, count)
        finally:
            check(measured('release_buffer', s.CameraReleaseImageBuffer, h, raw))
        # Release SDK-owned memory before application transforms and encoding.
        if getattr(self, 'defer_orientation', False):
            return tick, shape, pixels
        pixels = measured('orientation', orient_rgb, pixels, shape, getattr(self, 'view_rotate180', False),
                          getattr(self, 'mirror_horizontal', False), getattr(self, 'mirror_vertical', False))
        return tick, shape, pixels

    def stats(self):
        stat = self.sys.get(self.s.CameraGetFrameStatistic, self.h)
        return dict(total=stat.iTotal, captured=stat.iCapture, lost=stat.iLost)

    def close(self):
        errors = []
        if self.h is not None:
            try:
                self.sys.check(self.s.CameraPause(self.h))
            except Exception as exc:
                errors.append(str(exc))
            if self.backup:
                try:
                    self.sys.check(self.s.CameraReadParameterFromFile(self.h, str(self.backup)))
                except Exception as exc:
                    errors.append(f'恢复原参数失败，备份保留在 {self.backup}: {exc}')
            try:
                self.sys.check(self.s.CameraUnInit(self.h))
            except Exception as exc:
                errors.append(str(exc))
            self.h = None
        if self.buffer:
            self.s.CameraAlignFree(self.buffer)
            self.buffer = None
        if errors:
            # Keep recovery data on disk instead of TemporaryDirectory auto-cleanup.
            self.temp._finalizer.detach()
            raise RuntimeError('；'.join(errors))
        self.temp.cleanup()


class DemoSystem:
    def scan(self):
        return [dict(serial=f'DEMO-{n+1:02}', model='模拟相机', native=n) for n in range(2)]

    def open(self, info):
        return DemoCamera(info)


class DemoCamera:
    @property
    def view_rotate180(self):
        return self.current.rotate180

    def __init__(self, info, rate=90):
        self.info, self.rate = info, rate
        self.current = Settings()
        self.origin = time.monotonic()
        self.count = 0
        self.capture_metadata = self.capture_raw = False
        self.frame_metadata = self.raw_bytes = None
        self.gain_min, self.gain_max = 1, 8
        self.mirror_horizontal = True
        self.mirror_vertical = False
        self.extra_controls = {}

    def settings(self):
        return self.current

    def apply(self, settings):
        settings.validate()
        if not 1 <= settings.gain <= 8:
            raise ValueError('增益范围为 1–8 倍')
        self.current = settings
        return settings

    def white_balance(self):
        pass

    def advanced(self):
        return demo_controls(self)

    def apply_advanced(self, changes):
        merged = validate_changes(self.advanced(), changes)
        self.apply(Settings(merged['exposure_ms'], merged['gain'], merged['rotate180']))
        self.mirror_horizontal = merged['mirror_horizontal']
        self.mirror_vertical = merged['mirror_vertical']
        self.extra_controls.update({k: v for k, v in changes.items() if k not in
                                    ('exposure_ms', 'gain', 'rotate180', 'mirror_horizontal', 'mirror_vertical')})
        return self.advanced()

    def save_config(self, path):
        write_json(path, dict(simulated=True, settings=vars(self.current), extra_controls=self.extra_controls))

    def load_config(self, path):
        saved = json.loads(Path(path).read_text(encoding='utf-8'))
        self.extra_controls = saved.get('extra_controls', {})
        return self.apply(Settings(**saved['settings']))

    def lock_automatic(self):
        pass

    def snapshot(self):
        return dict(simulated=True, application_orientation=orientation(self), values=dict(settings=vars(self.current).copy(),
                    advanced=self.extra_controls.copy(),
                    auto_exposure=0, auto_white_balance=0, raw_media='SIMULATED_MONO8'), unsupported_or_failed={})

    def read(self, wanted):
        deadline = self.origin + self.count / self.rate
        time.sleep(max(0, deadline - time.monotonic()))
        tick = round(self.count * 10000 / self.rate) & 0xffffffff
        self.count += 1
        self.last_raw_size = 640*400
        self.frame_metadata = dict(camera_tick_100us=tick, host_receive_monotonic_ns=time.monotonic_ns(),
                                   host_receive_unix_ns=time.time_ns(), camera_frame_id=self.count,
                                   camera_timestamp_us=tick*100, simulated=True,
                                   raw_header=dict(uiTimeStamp=tick, uiExpTime=round(self.current.exposure_ms*1000),
                                                   fAnalogGain=self.current.gain, uBytes=640*400,
                                                   iWidth=640, iHeight=400, uiMediaType=0x01080001,
                                                   bIsTrigger=0))
        self.raw_bytes = bytes([self.count % 256])*(640*400) if self.capture_raw else None
        if not wanted(tick):
            return tick, None, None
        shape = (640, 400)
        image = Image.new('RGB', shape, (18, 31, 47))
        draw = ImageDraw.Draw(image)
        for x in range(0, 641, 40):
            draw.line((x, 0, x, 400), fill=(31, 48, 64))
        for y in range(0, 401, 40):
            draw.line((0, y, 640, y), fill=(31, 48, 64))
        x = (self.count * 3 + self.info['native'] * 180) % 580
        draw.ellipse((x, 180, x+54, 234), fill=(64, 206, 172))
        draw.text((24, 28), self.info['serial'] + '  /  SIMULATED', fill='white', font_size=26)
        draw.text((24, 70), f'Frame {self.count:06d}   {tick/10000:.2f}s', fill=(160, 190, 208), font_size=20)
        if getattr(self, 'defer_orientation', False):
            return tick, shape, image.tobytes()
        return tick, shape, orient_rgb(image.tobytes(), shape, self.current.rotate180,
                                      self.mirror_horizontal, self.mirror_vertical)

    def stats(self):
        return dict(total=self.count, captured=self.count, lost=0)

    def close(self):
        pass


class Encoder:
    """One bounded encoding queue per camera, with a ledger of frames actually written."""
    def __init__(self, output, shape, fps, ffmpeg=None, view=None):
        self.output, self.shape, self.fps = Path(output), shape, fps
        # NVENC initializes after its first input. Absorb that short startup burst
        # without blocking capture; sustained overload still fails explicitly.
        self.queue = queue.Queue(maxsize=max(8, min(fps*2, 256*1024*1024//(shape[0]*shape[1]*3))))
        ffmpeg = ffmpeg or shutil.which('ffmpeg') or 'ffmpeg'
        codec_args, self.codec_info = encoder_options(ffmpeg)
        self.codec_info['orientation_filters'] = orientation_filters(view)
        self.queue_peak = 0
        self.error = None
        self.written = self.repeats = 0
        self.performance = StageTimings()
        self.enqueue_performance = StageTimings()
        self.log = self.output.with_suffix('.ffmpeg.log').open('xb')
        self.ledger = None
        self.process = None
        try:
            self.ledger = self.output.with_suffix('.frames.csv').open('x', newline='', encoding='utf-8')
            self.csv = csv.writer(self.ledger)
            self.csv.writerow(['output_frame', 'output_seconds', 'source_camera_tick_100us', 'repeated', 'source_receive_index'])
            self.process = subprocess.Popen([
                ffmpeg, '-hide_banner', '-loglevel', 'warning',
                '-n', '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{shape[0]}x{shape[1]}',
                '-framerate', str(fps), '-i', 'pipe:0', '-an', *codec_args, *orientation_filters(view),
                '-filter_threads', '1', '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart', str(self.output)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log, bufsize=0,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.thread = threading.Thread(target=self._run, name='encode-'+self.output.stem, daemon=True)
            self.thread.start()
        except Exception:
            if self.process:
                self.process.kill()
                self.process.wait()
            if self.ledger:
                self.ledger.close()
            self.log.close()
            raise

    def _run(self):
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    break
                index, payload, repeated = item
                pixels, tick, *origin = payload
                data = memoryview(pixels)
                while data:
                    count = self.performance.call('pipe_write', self.process.stdin.write, data)
                    if not count:
                        raise OSError('编码器管道关闭')
                    data = data[count:]
                self.csv.writerow([index, f'{index/self.fps:.9f}', tick, int(repeated), origin[0] if origin else None])
                self.written += 1
                self.repeats += int(repeated)
            self.process.stdin.close()
            if self.process.wait(timeout=30) != 0:
                raise RuntimeError(f'FFmpeg 编码失败，详见 {self.output.with_suffix(".ffmpeg.log")}')
        except Exception as exc:
            self.error = exc

    def put(self, item):
        if self.error:
            raise RuntimeError(f'编码失败：{self.error}')
        try:
            self.enqueue_performance.call('encoder_enqueue_wait', self.queue.put_nowait, item)
            self.queue_peak = max(self.queue_peak, self.queue.qsize())
        except queue.Full:
            raise RuntimeError('编码队列已满，已停止本批录像，避免编码等待拖住取帧。请检查编码器、磁盘或降低帧率')

    def finish(self):
        try:
            deadline = time.monotonic() + 10
            while self.thread.is_alive() and not self.error:
                try:
                    self.queue.put(None, timeout=0.1)
                    break
                except queue.Full:
                    if time.monotonic() > deadline:
                        raise RuntimeError('编码队列无法排空')
            self.thread.join(timeout=35)
            if self.thread.is_alive():
                raise RuntimeError('编码器结束超时')
            if self.error:
                raise RuntimeError(str(self.error))
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self.thread.join(timeout=5)
            self.process.stdin.close()
            self.ledger.close()
            self.log.close()


class Recording:
    is_raw = False
    def __init__(self, folder, info, shape, fps, settings, demo=False, view=None):
        if min(shape) <= 0 or any(x % 2 for x in shape):
            raise ValueError('MP4 录像需要偶数宽高，请调整相机分辨率')
        self.output = Path(folder) / (safe_name(info['serial']) + '.mp4')
        self.shape, self.fps = shape, fps
        self.timeline = Timeline(fps, 2**63 - 1)
        self.encoder = Encoder(self.output, shape, fps, view=view)
        self.info = dict(serial=info['serial'], model=info['model'], simulated=demo,
                         width=shape[0], height=shape[1], target_fps=fps,
                         settings=vars(settings).copy(), errors=[], started_at=None,
                         timing='CFR from camera 100-us ticks; repeats explicitly logged',
                         color='SDK RGB8 ISP output; no additional color correction', encoder=self.encoder.codec_info)
        self.first_tick = self.previous_tick = None
        self.elapsed_ticks = self.source_count = 0
        self.closed = False
        self.audit = FrameAudit()
        self.sdk_start = None
        self.skipped = 0
        try:
            self.journal = FrameJournal(self.output.with_suffix('.received.jsonl'))
        except Exception:
            self.encoder.finish()
            raise
        self.info.update(schema='mvsdk-recording/2', mode='mp4_cfr', software=software_info(), metadata_units=METADATA_UNITS)

    def observe(self, metadata, selected=True):
        row = self.audit.observe(metadata)
        row['selected_for_mp4'] = selected
        self.skipped += int(not selected)
        self.journal.write(row)
        return row

    @property
    def duration(self):
        return self.encoder.written/self.fps

    def wants(self, tick):
        # Observe every acquired RAW frame; run ISP only for output or preview.
        if self.previous_tick is not None:
            delta = (tick - self.previous_tick) & 0xffffffff
            if delta == 0 or delta > 20000:
                raise RuntimeError('相机时间戳停滞或跳变，已停止录像')
            self.elapsed_ticks += delta
        self.previous_tick = tick
        self.source_count += 1
        return self.elapsed_ticks * self.fps // 10000 >= self.timeline.next_index

    def add(self, tick, shape, data):
        if shape != self.shape:
            raise RuntimeError('录像期间图像尺寸改变')
        if self.first_tick is None:
            self.first_tick = tick
            self.info['started_at'] = datetime.now().astimezone().isoformat()
        for item in self.timeline.add(tick, (data, tick, self.audit.count-1 if self.audit.count else None)):
            self.encoder.put(item)

    def finish(self, error=None, sdk_stats=None, snapshot_end=None):
        if self.closed:
            return self.info
        self.closed = True
        if error:
            self.info['errors'].append(str(error))
        try:
            self.encoder.finish()
        except Exception as exc:
            self.info['errors'].append(str(exc))
        try:
            self.journal.close()
        except Exception as exc:
            self.info['errors'].append(str(exc))
        if self.journal.written != self.audit.count:
            self.info['errors'].append(f'逐帧元数据保存不完整：{self.journal.written}/{self.audit.count}')
        self.info.update(ended_at=datetime.now().astimezone().isoformat(),
                         written_frames=self.encoder.written, repeated_frames=self.encoder.repeats,
                         video_seconds=self.encoder.written / self.fps,
                         source_frames_observed=self.source_count,
                         source_fps=((self.source_count-1)*10000/self.elapsed_ticks if self.elapsed_ticks else None),
                         sdk_counters=sdk_stats,
                         complete=not self.info['errors'] and self.encoder.written > 0,
                         all_frames_unique=self.encoder.repeats == 0,
                         video=str(self.output))
        self.info['encoder_performance'] = dict(writer=self.encoder.performance.summary(),
                                                producer=self.encoder.enqueue_performance.summary(),
                                                queue_peak=self.encoder.queue_peak, queue_capacity=self.encoder.queue.maxsize)
        if not self.encoder.written:
            self.info['errors'].append('尚未收到可录制的图像，未生成有效视频')
        self.info['snapshot_end'] = snapshot_end
        self.info['integrity'] = self.audit.report(self.sdk_start, sdk_stats, self.encoder.written,
                                                  self.skipped, self.encoder.repeats, self.info['errors'])
        write_report(self.output.with_suffix('.integrity.txt'), self.info)
        self.output.with_suffix('.json').write_text(json.dumps(self.info, ensure_ascii=False, indent=2), encoding='utf-8')
        return self.info


class CameraWorker(threading.Thread):
    def __init__(self, system, info, events):
        super().__init__(name='camera-'+info['serial'], daemon=True)
        self.system, self.info, self.events = system, info, events
        self.commands = queue.Queue()
        self.preview_processor = None
        self.performance = StageTimings()
        self.full_preview = threading.Event()
        self.session = None
        self.gate = None
        self.active = False
        self.shape = None
        self.pending_wb_request = None

    def emit(self, kind, **data):
        self.events.put((self.info['serial'], kind, data))

    def command(self, name, **data):
        self.commands.put((name, data))

    def take_preview(self):
        return self.preview_processor.take() if self.preview_processor else None

    def stop_record(self, error=None):
        if not self.session:
            self.emit('record_finished', summary=None)
            return
        session, self.session = self.session, None
        self.active = False
        self.camera.capture_metadata = self.camera.capture_raw = False
        session.info['capture_performance'] = dict(
            worker=self.performance.summary(),
            sdk=getattr(self.camera, 'performance', StageTimings()).summary(),
            note='Milliseconds of host elapsed time, not camera exposure time; nested stages overlap. Get-buffer includes waiting. Preview runs separately.')
        try:
            stats = self.camera.stats()
        except Exception:
            stats = None
        try:
            try:
                snapshot = self.camera.snapshot()
                end_config = session.output.parent/(safe_name(self.info['serial'])+'.end.config')
                self.camera.save_config(end_config)
            except Exception as exc:
                snapshot = dict(unavailable=str(exc))
            summary = session.finish(error, stats, snapshot)
        except Exception as exc:
            summary = dict(errors=[str(exc)], complete=False, video=str(session.output))
        self.emit('record_finished', summary=summary)

    def run(self):
        self.camera = None
        running = True
        last_frame = last_preview = last_report = time.monotonic()
        sampled = 0
        try:
            self.camera = self.system.open(self.info)
            self.camera.defer_orientation = True
            self.preview_processor = PreviewProcessor(self.info['serial'])
            self.emit('connected', settings=self.camera.settings(),
                      gain_range=(self.camera.gain_min, self.camera.gain_max))
            while running:
                while not self.commands.empty():
                    name, args = self.commands.get_nowait()
                    try:
                        if name == 'close':
                            running = False
                            self.stop_record()
                            break
                        if name == 'stop':
                            self.stop_record()
                        elif name == 'apply':
                            if self.session:
                                raise RuntimeError('录像期间不能修改参数')
                            actual = self.camera.apply(args['settings'])
                            self.emit('settings', settings=actual)
                        elif name == 'wb':
                            if self.session:
                                raise RuntimeError('请停止录像后调整白平衡')
                            self.camera.white_balance()
                            self.pending_wb_request = args.get('request')
                            self.emit('notice', text='已触发一次白平衡，请观察预览颜色')
                        elif name in ('advanced_get', 'advanced_apply'):
                            if self.session:
                                raise RuntimeError('采集期间高级参数已锁定，请先停止采集')
                            controls = (self.camera.apply_advanced(args['changes']) if name == 'advanced_apply'
                                        else self.camera.advanced())
                            self.emit('advanced', controls=controls, request=args.get('request'),
                                      applied=name == 'advanced_apply',
                                      readback_differences=[controls[k]['label'] for k, v in args.get('changes', {}).items()
                                                            if controls[k]['value'] != v])
                            self.emit('settings', settings=self.camera.settings())
                        elif name in ('save_preset', 'load_preset'):
                            if self.session:
                                raise RuntimeError('请停止采集后操作预设')
                            prefix = Path(args['folder'])/safe_name(self.info['serial'])
                            if name == 'save_preset':
                                actual = self.camera.apply(args['settings'])
                                self.camera.lock_automatic()
                                self.camera.save_config(prefix.with_suffix('.config'))
                                write_json(prefix.with_suffix('.preset.json'), dict(schema='mvsdk-preset/1', serial=self.info['serial'],
                                           simulated=isinstance(self.system, DemoSystem), settings=vars(actual),
                                           application_orientation=orientation(self.camera),
                                           snapshot=self.camera.snapshot(), created_at=utc_now()))
                                self.emit('notice', text=f'预设已保存：{prefix.parent}')
                            else:
                                description = json.loads(prefix.with_suffix('.preset.json').read_text(encoding='utf-8'))
                                if description['serial'] != self.info['serial'] or description['simulated'] != isinstance(self.system, DemoSystem):
                                    raise ValueError('预设的相机序列号或模拟/实机模式不匹配')
                                actual = self.camera.load_config(prefix.with_suffix('.config'))
                                actual = self.camera.apply(Settings(**description['settings']))
                                view = description.get('application_orientation', {})
                                self.camera.mirror_horizontal = bool(view.get('mirror_horizontal', False))
                                self.camera.mirror_vertical = bool(view.get('mirror_vertical', False))
                                self.emit('notice', text='预设已加载；连续采集和手动曝光/白平衡已锁定')
                            self.emit('settings', settings=actual)
                        elif name == 'prepare':
                            if self.session:
                                raise RuntimeError('相机正在录像')
                            if self.shape is None:
                                raise RuntimeError('尚未收到预览图像')
                            research = args.get('mode') == 'raw'
                            args['settings'].validate(None if research else args['fps'])
                            actual = self.camera.apply(args['settings'])
                            actual.validate(None if research else args['fps'])
                            self.camera.lock_automatic()
                            self.emit('settings', settings=actual)
                            self.camera.save_config(Path(args['folder']) / (safe_name(self.info['serial']) + '.config'))
                            snapshot = self.camera.snapshot()
                            if research:
                                self.session = ResearchRecording(args['folder'], safe_name(self.info['serial']), self.info, actual,
                                                                 args.get('experiment', {}), snapshot, isinstance(self.system, DemoSystem))
                            else:
                                self.session = Recording(args['folder'], self.info, self.shape, args['fps'], actual,
                                                         isinstance(self.system, DemoSystem), view=orientation(self.camera))
                                self.session.info.update(experiment=args.get('experiment', {}), snapshot_start=snapshot)
                                self.emit('notice', text='MP4 编码器：'+self.session.encoder.codec_info['codec'])
                            self.gate = args['gate']
                            self.active = False
                            self.emit('prepared')
                    except Exception as exc:
                        if name in ('advanced_get', 'advanced_apply') or (name == 'wb' and args.get('request')):
                            self.emit('advanced_failed', text=str(exc), request=args.get('request'))
                        self.emit('prepare_failed' if name == 'prepare' else 'command_error', text=str(exc))
                if not running:
                    break
                if self.session and not self.active and self.gate.is_set():
                    self.performance = StageTimings()
                    self.camera.performance = StageTimings()
                    try:
                        self.session.sdk_start = self.camera.stats()
                    except Exception:
                        self.session.sdk_start = None
                    self.active = True
                    sampled, last_report = 0, time.monotonic()
                    self.emit('record_started')
                self.camera.capture_metadata = self.active
                self.camera.capture_raw = self.active and self.session.is_raw
                need_preview = time.monotonic() - last_preview >= 0.1
                needed_record = False

                def wanted(tick):
                    nonlocal needed_record
                    if self.active:
                        needed_record = self.session.wants(tick)
                    return need_preview or (needed_record and not self.session.is_raw)

                frame = self.performance.call('camera_read_total', self.camera.read, wanted)
                now = time.monotonic()
                if frame is None:
                    if self.active:
                        self.session.audit.timeouts += 1
                    if now - last_frame > 3:
                        raise RuntimeError('连续 3 秒没有收到图像，请检查相机连接')
                    continue
                sampled += 1
                last_frame = now
                tick, shape, pixels = frame
                if self.active:
                    metadata = self.performance.call('metadata_audit_and_journal', self.session.observe, self.camera.frame_metadata, needed_record)
                    if self.session.is_raw:
                        self.performance.call('raw_enqueue', self.session.add_raw, self.camera.raw_bytes, metadata)
                if pixels is not None:
                    self.shape = shape
                    if self.pending_wb_request:
                        request, self.pending_wb_request = self.pending_wb_request, None
                        try:
                            self.emit('advanced', controls=self.camera.advanced(), request=request, applied=False)
                        except Exception as exc:
                            self.emit('advanced_failed', request=request, text=str(exc))
                    if needed_record and not self.session.is_raw:
                        self.performance.call('mp4_enqueue_total', self.session.add, tick, shape, pixels)
                    if need_preview:
                        view = dict(rotate180=self.camera.view_rotate180,
                                    mirror_horizontal=self.camera.mirror_horizontal,
                                    mirror_vertical=self.camera.mirror_vertical)
                        self.preview_processor.submit(pixels, shape, self.full_preview.is_set(), view)
                        last_preview = now
                if self.preview_processor.error:
                    self.emit('notice', text='预览处理失败：'+str(self.preview_processor.error))
                    self.preview_processor.close()
                    self.preview_processor = PreviewProcessor(self.info['serial'])
                if now - last_report >= 0.5:
                    self.emit('stats', fps=sampled/(now-last_report), shape=self.shape,
                              frames=self.session.encoder.written if self.session else 0,
                              repeats=self.session.encoder.repeats if self.session else 0,
                              seconds=self.session.duration if self.session else 0,
                              raw_bytes=getattr(self.camera, 'last_raw_size', None))
                    sampled, last_report = 0, now
        except Exception as exc:
            if self.pending_wb_request:
                self.emit('advanced_failed', request=self.pending_wb_request, text=str(exc))
            self.emit('fault', text=str(exc))
            if self.session:
                self.stop_record(exc)
        finally:
            if self.preview_processor:
                self.preview_processor.close()
            if self.camera:
                try:
                    self.camera.close()
                except Exception as exc:
                    self.emit('command_error', text=str(exc))
            self.emit('closed')


class MindVisionProcessWorker(SubprocessWorker):
    """CameraWorker in its own process (mv_worker.py), so cameras do not compete inside one interpreter."""
    settings_class = Settings
    label = 'MindVision '
    log_prefix = 'mindvision'

    def __init__(self, info, events, demo=False, log_folder=None):
        super().__init__(info, events, log_folder)
        self.demo = demo

    def command_line(self):
        python = Path(sys.executable)
        if python.name.lower() == 'pythonw.exe' and python.with_name('python.exe').is_file():
            python = python.with_name('python.exe')  # pipes need a console-subsystem interpreter
        return [str(python), '-B', str(Path(__file__).resolve().parent / 'mv_worker.py'), '--serial', self.info['serial']] + \
            (['--demo'] if self.demo else [])
