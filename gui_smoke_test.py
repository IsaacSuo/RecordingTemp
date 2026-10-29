"""Exercise the real Tk UI with simulated cameras and capture its own window."""
import json
from pathlib import Path
import time
import tkinter as tk
from PIL import ImageGrab
from multi_camera_app import RecorderApp
from research_data import verify_dataset


def main():
    output = Path(__file__).resolve().parents[1] / 'audit' / 'gui-smoke'
    output.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    app = RecorderApp(root, demo=True, output=output)
    root.update()
    app.scan()
    state = {'stage': 'scan', 'deadline': time.monotonic()+40, 'shot': False}

    def screenshot(name):
        root.update_idletasks()
        # Capture this application's window directly, even when another window overlaps it.
        ImageGrab.grab(window=root.winfo_id()).save(output/name)

    def drive():
        try:
            if time.monotonic() > state['deadline']:
                raise TimeoutError(f'UI test timed out: {state}')
            if app.last_error:
                raise AssertionError(app.last_error)
            if state['stage'] == 'scan' and app.scan_complete:
                assert len(app.cards) == 3, len(app.cards)  # two MindVision + one ZED, all simulated
                assert sum(c.kind == 'zed' for c in app.cards.values()) == 1
                app.connect_button.invoke()
                state['stage'] = 'preview'
            elif state['stage'] == 'preview' and all(c.ready for c in app.cards.values()):
                app.start_button.invoke()
                state['stage'] = 'record30'
            elif state['stage'] in ('record30', 'record60', 'record_raw') and app.phase == 'recording':
                if time.monotonic()-app.batch['started'] > 2:
                    screenshot(state['stage']+'.png')
                    app.stop_button.invoke()
                    state['stage'] = {'record30': 'saved30', 'record60': 'saved60', 'record_raw': 'saved_raw'}[state['stage']]
            elif state['stage'] == 'saved30' and app.phase == 'idle':
                assert len(app.completed_batches) == 1
                assert all(s['complete'] for s in app.completed_batches[-1]['summaries'].values())
                state['stage'] = 'export30'
            elif state['stage'] == 'export30' and not app.exports.busy and not app.exports.pending():
                batch = app.completed_batches[-1]['folder']
                folders = sorted(p.name for p in batch.iterdir() if p.is_dir())
                assert folders == ['mindvision_DEMO-01', 'mindvision_DEMO-02', 'zed_DEMO-ZED-01'], folders
                export = batch / 'zed_DEMO-ZED-01' / 'export'
                assert (export / 'left.mp4').is_file() and (export / 'right.mp4').is_file(), list(export.parent.iterdir())
                saved = json.loads((batch / 'batch.json').read_text(encoding='utf-8'))
                assert saved['exports']['DEMO-ZED-01']['status'] == 'completed', saved['exports']
                app.fps.set('60')
                app.fps_changed()
                assert all(c.exposure.get() == '10' for c in app.cards.values() if c.kind == 'mindvision')
                app.start_button.invoke()
                state['stage'] = 'record60'
            elif state['stage'] == 'saved60' and app.phase == 'idle':
                assert len(app.completed_batches) == 2
                assert all(s['complete'] for s in app.completed_batches[-1]['summaries'].values())
                screenshot('saved.png')
                app.mode.set('研究 RAW（全部接收帧）')
                app.experiment['experiment_id'].set('RAW-SMOKE-001')
                app.experiment['sample_id'].set('synthetic')
                app.mode_changed()
                app.start_button.invoke()
                state['stage'] = 'record_raw'
            elif state['stage'] == 'saved_raw' and app.phase == 'idle':
                assert len(app.completed_batches) == 3
                for result in app.completed_batches[-1]['summaries'].values():
                    assert result['complete'], result
                    if result.get('kind') == 'zed':
                        assert result['integrity']['status'] in ('pass_observed', 'review'), result['integrity']
                        continue
                    assert verify_dataset(result['data_path'])['valid'], result
                    assert result['experiment']['experiment_id'] == 'RAW-SMOKE-001'
                screenshot('saved_raw.png')
                # Exercise window close while recording, including encoder finalization.
                app.start_button.invoke()
                state['stage'] = 'close_recording'
            elif state['stage'] == 'close_recording' and app.phase == 'recording':
                if time.monotonic()-app.batch['started'] > 0.5:
                    state['stage'] = 'closed'
                    app.close()
                    return
            root.after(100, drive)
        except Exception as exc:
            state['failure'] = repr(exc)
            app.close()
    root.after(100, drive)
    root.mainloop()
    state['batches'] = [str(b['folder']) for b in app.completed_batches]
    state['workers_alive'] = [c.info['serial'] for c in app.cards.values() if c.worker and c.worker.is_alive()]
    (output/'result.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    print(json.dumps(state, indent=2))
    if state.get('failure') or state['workers_alive'] or len(state['batches']) != 4:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
