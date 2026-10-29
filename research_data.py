"""Versioned RAW datasets, frame provenance and recording integrity reports.

RAW segments contain exactly the SDK buffer bytes, without an invented pixel
layout. frames.jsonl gives byte offsets, lengths and the original frame header.
"""
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import queue
import shutil
import threading
import time

APP_VERSION = '2.4-zed'
METADATA_UNITS = dict(camera_tick_100us='100 microseconds per tick; uint32 wraps',
                      camera_timestamp_us='microseconds from optional SDK API; validity depends on device',
                      camera_frame_id='optional SDK frame ID, not an application sequence',
                      host_receive_monotonic_ns='host monotonic nanoseconds; not exposure time',
                      host_receive_unix_ns='host Unix nanoseconds at receive; clock can be adjusted',
                      raw_header=dict(uiExpTime='microseconds', fAnalogGain='gain multiplier reported by SDK',
                                      iGamma='SDK integer; -1 means not applicable',
                                      iContrast='SDK integer; -1 means not applicable',
                                      fRgain='digital gain multiplier', fGgain='digital gain multiplier',
                                      fBgain='digital gain multiplier', uBytes='byte count', uiMediaType='SDK pixel-format code'))


def utc_now():
    return datetime.now().astimezone().isoformat()


def clean_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    return value


def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name+'.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(clean_json(value), stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


_SOURCE_ROOT = Path(__file__).resolve().parent
_STARTUP_HASHES = {name: file_hash(_SOURCE_ROOT/name) for name in
                  ('multi_camera_core.py', 'multi_camera_app.py', 'research_data.py', 'camera_controls.py',
                   'advanced_panel.py', 'focus_preview.py', 'capture_performance.py', 'record_video.py', 'encoding_support.py',
                   'zed_support.py', 'zed_worker.py', 'mv_worker.py', 'subprocess_worker.py', 'zed_export.py', 'export_queue.py') if (_SOURCE_ROOT/name).is_file()}


def software_info():
    return dict(app_version=APP_VERSION, python=platform.python_version(), platform=platform.platform(),
                source_sha256=_STARTUP_HASHES.copy(), source_hash_scope='process startup files')


