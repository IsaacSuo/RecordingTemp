"""Explicit hardware acceptance: every free MindVision (MP4) and ZED (SVO2) recorded together through the recorder code.

Requires both cameras to be free (recorder and ZED Explorer closed). Restores camera parameters on close.
Usage: verify_zed_live.py [seconds] [mp4 fps] [zed compression] [inprocess]
MindVision cameras run in their own processes unless "inprocess" is given (old architecture, for comparison).
"""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from multi_camera_core import SDKSystem, CameraWorker, MindVisionProcessWorker, Settings
from research_data import utc_now
from zed_support import DEFAULT_MODE, ZedWorker, ZedSettings, ZED_PYTHON, WORKER_SCRIPT, list_devices


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30
    fps = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    compression = sys.argv[3] if len(sys.argv) > 3 else 'H264'
    inprocess = len(sys.argv) > 4 and sys.argv[4] == 'inprocess'
    folder = Path(__file__).resolve().parent.parent / 'audit' / ('live-zed-' + time.strftime('%Y%m%d-%H%M%S'))
    folder.mkdir()
    events = queue.Queue()
    system = SDKSystem()
    workers = {}
    for device in system.scan():
        if system.get(system.sdk.CameraIsOpened, device['native']):
            raise RuntimeError(f'MindVision {device["serial"]} is occupied; not touching it')
        workers[device['serial']] = (CameraWorker(system, device, events) if inprocess else
                                     MindVisionProcessWorker(device, events, log_folder=folder))
    zed_devices, note = list_devices()
    for device in zed_devices:
        zed_info = dict(device, mode=DEFAULT_MODE)
        workers[zed_info['serial']] = ZedWorker(zed_info, events, log_folder=folder)
    if not workers:
        raise RuntimeError(note or 'No camera found')
    print('cameras:', ', '.join(workers), flush=True)

    def wait(kind, serials, timeout=40):
        pending, data = set(serials), {}
        until = time.monotonic() + timeout
        while pending and time.monotonic() < until:
            try:
                serial, event, payload = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if event in ('fault', 'prepare_failed'):
                raise RuntimeError(f'{serial}: {payload}')
            if event == 'command_error':
                print('command_error', serial, payload, flush=True)
            if event == kind and serial in pending:
                pending.discard(serial)
                data[serial] = payload
        if pending:
            raise TimeoutError(f'{kind}: {pending}')
        return data

    results = {}
    try:
        for worker in workers.values():
            worker.start()
        connected = wait('connected', workers)
        wait('stats', workers)
        gate = threading.Event()
        for serial, worker in workers.items():
            if isinstance(worker, ZedWorker):
                worker.command('prepare', folder=folder, settings=ZedSettings(), gate=gate, mode='mp4',
                               experiment=dict(experiment_id='zed-live-acceptance'), compression=compression)
            else:
                worker.command('prepare', folder=folder, fps=fps, gate=gate, mode='mp4',
                               settings=Settings(13, connected[serial]['settings'].gain, True),
                               experiment=dict(experiment_id='zed-live-acceptance'))
        wait('prepared', workers)
        gate.set()
        wait('record_started', workers)
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            time.sleep(0.1)
            for worker in workers.values():
                worker.take_preview()
        for worker in workers.values():
            worker.command('stop')
        results = {serial: data['summary'] for serial, data in wait('record_finished', workers, 90).items()}
    finally:
        for worker in workers.values():
            worker.command('close')
        for worker in workers.values():
            worker.join(60)
    report = dict(date=utc_now(), seconds=seconds, mp4_fps=fps, zed_compression=compression, folder=str(folder),
                  mindvision_architecture='in-process threads' if inprocess else 'one process per camera')
    for serial, summary in results.items():
        if summary.get('kind') == 'zed':
            check = subprocess.run([str(ZED_PYTHON), '-B', str(WORKER_SCRIPT), '--verify-svo', summary['video']],
                                   capture_output=True, timeout=600)
            summary['full_svo_verification'] = json.loads(check.stdout.decode('utf-8').strip().splitlines()[-1])
            integrity = summary['integrity']
            report[serial] = dict(complete=summary['complete'], status=integrity['status'], frames=summary['written_frames'],
                                  camera_missing=integrity['camera_missing_frames'], app_missing=integrity['app_missing_frames'],
                                  app_late=integrity['app_late_count'], duplicates=integrity['duplicates'],
                                  sdk_dropped=integrity['sdk_dropped_increments'], measured_fps=integrity['measured_fps'],
                                  svo_light=integrity['svo_check'], svo_full=summary['full_svo_verification'],
                                  locked=summary['locked_from_auto'], errors=summary['errors'], warnings=integrity['warnings'])
        else:
            report[serial] = dict(complete=summary['complete'], source_fps=summary['source_fps'], written=summary['written_frames'],
                                  repeated=summary['repeated_frames'], status=summary['integrity']['status'],
                                  encoder=summary['encoder']['codec'], queue_peak=summary['encoder_performance']['queue_peak'],
                                  errors=summary['errors'])
    (folder / 'verification.json').write_text(json.dumps(dict(report=report, summaries=results), ensure_ascii=False, indent=2),
                                              encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
