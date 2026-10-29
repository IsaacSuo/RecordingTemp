"""One camera per process: framed stdout protocol, worker-side stdout protection, and the app-side worker thread.

Each camera process receives JSON-line commands on stdin and writes framed messages on stdout:
1-byte kind (J = JSON event, P = preview), uint32 length, payload. Vendor SDKs print to stdout,
so worker processes call protect_stdout() before importing them.
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------- protocol

def write_message(stream, kind, payload, lock=None):
    data = struct.pack('<cI', kind, len(payload)) + payload
    if lock:
        with lock:
            stream.write(data)
            stream.flush()
    else:
        stream.write(data)
        stream.flush()


def read_exact(stream, size):
    chunks, remaining = [], size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def read_message(stream):
    header = read_exact(stream, 5)
    if header is None:
        return None
    kind, length = struct.unpack('<cI', header)
    payload = read_exact(stream, length)
    if payload is None:
        return None
    return kind, payload


def pack_preview(meta, jpeg):
    head = json.dumps(meta).encode()
    return struct.pack('<H', len(head)) + head + jpeg


def unpack_preview(payload):
    (size,) = struct.unpack_from('<H', payload)
    return json.loads(payload[2:2+size]), payload[2+size:]


def protect_stdout():
    """Keep a private binary handle for the protocol and send every later stdout writer, native ones included, to stderr."""
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    if os.name == 'nt':
        import ctypes
        import msvcrt
        ctypes.windll.kernel32.SetStdHandle(-11, msvcrt.get_osfhandle(2))
    sys.stdout = sys.stderr
    return os.fdopen(proto_fd, 'wb', buffering=0)


def kill_tree(process):
    """End a worker and everything it started. A venv python.exe is only a launcher for the real interpreter,
    so killing the launcher alone would leave the camera process (and the camera) held."""
    if process is None or process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(process.pid)], capture_output=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if process.poll() is None:
        process.kill()


# ---------------------------------------------------------------- app side

class SubprocessWorker(threading.Thread):
    """Drop-in counterpart of CameraWorker: same command(), take_preview(), full_preview, shape and events."""
    settings_class = None
    label = '相机'

    def __init__(self, info, events, log_folder=None):
        super().__init__(name='camera-process-' + info['serial'], daemon=True)
        self.info, self.events = info, events
        self.commands = queue.Queue()
        self.full_preview = threading.Event()
        self.shape = None
        self.session = None
        self.process = None
        self._preview = None
        self._preview_lock = threading.Lock()
        self._gate = None
        self._closed_emitted = False
        self.log_path = Path(log_folder or (ROOT / 'logs')) / f'{self.log_prefix}-{info["serial"]}-{time.strftime("%Y%m%d-%H%M%S")}.log'

    log_prefix = 'camera'

    def command_line(self):
        raise NotImplementedError

    def emit(self, kind, **data):
        self.events.put((self.info['serial'], kind, data))

    def command(self, name, **data):
        self.commands.put((name, data))

    def terminate(self):
        """Force-end the camera process tree (used for cameras stuck inside a vendor SDK call)."""
        kill_tree(self.process)

    def take_preview(self):
        with self._preview_lock:
            image, self._preview = self._preview, None
        return image

    def _send(self, name, **data):
        line = json.dumps(dict(command=name, **data), ensure_ascii=False, default=str) + '\n'
        self.process.stdin.write(line.encode('utf-8'))
        self.process.stdin.flush()

    def encode(self, data):
        payload = {}
        for key, value in data.items():
            if dataclasses.is_dataclass(value):
                value = dataclasses.asdict(value)
            elif isinstance(value, Path):
                value = str(value)
            payload[key] = value
        return payload

    def translate(self, name, event):
        if name in ('connected', 'settings') and 'settings' in event and self.settings_class:
            event['settings'] = self.settings_class(**event['settings'])
        if 'shape' in event and event['shape'] is not None:
            event['shape'] = tuple(event['shape'])
            self.shape = event['shape']
        if 'gain_range' in event:
            event['gain_range'] = tuple(event['gain_range'])
        return event

    def _reader(self):
        from PIL import Image
        stream = self.process.stdout
        while True:
            message = read_message(stream)
            if message is None:
                return
            kind, payload = message
            if kind == b'P':
                _, jpeg = unpack_preview(payload)
                try:
                    image = Image.open(io.BytesIO(jpeg))
                    image.load()
                except Exception:
                    continue
                with self._preview_lock:
                    self._preview = image
                continue
            event = json.loads(payload)
            name = event.pop('event')
            event = self.translate(name, event)
            if name == 'record_started':
                self.session = True
            elif name == 'record_finished':
                self.session = None
            elif name == 'closed':
                self._closed_emitted = True
            self.emit(name, **event)

    def run(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open('ab')
        reader = None
        try:
            self.process = subprocess.Popen(self.command_line(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                            cwd=str(ROOT), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            reader = threading.Thread(target=self._reader, name='reader-' + self.info['serial'], daemon=True)
            reader.start()
            full = False
            while self.process.poll() is None:
                try:
                    name, data = self.commands.get(timeout=0.02)
                except queue.Empty:
                    name = None
                if name == 'prepare':
                    data = dict(data)
                    self._gate = data.pop('gate')
                if name is not None:
                    self._send(name, **self.encode(data))
                if self._gate is not None and self._gate.is_set():
                    self._gate = None
                    self._send('start')
                if self.full_preview.is_set() != full:
                    full = self.full_preview.is_set()
                    self._send('full_preview', enabled=full)
            if reader:
                reader.join(timeout=5)
            if self.process.returncode not in (0, None) and not self._closed_emitted:
                self.emit('fault', text=f'{self.label}进程异常退出（代码 {self.process.returncode}），详见 {self.log_path}')
        except Exception as exc:
            self.emit('fault', text=f'{self.label}进程通信失败：{exc}')
            if self.process and self.process.poll() is None:
                try:
                    self._send('close')
                    self.process.wait(timeout=30)
                except Exception:
                    kill_tree(self.process)
        finally:
            kill_tree(self.process)
            log.close()
            if self.session:
                self.session = None
                self.emit('record_finished', summary=dict(complete=False, errors=[f'{self.label}进程意外结束，录像可能不完整']))
            if not self._closed_emitted:
                self.emit('closed')


class OpenLock:
    """System-wide named mutex. ZED cameras must be opened one at a time: when several processes call
    Camera.open() together on Windows, the SDK can hang in open() indefinitely (seen with 3 ZED 2i)."""

    def __init__(self, name='Local\\multi-camera-recorder-zed-open', timeout_s=180):
        self.name, self.timeout_s = name, timeout_s
        self.handle = None
        self.waited_s = 0.0

    def __enter__(self):
        if os.name != 'nt':
            return self
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32 = kernel32
        self.handle = kernel32.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), 'CreateMutexW failed')
        started = time.monotonic()
        result = kernel32.WaitForSingleObject(self.handle, int(self.timeout_s * 1000))
        self.waited_s = time.monotonic() - started
        if result not in (0, 0x80):  # WAIT_OBJECT_0, or WAIT_ABANDONED: a previous holder died; we own it now
            kernel32.CloseHandle(self.handle)
            self.handle = None
            raise TimeoutError(f'等待其他 ZED 打开超过 {self.timeout_s:.0f} 秒；请检查是否有 ZED 进程卡住（可关闭录像台后重开）')
        return self

    def __exit__(self, *exc):
        if self.handle:
            from ctypes import wintypes
            self.kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
            self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel32.ReleaseMutex(self.handle)
            self.kernel32.CloseHandle(self.handle)
            self.handle = None
        return False


def boost_process():
    """Ask Windows for full speed: above-normal priority and no EcoQoS power throttling.

    Camera processes have no window, so on hybrid CPUs Windows may treat them as background
    work and run them on efficiency cores; the MindVision ISP then takes about twice as long.
    Returns what was applied, for the recording metadata.
    """
    if os.name != 'nt':
        return {}
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    process = kernel32.GetCurrentProcess()
    result = {}
    result['priority_above_normal'] = bool(kernel32.SetPriorityClass(process, 0x00008000))

    class PowerThrottlingState(ctypes.Structure):
        _fields_ = [('Version', wintypes.ULONG), ('ControlMask', wintypes.ULONG), ('StateMask', wintypes.ULONG)]

    # ProcessPowerThrottling = 4; control EXECUTION_SPEED (1) with state 0 = never throttle.
    state = PowerThrottlingState(1, 1, 0)
    kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    result['power_throttling_disabled'] = bool(kernel32.SetProcessInformation(process, 4, ctypes.byref(state), ctypes.sizeof(state)))
    return result
