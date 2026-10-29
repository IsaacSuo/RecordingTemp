"""ZED 2i live probe: device info, calibration, settings readback, timed grab and optional SVO2 test.

Run with .venv-zed. Reads camera settings but does not change them. Output goes to a new folder.
"""
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import statistics
import time

ZED_SDK = Path(os.environ.get('ZED_SDK_ROOT_DIR', r'C:\Program Files (x86)\ZED SDK'))
os.add_dll_directory(str(ZED_SDK / 'bin'))
import pyzed.sl as sl  # noqa: E402

SIZES = dict(HD2K=(2208, 1242), HD1080=(1920, 1080), HD720=(1280, 720), VGA=(672, 376))


def plain(value, depth=0):
    """Best-effort JSON view of pyzed structures; enum and unknown objects become strings."""
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
    if depth >= 5 or type(value).__module__ != 'pyzed.sl' or hasattr(value, 'name') and hasattr(value, 'value'):
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
        if callable(item):
            if name in ('get_nanoseconds',):
                result[name] = item()
            continue
        result[name] = plain(item, depth+1)
    return result


def settings(camera):
    values = {}
    for setting in sl.VIDEO_SETTINGS:
        if setting in (sl.VIDEO_SETTINGS.LAST, sl.VIDEO_SETTINGS.AEC_AGC_ROI):
            continue
        entry = dict(supported=camera.is_camera_setting_supported(setting))
        if entry['supported']:
            try:
                code, *value = (camera.get_camera_settings_range(setting) if 'RANGE' in setting.name
                                else camera.get_camera_settings(setting))
                entry.update(code=str(code), value=value[0] if len(value) == 1 else value)
            except Exception as exc:
                entry['error'] = str(exc)
        values[setting.name] = entry
    return values


def interval_stats(stamps, fps):
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    period = 1e9 / fps
    if not gaps:
        return None
    late = [g for g in gaps if g > 1.5 * period]
    return dict(count=len(gaps), min_ms=min(gaps)/1e6, max_ms=max(gaps)/1e6, mean_ms=statistics.fmean(gaps)/1e6,
                stdev_ms=statistics.pstdev(gaps)/1e6, nonincreasing=sum(g <= 0 for g in gaps),
                gaps_over_1_5_period=len(late),
                estimated_missing_frames=sum(max(0, round(g/period) - 1) for g in late),
                fps_from_timestamps=(len(gaps) * 1e9 / (stamps[-1] - stamps[0])) if stamps[-1] > stamps[0] else None)