class FrameAudit:
    def __init__(self):
        self.count = 0
        self.previous_tick = self.previous_id = None
        self.elapsed = 0
        self.clock_valid = True
        self.interval_min = self.interval_max = None
        self.interval_sum = self.interval_square_sum = self.interval_count = 0
        self.nonforward_timestamps = self.frame_id_gaps = self.frame_id_reversals = 0
        self.timeouts = 0
        self.first_host_ns = self.last_host_ns = None

    def observe(self, metadata):
        row = clean_json(dict(metadata))
        row['receive_index'] = self.count
        self.count += 1
        tick = row['camera_tick_100us']
        interval = None
        if self.previous_tick is not None:
            delta = (tick-self.previous_tick) & 0xffffffff
            if delta == 0 or delta >= 0x80000000:
                self.nonforward_timestamps += 1
                self.clock_valid = False
            else:
                interval = delta*100_000
                self.elapsed += delta
                self.interval_count += 1
                self.interval_sum += interval
                self.interval_square_sum += interval*interval
                self.interval_min = interval if self.interval_min is None else min(self.interval_min, interval)
                self.interval_max = interval if self.interval_max is None else max(self.interval_max, interval)
        self.previous_tick = tick
        row['camera_relative_ns'] = self.elapsed*100_000 if self.clock_valid else None
        row['camera_interval_ns'] = interval
        frame_id = row.get('camera_frame_id')
        if frame_id is not None and self.previous_id is not None:
            delta = (frame_id-self.previous_id) & 0xffffffff
            if 1 < delta < 0x80000000:
                self.frame_id_gaps += delta-1
            elif delta != 1:
                self.frame_id_reversals += 1
        if frame_id is not None:
            self.previous_id = frame_id
        if self.first_host_ns is None:
            self.first_host_ns = row.get('host_receive_monotonic_ns')
        self.last_host_ns = row.get('host_receive_monotonic_ns')
        return row

    def report(self, start, end, written, skipped=0, repeats=0, errors=None):
        delta = None
        if start is not None and end is not None:
            candidate = {key: end[key]-start[key] for key in ('total', 'captured', 'lost')}
            if all(value >= 0 for value in candidate.values()):
                delta = candidate
        mean = self.interval_sum/self.interval_count if self.interval_count else None
        std = math.sqrt(max(0, self.interval_square_sum/self.interval_count-mean*mean)) if mean else None
        missing = max(0, self.count-skipped-(written-repeats))
        warnings = []
        if delta is None:
            warnings.append('SDK 帧统计差值不可用或发生复位；不能证明传输完整性')
        elif delta['lost']:
            warnings.append(f'SDK 在采集期间报告丢帧 {delta["lost"]}')
        if self.frame_id_gaps or self.frame_id_reversals:
            warnings.append('硬件帧 ID 存在不连续，需要结合该型号帧 ID 语义核查')
        if self.nonforward_timestamps:
            warnings.append('相机时间戳不递增，派生相对时间不再有效')
        if repeats:
            warnings.append(f'MP4 补入 {repeats} 帧重复画面')
        if missing:
            warnings.append(f'应用接收但未写入且非主动选帧跳过的帧：{missing}')
        failed = bool(errors) or missing > 0
        return dict(status='failed' if failed else ('review' if warnings else 'pass_observed'),
                    meaning='仅核对观测到的计数；不是硬件零丢帧或跨相机同步证明',
                    received_frames=self.count, intentionally_skipped_frames=skipped,
                    written_frames=written, repeated_frames=repeats, unwritten_received_frames=missing,
                    sdk_start=start, sdk_end=end, sdk_delta=delta,
                    camera_frame_id_gap_count=self.frame_id_gaps,
                    camera_frame_id_nonforward_count=self.frame_id_reversals,
                    nonforward_timestamps=self.nonforward_timestamps, receive_timeouts=self.timeouts,
                    interval_ns=dict(min=self.interval_min, max=self.interval_max, mean=mean, std=std),
                    source_fps=((self.count-1)*10000/self.elapsed if self.elapsed and self.clock_valid else None),
                    warnings=warnings)


def write_report(path, info):
    report = info['integrity']
    rows = ['采集完整性报告', f'相机：{info["serial"]}', f'模式：{info["mode"]}',
            f'结果：{report["status"]}', report['meaning'],
            f'应用接收：{report["received_frames"]} 帧',
            f'主动选帧跳过：{report["intentionally_skipped_frames"]} 帧',
            f'写入：{report["written_frames"]} 帧；重复补帧：{report["repeated_frames"]} 帧',
            f'未写入接收帧：{report["unwritten_received_frames"]}',
            f'SDK 起止差值：{report["sdk_delta"]}',
            '注意：主机接收时间不是曝光时间；不同相机时间戳未经同步。']
    rows += ['注意：'+message for message in report['warnings']]
    rows += ['错误：'+message for message in info.get('errors', [])]
    Path(path).write_text('\n'.join(rows)+'\n', encoding='utf-8')


