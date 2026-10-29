"""Verified encoder selection and asynchronous per-frame metadata output."""
import json
import queue
import subprocess
import threading
import time

_lock = threading.Lock()
_probes = {}


def encoder_options(ffmpeg):
    with _lock:
        if ffmpeg not in _probes:
            try:
                # -nostdin / DEVNULL: inside a camera process stdin is the recorder's command pipe; an
                # inheriting ffmpeg would wait on it (probe timeout) or swallow commands.
                result = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=320x240:rate=60',
                                         '-frames:v', '1', '-c:v', 'h264_nvenc', '-preset', 'p4', '-f', 'null', '-'],
                                        stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                _probes[ffmpeg] = (result.returncode == 0, result.stderr.decode(errors='replace')[-2000:])
            except Exception as exc:
                _probes[ffmpeg] = (False, str(exc))
        supported, reason = _probes[ffmpeg]
    if supported:
        return (['-c:v', 'h264_nvenc', '-preset', 'p4', '-tune', 'll', '-rc', 'vbr', '-cq', '18', '-b:v', '0', '-bf', '0'],
                dict(codec='h264_nvenc', preset='p4', quality='VBR CQ 18', hardware=True))
    return (['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18', '-threads', '4'],
            dict(codec='libx264', preset='ultrafast', quality='CRF 18', hardware=False, hardware_probe_error=reason))


def orientation_filters(view):
    if not view:
        return []
    filters = []
    if bool(view.get('rotate180')) ^ bool(view.get('mirror_horizontal')):
        filters.append('hflip')
    if bool(view.get('rotate180')) ^ bool(view.get('mirror_vertical')):
        filters.append('vflip')
    return ['-vf', ','.join(filters)] if filters else []


class FrameJournal:
    def __init__(self, path):
        self.stream = path.open('x', encoding='utf-8')
        self.queue = queue.Queue(maxsize=1024)
        self.error = None
        self.written = 0
        self.thread = threading.Thread(target=self.run, name='metadata-'+path.stem, daemon=True)
        self.thread.start()

    def write(self, row):
        if self.error:
            raise RuntimeError(f'逐帧元数据写入失败：{self.error}')
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            raise RuntimeError('逐帧元数据写入持续跟不上，已停止采集')

    def run(self):
        try:
            with self.stream:
                while True:
                    row = self.queue.get()
                    if row is None:
                        break
                    self.stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
                    self.written += 1
                    if self.written % 50 == 0:
                        self.stream.flush()
        except Exception as exc:
            self.error = exc

    def close(self):
        deadline = time.monotonic()+10
        while self.thread.is_alive() and not self.error:
            try:
                self.queue.put(None, timeout=0.1)
                break
            except queue.Full:
                if time.monotonic() > deadline:
                    raise RuntimeError('逐帧元数据队列无法排空')
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError('逐帧元数据保存超时')
        if self.error:
            raise RuntimeError(f'逐帧元数据写入失败：{self.error}')