def verify_svo(path, live_stamps):
    camera = sl.Camera()
    init = sl.InitParameters()
    init.set_from_svo_file(str(path))
    init.svo_real_time_mode = False
    init.depth_mode = sl.DEPTH_MODE.NONE
    code = camera.open(init)
    if code > sl.ERROR_CODE.SUCCESS:
        return dict(error=f'open: {code}')
    try:
        reported = camera.get_svo_number_of_frames()
        stamps, end = [], None
        while True:
            code = camera.grab()
            if code == sl.ERROR_CODE.END_OF_SVOFILE_REACHED:
                break
            if code > sl.ERROR_CODE.SUCCESS:
                end = str(code)
                break
            stamps.append(camera.get_timestamp(sl.TIME_REFERENCE.IMAGE).get_nanoseconds())
        # SVO2 stores microseconds; live timestamps can carry a constant sub-microsecond remainder.
        live, saved = {t // 1000 for t in live_stamps}, {t // 1000 for t in stamps}
        return dict(sdk_reported_frames=reported, decoded_frames=len(stamps), decode_stop=end,
                    timestamp_match_unit='microsecond',
                    duplicated_timestamps=len(stamps) - len(saved),
                    live_frames_missing_in_svo=len(live - saved), svo_frames_not_seen_live=len(saved - live),
                    svo_config=plain(camera.get_camera_information().camera_configuration))
    finally:
        camera.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resolution', default='HD1080', choices=list(SIZES))
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--compression', default='H265_LOSSLESS',
                        choices=['NONE', 'H264', 'H265', 'H264_LOSSLESS', 'H265_LOSSLESS', 'LOSSLESS'])
    parser.add_argument('--serial', type=int)
    parser.add_argument('--settings-every', type=int, default=0,
                        help='Read exposure/gain every N frames during the test (0 = never; reads can disturb streaming)')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent.parent / 'audit')
    args = parser.parse_args()
    folder = args.output / f'zed-probe-{datetime.now():%Y%m%d-%H%M%S}-{args.resolution}-{args.fps}-{args.compression}'
    folder.mkdir(parents=True, exist_ok=False)
    report = dict(schema='zed-probe/1', created_at=datetime.now().astimezone().isoformat(), args={k: str(v) for k, v in vars(args).items()},
                  sdk_version=sl.Camera.get_sdk_version(), timestamp_clock=str(sl.Camera.get_timestamp_clock()),
                  devices=[dict(serial=d.serial_number, model=str(d.camera_model), state=str(d.camera_state))
                           for d in sl.Camera.get_device_list()], errors=[])
    camera = sl.Camera()
    init = sl.InitParameters()
    if args.serial:
        init.set_from_serial_number(args.serial)
    init.camera_resolution = getattr(sl.RESOLUTION, args.resolution)
    init.camera_fps = args.fps
    init.depth_mode = sl.DEPTH_MODE.NONE
    init.async_grab_camera_recovery = False
    init.open_timeout_sec = 5
    report['init_parameters'] = plain(init)
    started = time.monotonic()
    code = camera.open(init)
    report['open_seconds'] = time.monotonic() - started
    if code > sl.ERROR_CODE.SUCCESS:
        raise SystemExit(f'ZED open failed: {code}')
    rows, recording = [], False
    try:
        info = camera.get_camera_information()
        config = info.camera_configuration
        actual = (config.resolution.width, config.resolution.height)
        report['camera_information'] = plain(info)
        if actual != SIZES[args.resolution] or abs(config.fps - args.fps) > 0.01:
            report['errors'].append(f'configuration differs: {actual}@{config.fps}')
        report['settings_start'] = settings(camera)
        for _ in range(int(args.fps)):  # one second warm-up, not recorded
            camera.grab()
        svo = folder / f'{info.serial_number}.svo2'
        if args.compression != 'NONE':
            params = sl.RecordingParameters(str(svo), getattr(sl.SVO_COMPRESSION_MODE, args.compression))
            report['recording_parameters'] = plain(params)
            code = camera.enable_recording(params)
            if code > sl.ERROR_CODE.SUCCESS:
                raise RuntimeError(f'enable_recording: {code}')
            recording = True
        dropped_start = camera.get_frame_dropped_count()
        deadline = time.monotonic() + args.seconds
        failures = {}
        while time.monotonic() < deadline:
            t0 = time.perf_counter_ns()
            code = camera.grab()
            t1 = time.perf_counter_ns()
            if code != sl.ERROR_CODE.SUCCESS:
                failures[str(code)] = failures.get(str(code), 0) + 1
                continue
            row = dict(host_monotonic_ns=time.monotonic_ns(), host_unix_ns=time.time_ns(), grab_ms=(t1-t0)/1e6,
                       image_ns=camera.get_timestamp(sl.TIME_REFERENCE.IMAGE).get_nanoseconds(),
                       center_of_exposure_ns=camera.get_timestamp(sl.TIME_REFERENCE.IMAGE_CENTER_OF_EXPOSURE).get_nanoseconds(),
                       dropped_total=camera.get_frame_dropped_count(), current_fps=camera.get_current_fps())
            health = camera.get_health_status()
            row.update(duplicated_image=health.duplicated_image, low_image_quality=health.low_image_quality,
                       low_lighting=health.low_lighting)
            if recording:
                status = camera.get_recording_status()
                row.update(rec_ok=status.status, ingested=status.number_frames_ingested, encoded=status.number_frames_encoded,
                           compression_ms=status.current_compression_time)
            if args.settings_every and len(rows) % args.settings_every == 0:
                row['exposure'] = camera.get_camera_settings(sl.VIDEO_SETTINGS.EXPOSURE)[1]
                row['gain'] = camera.get_camera_settings(sl.VIDEO_SETTINGS.GAIN)[1]
            row['metadata_ms'] = (time.perf_counter_ns() - t1)/1e6
            rows.append(row)
        report['grab_failures'] = failures
        report['dropped_delta'] = camera.get_frame_dropped_count() - dropped_start
        report['settings_end'] = settings(camera)
        if recording:
            status = camera.get_recording_status()
            report['recording_status_before_disable'] = plain(status)
            camera.disable_recording()
            recording = False
    except Exception as exc:
        report['errors'].append(repr(exc))
    finally:
        if recording:
            camera.disable_recording()
        camera.close()
    with (folder / 'frames.jsonl').open('x', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')
    stamps = [r['image_ns'] for r in rows]
    report['summary'] = dict(
        grabbed_frames=len(rows), span_s=(stamps[-1]-stamps[0])/1e9 if len(stamps) > 1 else None,
        image_intervals=interval_stats(stamps, args.fps),
        host_intervals=interval_stats([r['host_monotonic_ns'] for r in rows], args.fps),
        image_minus_host_unix_ms=dict(median=statistics.median((r['host_unix_ns'] - r['image_ns'])/1e6 for r in rows)) if rows else None,
        center_minus_image_ms=statistics.median((r['center_of_exposure_ns'] - r['image_ns'])/1e6 for r in rows) if rows else None,
        grab_ms_max=max((r['grab_ms'] for r in rows), default=None),
        metadata_ms_mean=statistics.fmean(r['metadata_ms'] for r in rows) if rows else None,
        metadata_ms_max=max((r['metadata_ms'] for r in rows), default=None),
        health_flags={k: sum(bool(r[k]) for r in rows) for k in ('duplicated_image', 'low_image_quality', 'low_lighting')},
        exposure_samples=sorted({r['exposure'] for r in rows if 'exposure' in r}),
        gain_samples=sorted({r['gain'] for r in rows if 'gain' in r}))
    svo = next(folder.glob('*.svo2'), None)
    if svo:
        size = svo.stat().st_size
        report['svo'] = dict(file=svo.name, bytes=size, mb_per_s=size/1e6/args.seconds,
                             ingest_minus_encoded_max=max((r['ingested']-r['encoded'] for r in rows if 'ingested' in r), default=None),
                             verify=verify_svo(svo, stamps))
    (folder / 'probe.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(folder=str(folder), errors=report['errors'], grab_failures=report.get('grab_failures'),
                          dropped_delta=report.get('dropped_delta'), summary=report['summary'], svo=report.get('svo', {}) and
                          {k: v for k, v in report['svo'].items() if k != 'verify'} | {k: v for k, v in report['svo']['verify'].items() if k != 'svo_config'}),
                     ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