class RawWriter:
    """Bounded, segmented, byte-exact writer. Index entries follow pixel writes."""
    def __init__(self, folder, segment_limit=256*1024**2):
        self.folder = Path(folder)
        self.folder.mkdir(exist_ok=False)
        self.segment_limit = segment_limit
        self.queue = queue.Queue(maxsize=8)
        self.error = None
        self.written = self.repeats = self.bytes_written = 0
        self.segments = []
        self.index_hash = None
        self.queue_peak = 0
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._run, daemon=True, name='RAW-writer')
        self.thread.start()

    def _run(self):
        segment = None
        index = None
        current = None
        digest = None
        index_digest = hashlib.sha256()

        def close_segment():
            nonlocal segment
            if segment:
                segment.flush()
                os.fsync(segment.fileno())
                segment.close()
                segment = None
                current['sha256'] = digest.hexdigest()
                self.segments.append(current.copy())
                write_json(self.folder/'segments.json', dict(schema='mvsdk-raw-segments/1', segments=self.segments))

        try:
            index = (self.folder/'frames.jsonl').open('xb')
            last_flush = time.monotonic()
            last_space_check = 0
            while True:
                item = self.queue.get()
                if item is None:
                    break
                pixels, metadata = item
                if time.monotonic()-last_space_check >= 0.25:
                    if shutil.disk_usage(self.folder).free < 128*1024**2:
                        raise OSError('磁盘可用空间低于 128 MB，停止 RAW 写入并保留错误报告')
                    last_space_check = time.monotonic()
                if segment is None or (current['bytes'] and current['bytes']+len(pixels) > self.segment_limit):
                    close_segment()
                    name = f'segment_{len(self.segments)+1:06d}.raw'
                    segment = (self.folder/name).open('xb')
                    current = dict(file=name, bytes=0, frames=0)
                    digest = hashlib.sha256()
                row = dict(metadata, segment=current['file'], offset=current['bytes'], byte_count=len(pixels))
                segment.write(pixels)
                digest.update(pixels)
                current['bytes'] += len(pixels)
                current['frames'] += 1
                line = (json.dumps(clean_json(row), ensure_ascii=False, separators=(',', ':'), allow_nan=False)+'\n').encode('utf-8')
                index.write(line)
                index_digest.update(line)
                self.written += 1
                self.bytes_written += len(pixels)
                if time.monotonic()-last_flush >= 1:
                    segment.flush()
                    os.fsync(segment.fileno())
                    index.flush()
                    os.fsync(index.fileno())
                    last_flush = time.monotonic()
            close_segment()
            index.flush()
            os.fsync(index.fileno())
            self.index_hash = index_digest.hexdigest()
        except Exception as exc:
            self.error = exc
        finally:
            if segment:
                segment.close()
            if index:
                index.close()

    def put(self, pixels, metadata):
        if self.error:
            raise RuntimeError(f'RAW 写入失败：{self.error}')
        try:
            self.queue.put((pixels, metadata), timeout=1)
            self.queue_peak = max(self.queue_peak, self.queue.qsize())
        except queue.Full:
            raise RuntimeError('原始数据写盘跟不上，停止本批采集；不会静默丢弃接收帧')

    def finish(self):
        deadline = time.monotonic()+15
        while self.thread.is_alive() and not self.error:
            try:
                self.queue.put(None, timeout=0.1)
                break
            except queue.Full:
                if time.monotonic() > deadline:
                    raise RuntimeError('RAW 队列排空超时；写入线程仍在处理，不能宣布保存完成')
        self.thread.join(30)
        if self.thread.is_alive():
            raise RuntimeError('RAW 写盘线程未结束；不能宣布保存完成')
        if self.error:
            raise RuntimeError(str(self.error))


