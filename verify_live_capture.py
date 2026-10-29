"""Explicit short hardware acceptance test; restores camera parameters on close."""
import json
from pathlib import Path
import queue
import threading
import time
from multi_camera_core import SDKSystem, CameraWorker, Settings
from research_data import utc_now


def main():
    system = SDKSystem()
    devices = [d for d in system.scan() if d['serial'] == '046011323028']
    if len(devices) != 1:
        raise RuntimeError('Expected camera not found')
    if system.get(system.sdk.CameraIsOpened, devices[0]['native']):
        raise RuntimeError('Camera is still occupied; no attempt to disconnect it')
    directory = Path(__file__).resolve().parent.parent/'audit'/('live-encoding-'+time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir()
    events = queue.Queue()
    worker = CameraWorker(system, devices[0], events)
    def wait(kind, timeout=15):
        until = time.monotonic()+timeout
        while time.monotonic() < until:
            serial, event, data = events.get(timeout=max(0.01, until-time.monotonic()))
            if event in ('fault', 'command_error', 'prepare_failed'):
                raise RuntimeError(str(data))
            if event == kind:
                return data
        raise TimeoutError(kind)
    results = []
    try:
        worker.start()
        initial = wait('connected')['settings']
        wait('stats')
        worker.full_preview.set()
        for fps, seconds in ((30, 5), (60, 10)):
            folder = directory/str(fps)
            folder.mkdir()
            gate = threading.Event()
            worker.command('prepare', folder=folder, fps=fps, settings=Settings(13, initial.gain, True), gate=gate,
                           experiment=dict(experiment_id='encoding acceptance', notes='Short test requested after releasing camera'))
            wait('prepared')
            gate.set()
            wait('record_started')
            until = time.monotonic()+seconds
            while time.monotonic() < until:
                time.sleep(0.1)
                worker.take_preview()
                if worker.session is None or not worker.is_alive():
                    raise RuntimeError('Capture stopped before test duration')
            worker.command('stop')
            info = wait('record_finished', 45)['summary']
            results.append(info)
            print(json.dumps({k:info.get(k) for k in ('target_fps','source_fps','written_frames','repeated_frames','complete','encoder','capture_performance','encoder_performance','errors','video')},ensure_ascii=True), flush=True)
    finally:
        worker.command('close')
        worker.join(50)
        if worker.is_alive():
            raise RuntimeError('Camera worker did not close')
        (directory/'verification.json').write_text(json.dumps(dict(date=utc_now(),results=results),ensure_ascii=False,indent=2),encoding='utf-8')
        print('Saved:',directory,flush=True)


if __name__ == '__main__':
    main()
