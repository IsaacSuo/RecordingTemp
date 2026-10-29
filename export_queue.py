"""Background ZED exports for the recorder: one zed_export.py process at a time, paused while recording."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time

from subprocess_worker import kill_tree  # also stops the ffmpeg encoders, which would keep partial files open
from zed_support import WORKER_SCRIPT, worker_python

EXPORT_SCRIPT = Path(__file__).resolve().parent / 'zed_export.py'
# Sidebar label -> depth mode passed to zed_export.py (None = rectified left/right only).
EXPORT_CHOICES = {
    '左右目视频（不算深度）': None,
    '左右目 + 深度 NEURAL（效果最好，较慢）': 'NEURAL',
    '左右目 + 深度 NEURAL_LIGHT（较快）': 'NEURAL_LIGHT',
}
DEFAULT_EXPORT = '左右目视频（不算深度）'


BATCH_LOCK = threading.Lock()  # concurrent exports of one batch update the same batch.json


def update_batch_exports(batch_folder, serial, info):
    """Record an export result in <batch>/batch.json without touching the recording summaries."""
    target = Path(batch_folder) / 'batch.json'
    try:
        data = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    data.setdefault('exports', {})[serial] = info
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(target)


def completed_export(job, result):
    """The result of an export that finished before a pause stopped it, else None."""
    if result and result.get('status') == 'completed':
        return result
    folder = Path(job['svo']).parent / ('export' + (f'_{job["depth"]}' if job.get('depth') else ''))
    try:  # killed after export.partial was renamed but before it reported
        summary = json.loads((folder / 'export.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return dict(summary, folder=str(folder)) if summary.get('status') == 'completed' else None


class ExportQueue:
    """Up to `parallel` exports at once (measured: three 1080p exports each still run faster than real time)."""

    def __init__(self, events, demo=False, log_folder=None, parallel=3):
        self.events, self.demo, self.parallel = events, demo, parallel
        self.log_folder = Path(log_folder or (WORKER_SCRIPT.parent / 'logs'))
        self.jobs = []
        self.running = {}  # job key -> (job, process or None)
        self.paused = False
        self.lock = threading.Lock()
        self.counter = 0

    @property
    def busy(self):
        return bool(self.running)

    def pending(self):
        return len(self.jobs) + len(self.running)

    def add(self, job):
        """job: serial, name, svo, depth (None / NEURAL / NEURAL_LIGHT), batch_folder, simulated."""
        with self.lock:
            self.counter += 1
            job['key'] = self.counter
            self.jobs.append(job)
        self._start_next()

    def pause(self):
        """Stop running exports (they restart from the beginning later) so recording gets the GPU."""
        with self.lock:
            self.paused = True
            stopped = list(self.running.values())
            for job, _ in reversed(stopped):
                job['interrupted'] = True
                self.jobs.insert(0, job)
        for _, process in stopped:
            kill_tree(process)
        return bool(stopped)

    def resume(self):
        with self.lock:
            self.paused = False
        self._start_next()

    def close(self):
        with self.lock:
            self.paused = True
            self.jobs.clear()
            stopped = list(self.running.values())
        for _, process in stopped:
            kill_tree(process)

    def _start_next(self):
        started = []
        with self.lock:
            while not self.paused and self.jobs and len(self.running) < self.parallel:
                job = self.jobs.pop(0)
                job.pop('interrupted', None)
                self.running[job['key']] = (job, None)
                started.append(job)
        for job in started:
            threading.Thread(target=self._run, args=(job,), daemon=True, name='zed-export-' + job['serial']).start()

    def _emit(self, kind, job, **data):
        self.events.put(('', kind, dict(job=dict(job), **data)))

    def _run(self, job):
        args = [worker_python(job.get('simulated')), '-B', str(EXPORT_SCRIPT), str(job['svo'])]
        if job.get('depth'):
            args += ['--depth', job['depth']]
        if job.get('simulated'):
            args.append('--simulate')
        self.log_folder.mkdir(parents=True, exist_ok=True)
        log_path = self.log_folder / f'export-{job["serial"]}-{time.strftime("%Y%m%d-%H%M%S")}-{job["key"]}.log'
        result = None
        with log_path.open('ab') as log:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, cwd=str(EXPORT_SCRIPT.parent),
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            with self.lock:
                if job.get('interrupted'):  # paused between scheduling and start
                    kill_tree(process)
                self.running[job['key']] = (job, process)
            self._emit('export_progress', job, done=0, total=None)
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get('event') == 'progress':
                    self._emit('export_progress', job, done=message.get('done'), total=message.get('total'))
                elif message.get('event') == 'finished':
                    result = message
            process.stdout.close()
            process.wait()
        with self.lock:
            interrupted = job.get('interrupted')
            done = completed_export(job, result) if interrupted else None
            if done:  # finished just before the pause reached it: keep the result instead of exporting again
                job.pop('interrupted', None)
                self.jobs = [j for j in self.jobs if j is not job]
                result, interrupted = done, False
            elif interrupted:
                self.running.pop(job['key'], None)
        if interrupted:
            self._emit('export_paused', job)
            return
        if result is None:
            result = dict(status='failed', error=f'导出进程退出（代码 {process.returncode}），详见 {log_path}')
        result.pop('event', None)
        result['log'] = str(log_path)
        with BATCH_LOCK:
            update_batch_exports(job['batch_folder'], job['serial'], dict(depth=job.get('depth') or 'none', **{
                k: result.get(k) for k in ('status', 'folder', 'source_frames', 'output_frames', 'repeated_frames', 'output_fps', 'error', 'log')}))
        with self.lock:  # only now idle: batch.json already holds the result
            self.running.pop(job['key'], None)
        self._emit('export_finished', job, result=result)
        self._start_next()
