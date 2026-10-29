"""One MindVision camera per process. Runs the unchanged CameraWorker and bridges it to the recorder.

Started by multi_camera_core.MindVisionProcessWorker. Commands: JSON lines on stdin. Events and
previews: framed binary on stdout (see subprocess_worker). Each camera gets its own interpreter,
so ISP calls, pixel copies and encoder feeding no longer compete inside one process.
"""
# The MindVision SDK prints to stdout; keep the protocol on a private handle before importing it.
from subprocess_worker import boost_process, protect_stdout
PROTO = protect_stdout()
PROCESS_BOOST = boost_process()

import argparse  # noqa: E402
import os  # noqa: E402
import dataclasses  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402
import queue  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402

from multi_camera_core import CameraWorker, DemoSystem, SDKSystem, Settings  # noqa: E402
from research_data import clean_json  # noqa: E402
from subprocess_worker import pack_preview, write_message  # noqa: E402


class FaultInjectingDemo(DemoSystem):
    """Simulated cameras whose stream can be broken on request (offline tests only)."""
    fail = threading.Event()

    def open(self, info):
        camera = super().open(info)
        read = camera.read

        def failing_read(wanted):
            if self.fail.is_set():
                raise OSError('injected camera disconnection')
            return read(wanted)
        camera.read = failing_read
        return camera


def to_json(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return str(value)


class Bridge:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.gate = None

    def emit(self, event, **data):
        payload = json.dumps(clean_json(dict(event=event, **data)), ensure_ascii=False, default=to_json)
        write_message(PROTO, b'J', payload.encode('utf-8'), self.lock)

    def handle(self, command):
        name = command.pop('command')
        if name == 'start':
            if self.gate is not None:
                self.gate.set()
            return
        if name == 'full_preview':
            (self.worker.full_preview.set if command.get('enabled') else self.worker.full_preview.clear)()
            return
        if name == 'inject_fault':
            if self.args.demo:
                self.system.fail.set()
            return
        if isinstance(command.get('settings'), dict):
            command['settings'] = Settings(**command['settings'])
        if 'folder' in command:
            command['folder'] = Path(command['folder'])
        if name == 'prepare':
            self.gate = command['gate'] = threading.Event()
        self.worker.command(name, **command)

    def stdin_reader(self):
        try:
            for line in sys.stdin.buffer:
                if line.strip():
                    try:
                        self.handle(json.loads(line))
                    except Exception as exc:
                        self.emit('command_error', text=f'命令处理失败：{exc}')
        finally:
            # The recorder went away: finish any recording and release the camera.
            self.worker.command('close')

    def preview_sender(self):
        while self.worker.is_alive():
            image = self.worker.take_preview()
            if image is None:
                time.sleep(0.01)
                continue
            full = self.worker.shape is not None and image.size == tuple(self.worker.shape)
            buffer = io.BytesIO()
            image.save(buffer, 'JPEG', quality=92 if full else 80)
            write_message(PROTO, b'P', pack_preview(dict(width=image.width, height=image.height, full=full), buffer.getvalue()), self.lock)

    def run(self):
        try:
            self.system = FaultInjectingDemo() if self.args.demo else SDKSystem()
            device = next((d for d in self.system.scan() if d['serial'] == self.args.serial), None)
            if device is None:
                raise RuntimeError(f'未找到相机 {self.args.serial}，请检查连接')
        except Exception as exc:
            traceback.print_exc()
            self.emit('fault', text=str(exc))
            self.emit('closed')
            return
        events = queue.Queue()
        self.emit('notice', text=f'{self.args.serial} 独立进程已启动（{"高优先级" if PROCESS_BOOST.get("priority_above_normal") else "普通优先级"}，'
                                 f'{"已关闭" if PROCESS_BOOST.get("power_throttling_disabled") else "未能关闭"}节能降速）')
        self.worker = CameraWorker(self.system, device, events)
        self.worker.start()
        threading.Thread(target=self.stdin_reader, daemon=True, name='mv-stdin').start()
        threading.Thread(target=self.preview_sender, daemon=True, name='mv-preview').start()
        while True:
            _, kind, data = events.get()
            self.emit(kind, **data)
            if kind == 'closed':
                break
        self.worker.join(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--demo', action='store_true', help='simulated camera from DemoSystem')
    Bridge(parser.parse_args()).run()
    # Everything is closed and flushed. Skip interpreter teardown: daemon threads inside native
    # code (SDK, Pillow) can crash it on Windows after the work is already done.
    sys.stderr.flush()
    os._exit(0)


if __name__ == '__main__':
    main()