class ResearchRecording:
    is_raw = True

    def __init__(self, folder, serial_name, info, settings, experiment, snapshot, simulated):
        self.output = Path(folder)/serial_name
        self.encoder = RawWriter(self.output)
        self.audit = FrameAudit()
        self.sdk_start = None
        self.closed = False
        self.info = dict(schema='mvsdk-research-dataset/1', mode='raw_all_received', serial=info['serial'],
                         model=info['model'], simulated=simulated, software=software_info(), experiment=experiment,
                         settings=vars(settings).copy(), snapshot_start=snapshot, errors=[],
                         metadata_units=METADATA_UNITS,
                         started_at=None, complete=False, index='frames.jsonl', data_path=str(self.output),
                         pixel_policy='SDK raw buffer copied before CameraImageProcess; no software rotation, resampling or repeated frames',
                         timestamp_policy='Original device ticks and host receive clocks; not synchronized between cameras',
                         layout_policy='Exact SDK uBytes; use raw_header and media descriptor, do not assume packed rows or Bayer order')
        try:
            write_json(self.output/'dataset.json', self.info)
        except Exception:
            self.encoder.finish()
            raise

    def wants(self, tick):
        return True

    def observe(self, metadata, selected=True):
        return self.audit.observe(metadata)

    def add_raw(self, pixels, metadata):
        if pixels is None or len(pixels) != metadata['raw_header']['uBytes']:
            raise RuntimeError('原始帧长度与 SDK 帧头不一致')
        if self.info['started_at'] is None:
            self.info['started_at'] = utc_now()
        self.encoder.put(pixels, metadata)

    @property
    def duration(self):
        return max(0, ((self.audit.last_host_ns or 0)-(self.audit.first_host_ns or 0))/1e9)

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
        if not self.encoder.written:
            self.info['errors'].append('未收到并保存有效帧')
        integrity = self.audit.report(self.sdk_start, sdk_stats, self.encoder.written, errors=self.info['errors'])
        self.info.update(ended_at=utc_now(), snapshot_end=snapshot_end, integrity=integrity,
                         written_frames=self.encoder.written, repeated_frames=0,
                         source_frames_observed=self.audit.count, source_fps=integrity['source_fps'],
                         duration_seconds=self.duration, segments=self.encoder.segments,
                         index_sha256=self.encoder.index_hash, queue_peak=self.encoder.queue_peak,
                         bytes_written=self.encoder.bytes_written,
                         complete=not self.info['errors'] and self.encoder.written == self.audit.count,
                         all_frames_unique=True)
        write_json(self.output/'dataset.json', self.info)
        write_json(self.output.with_suffix('.json'), self.info)
        write_report(self.output/'integrity.txt', self.info)
        return self.info


def verify_dataset(folder):
    """Read-only verification, including truncated index and unindexed trailing bytes."""
    folder = Path(folder).resolve()
    errors = []
    manifest_path = folder/'dataset.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    segments = manifest.get('segments', [])
    if not segments and (folder/'segments.json').exists():
        segments = json.loads((folder/'segments.json').read_text(encoding='utf-8'))['segments']
    offsets, count = {}, 0
    with (folder/'frames.jsonl').open('rb') as index:
        for number, line in enumerate(index, 1):
            try:
                row = json.loads(line)
                name = row['segment']
                if Path(name).name != name or not name.startswith('segment_') or not name.endswith('.raw'):
                    raise ValueError('invalid segment path')
                offset, length = row['offset'], row['byte_count']
                if not isinstance(length, int) or length <= 0 or offset != offsets.get(name, 0):
                    raise ValueError('invalid frame offset/length')
                if (folder/name).stat().st_size < offset+length:
                    raise ValueError('frame payload truncated')
                if row['raw_header']['uBytes'] != length or row['receive_index'] != count:
                    raise ValueError('header or receive index mismatch')
                offsets[name] = offset+length
                count += 1
            except Exception as exc:
                errors.append(f'index line {number}: {exc}')
                break
    for path in folder.glob('segment_*.raw'):
        if path.stat().st_size != offsets.get(path.name, 0):
            errors.append(f'{path.name}: unindexed or missing bytes')
    for segment in segments:
        name = segment['file']
        if Path(name).name != name:
            errors.append('invalid manifest segment path')
            continue
        path = folder/name
        if not path.exists() or path.stat().st_size != segment['bytes'] or file_hash(path) != segment['sha256']:
            errors.append(f'{name}: SHA256/size mismatch')
    hash_verified = bool(segments) and set(offsets) == {s['file'] for s in segments}
    expected = manifest.get('index_sha256')
    if expected and file_hash(folder/'frames.jsonl') != expected:
        errors.append('frame index SHA256 mismatch')
    if manifest.get('complete') and count != manifest.get('written_frames'):
        errors.append('manifest frame count mismatch')
    return dict(valid=not errors and bool(manifest.get('complete')) and hash_verified and bool(expected),
                readable_frames=count, complete=bool(manifest.get('complete')),
                all_segment_hashes_present=hash_verified, errors=errors,
                note='Incomplete datasets may retain readable frames; verification never modifies data.')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='只读验证 RAW 数据集的帧索引、长度及 SHA256')
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    result = verify_dataset(args.folder)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['valid'] else 1)
