import gc
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from PIL import Image, ImageGrab
from camera_controls import orient_rgb, validate_changes, apply_sdk_controls, control
from multi_camera_core import DemoCamera, Settings, Recording
from multi_camera_app import RecorderApp


class AdvancedTests(unittest.TestCase):
    def test_sdk_error_rolls_back_partially_applied_controls(self):
        state = dict(gamma=100, contrast=100)
        saved = {}
        def save(handle, path):
            saved.update(state)
            return 0
        def restore(handle, path):
            state.update(saved)
            return 0
        def set_gamma(handle, value):
            state['gamma'] = value
            return 0
        def check(code):
            if code:
                raise RuntimeError('injected SDK failure')
        camera = SimpleNamespace(s=SimpleNamespace(CameraSaveParameterToFile=save, CameraReadParameterFromFile=restore,
                                 CameraSetGamma=set_gamma, CameraSetContrast=lambda *args: -7), h=1,
                                 sys=SimpleNamespace(check=check, get=lambda fn, *args: fn(*args)),
                                 temp=SimpleNamespace(name='unused'), settings=lambda: Settings(),
                                 mirror_horizontal=False, mirror_vertical=False, view_rotate180=True)
        fields = {key: control(key, 'color', value, minimum=0, maximum=255) for key, value in state.items()}
        fields.update(lut_mode=control('LUT', 'color', 0),
                      mirror_horizontal=control('h', 'direction', False, 'bool'),
                      mirror_vertical=control('v', 'direction', False, 'bool'))
        with patch('camera_controls.sdk_controls', return_value=fields):
            with self.assertRaisesRegex(RuntimeError, 'injected SDK'):
                apply_sdk_controls(camera, dict(gamma=85, contrast=110))
        self.assertEqual(state, dict(gamma=100, contrast=100))
        self.assertTrue(camera.view_rotate180)

    def test_rotation_and_mirrors_are_exact_pixel_permutations(self):
        pixels = b''.join(bytes([n, n, n]) for n in range(1, 7))
        self.assertEqual(list(orient_rgb(pixels, (3, 2))[::3]), [6, 5, 4, 3, 2, 1])
        self.assertEqual(list(orient_rgb(pixels, (3, 2), False, True)[::3]), [3, 2, 1, 6, 5, 4])
        self.assertEqual(orient_rgb(pixels, (3, 2), True, True, True), pixels)
        self.assertTrue(Settings().rotate180)

    def test_validation_prevents_ineffective_gamma_and_invalid_levels(self):
        camera = DemoCamera(dict(serial='DEMO', native=0))
        camera.apply_advanced(dict(lut_mode=1))
        with self.assertRaisesRegex(ValueError, 'LUT'):
            camera.apply_advanced(dict(gamma=80))
        self.assertEqual(camera.advanced()['gamma']['value'], 100)
        camera.apply_advanced(dict(lut_mode=0, gamma=80, mirror_horizontal=True))
        self.assertEqual(camera.advanced()['gamma']['value'], 80)
        with self.assertRaises(ValueError):
            camera.apply_advanced(dict(black_level=230, white_level=100))
        with self.assertRaises(ValueError):
            camera.apply_advanced(dict(flat_field=True))
        with self.assertRaises(ValueError):
            camera.apply_advanced(dict(gain=float('nan')))

    def test_mp4_pixels_have_same_rotation_as_preview(self):
        image = Image.new('RGB', (160, 120))
        image.paste((220, 20, 20), (0, 0, 80, 60))
        image.paste((20, 220, 20), (80, 0, 160, 60))
        image.paste((20, 20, 220), (0, 60, 80, 120))
        image.paste((220, 220, 20), (80, 60, 160, 120))
        pixels = orient_rgb(image.tobytes(), image.size)
        with tempfile.TemporaryDirectory() as folder:
            record = Recording(folder, dict(serial='ROT', model='test'), image.size, 30, Settings(), True)
            try:
                for index in range(8):
                    record.add(round(index*10000/30), image.size, pixels)
            finally:
                record.finish()
            decoded = subprocess.check_output([shutil.which('ffmpeg'), '-v', 'error', '-i', str(record.output),
                                               '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'],
                                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result = Image.open(io.BytesIO(decoded)).convert('RGB')
            for position, expected in [((40, 30), (220, 220, 20)), ((120, 30), (20, 20, 220)),
                                       ((40, 90), (20, 220, 20)), ((120, 90), (220, 20, 20))]:
                self.assertTrue(all(abs(a-b) < 12 for a, b in zip(result.getpixel(position), expected)))

    def test_panel_immediate_rotation_apply_and_recording_lock(self):
        root = tk.Tk()
        with tempfile.TemporaryDirectory() as folder:
            app = RecorderApp(root, demo=True, output=folder)
            app.isolate_mindvision = False  # this test inspects the in-process camera object

            def pump(predicate, seconds=12):
                deadline = time.monotonic()+seconds
                while time.monotonic() < deadline:
                    root.update()
                    if predicate():
                        return
                    time.sleep(0.01)
                self.fail('UI condition timed out: '+str(app.last_error))

            try:
                app.scan()
                pump(lambda: app.scan_complete)
                for c in app.cards.values():
                    c.selected.set(c.info['serial'] == 'DEMO-01')
                app.connect_selected()
                card = app.cards['DEMO-01']
                pump(lambda: card.ready)
                self.assertTrue(card.rotate.get())
                self.assertFalse(hasattr(card, 'rotate_check'))
                card.focus_button.invoke()
                focus = card.focus_window
                pump(lambda: focus.frame is not None)
                self.assertEqual(focus.frame.size, card.worker.shape)
                self.assertEqual(focus.frame.size, (640, 400))
                self.assertTrue(card.worker.full_preview.is_set())
                self.assertEqual(focus.scale, 1)
                focus.mode.set('200%')
                focus.render()
                self.assertEqual(focus.scale, 2)
                focus.begin_drag(SimpleNamespace(x=100, y=100))
                focus.pan(SimpleNamespace(x=-3000, y=-3000))
                self.assertTrue(all(0 <= value <= 1 for value in focus.center))
                focus.recenter()
                focus.mode.set('适应窗口')
                focus.render()
                self.assertLessEqual(focus.photo.width(), focus.canvas.winfo_width())
                self.assertLessEqual(focus.photo.height(), focus.canvas.winfo_height())
                focus.fullscreen()
                root.update()
                self.assertTrue(focus.window.attributes('-fullscreen'))
                focus.window.attributes('-fullscreen', False)
                focus.mode.set('100% 原始像素')
                focus.recenter()
                card.advanced_button.invoke()
                panel = card.advanced_panel
                pump(lambda: bool(panel.fields))
                self.assertEqual(panel.variables['rotate180'].get(), '180°')
                self.assertTrue(panel.variables['mirror_horizontal'].get())
                self.assertFalse(panel.variables['mirror_vertical'].get())
                panel.wb_button.invoke()
                pump(lambda: not card.control_pending)
                panel.variables['rotate180'].set('0°')
                panel.widgets['rotate180'].event_generate('<<ComboboxSelected>>')
                pump(lambda: not card.control_pending and not card.rotate.get())
                self.assertFalse(card.worker.camera.settings().rotate180)
                panel.variables['gamma'].set('85')
                panel.changed('gamma')
                # Applying orientation must preserve other unsent edits.
                panel.variables['rotate180'].set('180°')
                panel.widgets['rotate180'].event_generate('<<ComboboxSelected>>')
                pump(lambda: not card.control_pending and card.rotate.get())
                self.assertEqual(panel.variables['gamma'].get(), '85')
                self.assertIn('gamma', panel.dirty)
                panel.apply_button.invoke()
                pump(lambda: not card.control_pending)
                self.assertEqual(card.worker.camera.advanced()['gamma']['value'], 85)
                output = Path(__file__).resolve().parents[1]/'audit'/'advanced-ui'
                output.mkdir(parents=True, exist_ok=True)
                ImageGrab.grab(window=focus.window.winfo_id()).save(output/'focus.png')
                for index, name in enumerate(('orientation', 'color', 'processing')):
                    panel.notebook.select(index)
                    root.update()
                    ImageGrab.grab(window=panel.window.winfo_id()).save(output/(name+'.png'))
                app.start_recording()
                pump(lambda: app.phase == 'recording')
                self.assertEqual(str(panel.apply_button['state']), 'disabled')
                self.assertEqual(str(card.focus_button['state']), 'normal')
                app.stop_recording()
                pump(lambda: app.phase == 'idle')
                panel.close()
                focus.close()
                self.assertFalse(card.worker.full_preview.is_set())
                self.assertIsNone(card.focus_window)
            finally:
                app.close()
                deadline = time.monotonic()+10
                while time.monotonic() < deadline:
                    try:
                        root.update()
                        if not root.winfo_exists():
                            break
                    except tk.TclError:
                        break
                    time.sleep(0.02)
                del pump
                if 'panel' in locals():
                    del panel
                if 'focus' in locals():
                    del focus
                if 'card' in locals():
                    del card
                if 'c' in locals():
                    del c
                del app
                del root
                gc.collect()


if __name__ == '__main__':
    unittest.main(verbosity=2)
