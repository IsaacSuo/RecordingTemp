"""Read-only timestamp replay and synthetic orientation benchmark; no camera access."""
import argparse
import collections
import csv
import json
from pathlib import Path
import statistics
import time
from PIL import Image
from camera_controls import orient_rgb
from record_video import Timeline


def audit(path, iterations=30):
    info = json.loads(path.read_text(encoding='utf-8'))
    rows = [json.loads(line) for line in path.with_suffix('.received.jsonl').read_text(encoding='utf-8').splitlines()]
    with path.with_suffix('.frames.csv').open(encoding='utf-8') as source:
        ledger = list(csv.DictReader(source))
    timeline = Timeline(info['target_fps'], 2**63-1)
    replay = []
    for row in rows:
        replay.extend(timeline.add(row['camera_tick_100us'], row['receive_index']))
    timing = collections.defaultdict(list)
    for first, second in zip(rows, rows[1:]):
        timing['after_selected' if first['selected_for_mp4'] else 'after_skipped'].append(
            (second['host_receive_monotonic_ns']-first['host_receive_monotonic_ns'])/1e6)
    shape = (info['width'], info['height'])
    pixels = bytes((100, 150, 200))*(shape[0]*shape[1])
    def original():
        return Image.frombytes('RGB', shape, pixels).transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes()
    def improved():
        return orient_rgb(pixels, shape, True, True, False)
    assert original() == improved()
    durations = collections.defaultdict(list)
    # Interleave methods to reduce load/thermal drift bias.
    for _ in range(iterations):
        for name, function in (('previous_default_orientation', original), ('optimized_default_orientation', improved)):
            start = time.perf_counter_ns()
            function()
            durations[name].append((time.perf_counter_ns()-start)/1e6)
    stats = lambda values: dict(samples=len(values), median_ms=statistics.median(values), mean_ms=statistics.mean(values), max_ms=max(values))
    return dict(recording=str(path), target_fps=info['target_fps'], actual_receive_fps=info['source_fps'],
                original_received_frames=len(rows), original_output_frames=len(ledger),
                original_repeated_frames=info['repeated_frames'], replay_repeated_frames=timeline.duplicates,
                replay_matches_ledger=len(replay)==len(ledger) and all(int(a['source_receive_index'])==b[1] and bool(int(a['repeated']))==b[2] for a,b in zip(ledger,replay)),
                original_receive_delay={name:stats(values) for name,values in timing.items()},
                benchmark={name:stats(values) for name,values in durations.items()},
                limitations='Synthetic RGB orientation only, not SDK ISP or live camera throughput. Existing recordings were not changed. Fixed-bin CFR selection is unchanged.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recording_json', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = json.dumps(audit(args.recording_json), ensure_ascii=False, indent=2)
    if args.output:
        # Audit results must never overwrite a recording or previous audit.
        with args.output.open('x', encoding='utf-8') as destination:
            destination.write(result+'\n')
    print(result)
