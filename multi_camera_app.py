"""A local Windows GUI for multi-camera recording: MindVision (MP4 / RAW) and ZED (SVO2)."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

from PIL import Image, ImageTk
from multi_camera_core import CameraWorker, DemoSystem, MindVisionProcessWorker, SDKSystem, Settings, safe_name
from research_data import APP_VERSION, write_json, software_info
from advanced_panel import AdvancedPanel
from focus_preview import FocusPreview
from export_queue import DEFAULT_EXPORT, EXPORT_CHOICES, ExportQueue
from zed_support import (ZedWorker, ZedSettings, list_devices as list_zed_devices, MODES as ZED_MODES,
                         COMPRESSIONS as ZED_COMPRESSIONS, FLIPS as ZED_FLIPS, DEFAULT_MODE as ZED_DEFAULT_MODE,
                         DEFAULT_COMPRESSION as ZED_DEFAULT_COMPRESSION, DEFAULT_FLIP as ZED_DEFAULT_FLIP)

ROOT = Path(__file__).resolve().parent
FONT = 'Microsoft YaHei UI'
BG = '#0f161d'        # window
PANEL = '#16212b'     # top bar, sidebar, settings drawer, status bar
CARD = '#1b2833'      # camera tiles
FIELD = '#0d141b'
LINE = '#2a3a48'
TEXT = '#e8eef3'
MUTED = '#93a6b5'
DIM = '#62778a'
ACCENT = '#4ed4b1'
DANGER = '#ff7b86'
WARN = '#f2c06b'
SHORTCUT = 'F9'


def enable_dpi_awareness():
    """Crisp text on scaled Windows displays; otherwise Windows bitmap-stretches the window and text blurs."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class CameraCard:
    """One camera: a tile in the grid, a row in the device list and a controls page in the settings drawer."""
    kind = 'mindvision'
    type_label = 'MindVision'
    advanced_note = ('RAW 保持原始字节；方向和 ISP 调整作用于预览 / MP4。研究采集使用手动曝光和白平衡。\n'
                     'Gamma / 对比度仅在动态 LUT 下生效。3D 降噪会混合多帧，运动测量通常关闭；修改后采用等权重。')

    def __init__(self, app, info, index):
        self.app, self.info, self.index = app, info, index
        self.worker = None
        self.connected = self.ready = False
        self.recording = False
        self.issue = None
        self.last_fps = self.raw_size = 0
        self.advanced_panel = None
        self.focus_window = None
        self.control_pending = False
        px = app.px
        self.selected = tk.BooleanVar(value=True)
        self.position = tk.StringVar()
        self.exposure = tk.StringVar(value='20')
        self.gain = tk.StringVar(value='1')
        self.rotate = tk.BooleanVar(value=True)

        # Tile in the camera grid.
        self.frame = tk.Frame(app.grid_frame, bg=CARD, padx=px(10), pady=px(8), highlightthickness=px(2),
                              highlightbackground=LINE, highlightcolor=LINE)
        header = tk.Frame(self.frame, bg=CARD)
        header.pack(fill='x')
        self.title = tk.Label(header, text=self.display_name(), bg=CARD, fg=TEXT, font=(FONT, 11, 'bold'), anchor='w')
        self.title.pack(side='left')
        tk.Label(header, text=self.type_label, bg=LINE, fg=MUTED, font=(FONT, 8), padx=px(6)).pack(side='left', padx=px(8))
        self.state = tk.Label(header, text='未连接', fg=DIM, bg=CARD, font=(FONT, 9))
        self.state.pack(side='right')
        tk.Label(self.frame, text=f'{info["model"]} · {info["serial"]}', bg=CARD, fg=DIM, font=(FONT, 8), anchor='w').pack(fill='x')
        self.preview_box = tk.Frame(self.frame, bg='#070b0f', width=px(320), height=px(200))
        self.preview_box.pack(fill='x', pady=(px(6), px(6)))
        self.preview_box.pack_propagate(False)
        self.preview = tk.Label(self.preview_box, text='未连接', bg='#070b0f', fg=DIM, font=(FONT, 10))
        self.preview.pack(fill='both', expand=True)
        self.photo = None
        footer = tk.Frame(self.frame, bg=CARD)
        footer.pack(fill='x')
        self.stats = tk.Label(footer, text='—', fg=MUTED, bg=CARD, anchor='w', font=(FONT, 9))
        self.stats.pack(side='left')
        self.settings_button = ttk.Button(footer, text='参数', style='Small.TButton', command=self.select)
        self.settings_button.pack(side='right')
        self.focus_button = ttk.Button(footer, text='放大对焦', style='Small.TButton', command=self.open_focus)
        self.focus_button.pack(side='right', padx=px(6))
        self.record_stats = tk.Label(self.frame, text='', fg=ACCENT, bg=CARD, anchor='w', font=(FONT, 9))
        self.record_stats.pack(fill='x')
        for widget in (self.frame, self.preview, self.title):
            widget.bind('<Button-1>', lambda event: self.select())
        self.preview.bind('<Double-Button-1>', lambda event: self.open_focus())

        # Row in the sidebar device list.
        self.row = tk.Frame(app.device_list, bg=PANEL)
        self.row.pack(fill='x', pady=px(1))
        self.check = tk.Checkbutton(self.row, variable=self.selected, command=app.refresh_controls, bg=PANEL,
                                    activebackground=PANEL, selectcolor=FIELD, fg=TEXT, highlightthickness=0, bd=0)
        self.check.pack(side='left')
        self.dot = tk.Label(self.row, text='●', fg=DIM, bg=PANEL, font=(FONT, 9))
        self.dot.pack(side='left')
        self.row_name = tk.Label(self.row, text=self.display_name(), fg=TEXT, bg=PANEL, anchor='w', font=(FONT, 9), cursor='hand2')
        self.row_name.pack(side='left', padx=(px(4), 0))
        self.row_name.bind('<Button-1>', lambda event: self.select())
        self.row_info = tk.Label(self.row, text=self.type_label, fg=DIM, bg=PANEL, font=(FONT, 8))
        self.row_info.pack(side='right')

        # Page in the settings drawer.
        self.controls = tk.Frame(app.drawer_body, bg=PANEL)
        self.field_row('位置 / 用途', 'position_entry', self.position, 18)
        self.position.trace_add('write', lambda *args: self.rename())
        grid = tk.Frame(self.controls, bg=PANEL)
        grid.pack(fill='x', pady=(px(10), 0))
        self.exp_label = tk.Label(grid, text='曝光 ms', fg=MUTED, bg=PANEL)
        self.exp_label.grid(row=0, column=0, sticky='w')
        self.exp_entry = ttk.Entry(grid, textvariable=self.exposure, width=9)
        self.exp_entry.grid(row=1, column=0, sticky='w', padx=(0, px(10)))
        self.gain_label = tk.Label(grid, text='增益 ×', fg=MUTED, bg=PANEL)
        self.gain_label.grid(row=0, column=1, sticky='w')
        self.gain_entry = ttk.Entry(grid, textvariable=self.gain, width=9)
        self.gain_entry.grid(row=1, column=1, sticky='w')
        buttons = tk.Frame(self.controls, bg=PANEL)
        buttons.pack(fill='x', pady=(px(10), 0))
        self.apply_button = ttk.Button(buttons, text='应用', style='Small.TButton', command=self.apply)
        self.apply_button.pack(side='left')
        self.wb_button = ttk.Button(buttons, text='一次白平衡', style='Small.TButton', command=self.white_balance)
        self.wb_button.pack(side='left', padx=px(6))
        self.advanced_button = ttk.Button(self.controls, text='高级参数与方向…', command=self.open_advanced)
        self.advanced_button.pack(fill='x', pady=(px(10), 0))
        self.range_label = tk.Label(self.controls, text='白平衡前，让中性白色或灰色目标充满画面', fg=DIM, bg=PANEL,
                                    anchor='w', justify='left', font=(FONT, 8), wraplength=px(270))
        self.range_label.pack(fill='x', pady=(px(10), 0))
        self.build_extra()
        self.lock(False)

    # -- helpers
    def display_name(self):
        position = self.position.get().strip() if hasattr(self, 'position') else ''
        return position or self.info['model']

    def rename(self):
        name = self.display_name()
        self.title.configure(text=name)
        self.row_name.configure(text=name)
        if self.app.selected_card is self:
            self.app.drawer_title.configure(text=name)

    def field_row(self, label, attribute, variable, width):
        tk.Label(self.controls, text=label, fg=MUTED, bg=PANEL, anchor='w').pack(fill='x')
        entry = ttk.Entry(self.controls, textvariable=variable, width=width)
        entry.pack(fill='x', pady=(self.app.px(2), 0))
        setattr(self, attribute, entry)
        return entry

    def combo_row(self, label, variable, values):
        tk.Label(self.controls, text=label, fg=MUTED, bg=PANEL, anchor='w').pack(fill='x', pady=(self.app.px(8), 0))
        box = ttk.Combobox(self.controls, textvariable=variable, values=values, state='readonly')
        box.pack(fill='x', pady=(self.app.px(2), 0))
        return box

    def select(self):
        self.app.select_card(self)

    def set_preview_size(self, width, height):
        self.preview_box.configure(width=width, height=height)
        self.preview_size = (width, height)

    def show_state(self, text, color):
        self.state.configure(text=text, fg=color)
        self.refresh_style()

    def refresh_style(self):
        if self.recording:
            color = WARN if self.issue else DANGER
        elif self.issue:
            color = WARN
        elif self.app.selected_card is self:
            color = ACCENT
        else:
            color = LINE
        self.frame.configure(highlightbackground=color, highlightcolor=color)
        dot = DANGER if self.recording else WARN if self.issue else ACCENT if self.connected else DIM
        self.dot.configure(fg=dot)

    # -- camera specific
    def build_extra(self):
        pass

    def create_worker(self):
        if self.app.isolate_mindvision:
            return MindVisionProcessWorker(self.info, self.app.events, demo=self.app.demo, log_folder=ROOT / 'logs')
        return CameraWorker(self.app.system, self.info, self.app.events)

    def prepare_extra(self):
        return {}

    def on_connected(self, data):
        self.set_settings(data['settings'])
        low, high = data['gain_range']
        self.range_label.configure(text=f'增益范围 {low:g}–{high:g}×。一次白平衡前，让中性白 / 灰目标充满画面。'
                                        '方向（默认 180° + 水平镜像）在高级参数里修改。')

    def settings(self):
        try:
            result = Settings(float(self.exposure.get()), float(self.gain.get()), self.rotate.get())
        except ValueError:
            raise ValueError(f'{self.info["serial"]}：曝光和增益须填写数字')
        result.validate()
        return result

    def set_settings(self, settings):
        self.exposure.set(f'{settings.exposure_ms:.4f}'.rstrip('0').rstrip('.'))
        self.gain.set(f'{settings.gain:.3f}'.rstrip('0').rstrip('.'))
        self.rotate.set(settings.rotate180)

    def apply(self):
        try:
            self.worker.command('apply', settings=self.settings())
        except Exception as exc:
            self.app.note(str(exc), True)

    def white_balance(self):
        self.worker.command('wb')

    def open_advanced(self):
        if self.advanced_panel is not None:
            self.advanced_panel.window.lift()
            return
        self.advanced_panel = AdvancedPanel(self)

    def open_focus(self):
        if not self.connected or self.app.closing or self.app.disconnecting:
            return
        if self.focus_window:
            self.focus_window.window.lift()
            return
        self.focus_window = FocusPreview(self)

    def lock(self, recording):
        self.focus_button.configure(state='normal' if self.connected and not self.app.closing and not self.app.disconnecting else 'disabled')
        state = 'normal' if self.connected and not recording else 'disabled'
        for widget in (self.exp_entry, self.gain_entry, self.apply_button, self.wb_button, self.advanced_button):
            widget.configure(state=state)
        self.position_entry.configure(state='disabled' if recording else 'normal')
        self.check.configure(state='disabled' if recording else 'normal')
        if self.advanced_panel:
            self.advanced_panel.sync_state()

    def update_preview(self):
        if not self.worker:
            return
        image = self.worker.take_preview()
        if image:
            if self.focus_window and image.size == self.worker.shape:
                self.focus_window.update(image)
            image = image.copy()
            image.thumbnail(getattr(self, 'preview_size', (320, 200)), Image.Resampling.BILINEAR)
            self.photo = ImageTk.PhotoImage(image)
            self.preview.configure(image=self.photo, text='')

    def clear_preview(self, text):
        self.photo = None
        self.preview.configure(image='', text=text)

    def destroy(self):
        for widget in (self.frame, self.row, self.controls):
            widget.destroy()


class ZedCard(CameraCard):
    """ZED 2i: SVO2 recording in a separate process; exposure / gain are SDK 0-100 units."""
    kind = 'zed'
    type_label = 'ZED · SVO2'
    advanced_note = ('ZED 参数为 SDK 0–100 等单位，录制期间不读写相机参数（读取会造成丢帧）。开始录制时自动曝光 / 白平衡锁定为当时的值。\n'
                     '画面翻转、分辨率与帧率属于连接参数：断开后在参数面板修改，重新连接生效。')

    def build_extra(self):
        self.exp_label.configure(text='曝光 %（空 = 自动）')
        self.gain_label.configure(text='增益（空 = 自动）')
        self.exposure.set('')
        self.gain.set('')
        self.wb_button.configure(text='自动白平衡并锁定')
        self.advanced_button.configure(text='高级参数…')
        self.range_label.configure(text='曝光 / 增益填 0–100，两项都留空为自动；开始录制时锁定当前值。'
                                        'SVO2 保存左右目原图、IMU 与逐帧时间戳。')
        self.mode = tk.StringVar(value=self.info.get('mode', ZED_DEFAULT_MODE))
        self.compression = tk.StringVar(value=ZED_DEFAULT_COMPRESSION)
        self.flip = tk.StringVar(value=self.info.get('flip', ZED_DEFAULT_FLIP))
        self.mode_box = self.combo_row('分辨率与帧率（连接前选择）', self.mode, list(ZED_MODES))
        self.flip_box = self.combo_row('画面翻转（连接前选择）', self.flip, list(ZED_FLIPS))
        self.compression_box = self.combo_row('SVO2 压缩', self.compression, list(ZED_COMPRESSIONS))

    def settings(self):
        def number(text, name):
            text = text.strip()
            if text in ('', '自动', 'auto'):
                return None
            try:
                return int(text)
            except ValueError:
                raise ValueError(f'{self.info["serial"]}：ZED {name}须为 0–100 的整数，留空为自动')
        result = ZedSettings(number(self.exposure.get(), '曝光'), number(self.gain.get(), '增益'))
        result.validate()
        return result

    def set_settings(self, settings):
        self.exposure.set('' if settings.exposure is None else str(settings.exposure))
        self.gain.set('' if settings.gain is None else str(settings.gain))

    def create_worker(self):
        self.info.update(mode=self.mode.get(), flip=self.flip.get())
        return ZedWorker(self.info, self.app.events, demo=self.app.demo, log_folder=ROOT / 'logs')

    def prepare_extra(self):
        return dict(compression=ZED_COMPRESSIONS[self.compression.get()])

    def on_connected(self, data):
        self.set_settings(data['settings'])
        source = '模拟' if data.get('simulated') else f'固件 {data.get("firmware", {}).get("camera")} · SDK {data.get("sdk_version")}'
        self.range_label.configure(text=f'{data.get("mode_label")} · 翻转 {data.get("flip")} · {source}。'
                                        '曝光 / 增益 0–100，两项都留空为自动；开始录制时锁定当前值。')
        self.row_info.configure(text=f'ZED · {data.get("mode_label", "")}')

    def lock(self, recording):
        super().lock(recording)
        if not hasattr(self, 'mode_box'):
            return
        live = bool(self.worker and self.worker.is_alive())
        for box in (self.mode_box, self.flip_box):
            box.configure(state='disabled' if live else 'readonly')
        self.compression_box.configure(state='disabled' if recording else 'readonly')


class RecorderApp:
    # Each MindVision camera runs in its own process (mv_worker.py); False keeps the in-process worker for tests.
    isolate_mindvision = True
    # A camera that loses its USB link can block inside the vendor SDK. Never wait for it forever.
    PREPARE_TIMEOUT_S = 45
    NO_IMAGE_WARN_S = 10  # connected but no statistics from the worker: usually a USB link the SDK keeps recovering
    STOP_TIMEOUT_S = 60
    CLOSE_TIMEOUT_S = 60       # while a batch is being saved
    DISCONNECT_TIMEOUT_S = 20  # connected cameras restoring their settings

    def __init__(self, root, demo=False, output=None):
        self.root, self.demo = root, demo
        self.scale = max(1.0, root.winfo_fpixels('1i') / 96)
        self.events = queue.Queue()
        self.cards = {}
        self.system = None
        self.scanning = self.closing = self.disconnecting = False
        self.phase = 'idle'
        self.batch = None
        self.last_folder = None
        self.completed_batches = []
        self.scan_complete = False
        self.last_error = None
        self.selected_card = None
        self.result_window = None
        self.columns = 0
        self.disk_checked = 0
        self.written_sample = None
        px = self.px
        root.title('多相机录像台 2.4' + (' · 模拟模式' if demo else ''))
        root.configure(bg=BG)
        width, height = min(px(1480), root.winfo_screenwidth()-px(40)), min(px(900), root.winfo_screenheight()-px(80))
        root.geometry(f'{width}x{height}+{px(20)}+{px(20)}')
        root.minsize(min(px(1100), width), min(px(640), height))
        if not demo:
            try:
                root.state('zoomed')
            except tk.TclError:
                pass
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.build_style()
        self.output = tk.StringVar(value=str(output or ROOT.parent / 'recordings'))
        self.fps = tk.StringVar(value='30')
        self.mode = tk.StringVar(value='MP4 视频')
        self.export_choice = tk.StringVar(value=DEFAULT_EXPORT)
        self.exports = ExportQueue(self.events, demo, ROOT / 'logs')
        self.experiment = {key: tk.StringVar() for key in
                           ('experiment_id', 'sample_id', 'repeat_id', 'operator', 'lens', 'aperture', 'lighting', 'notes')}
        self.build_top_bar()
        self.build_status_bar()
        body = tk.Frame(root, bg=BG)
        body.pack(fill='both', expand=True)
        self.build_sidebar(body)
        self.build_drawer(body)
        self.build_grid(body)
        root.bind('<F9>', lambda event: self.toggle_recording())
        self.refresh_controls()
        root.after(80, self.poll)

    def px(self, value):
        return int(round(value * self.scale))

    # ------------------------------------------------------------ layout
    def build_style(self):
        px = self.px
        style = self.style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font=(FONT, 10))
        style.configure('TButton', background='#2a3c4b', foreground=TEXT, borderwidth=0, padding=(px(12), px(6)))
        style.map('TButton', background=[('active', '#38506a'), ('disabled', '#1e2a35')], foreground=[('disabled', '#5a6c7b')])
        style.configure('Small.TButton', padding=(px(8), px(3)), font=(FONT, 9))
        style.configure('Start.TButton', background=ACCENT, foreground='#062a22', font=(FONT, 13, 'bold'), padding=(px(16), px(14)))
        style.map('Start.TButton', background=[('active', '#7de7cb'), ('disabled', '#22413c')], foreground=[('disabled', '#5f857d')])
        style.configure('Stop.TButton', background='#b8434e', foreground='white', font=(FONT, 13, 'bold'), padding=(px(16), px(14)))
        style.map('Stop.TButton', background=[('active', '#d25763'), ('disabled', '#3a262b')], foreground=[('disabled', '#8a6f75')])
        style.configure('TEntry', fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, bordercolor=LINE, padding=px(5))
        style.map('TEntry', fieldbackground=[('disabled', '#141d25')], foreground=[('disabled', '#6b7d8b')])
        style.configure('TCombobox', fieldbackground=FIELD, background='#2a3c4b', foreground=TEXT, padding=px(4), arrowsize=px(12))
        style.map('TCombobox', fieldbackground=[('readonly', FIELD), ('disabled', '#141d25')],
                  foreground=[('readonly', TEXT), ('disabled', '#6b7d8b')])
        self.root.option_add('*TCombobox*Listbox.font', (FONT, 10))
        style.configure('Vertical.TScrollbar', background='#2a3c4b', troughcolor=BG, borderwidth=0, arrowsize=px(12))

    def build_top_bar(self):
        px = self.px
        bar = tk.Frame(self.root, bg=PANEL, padx=px(16), pady=px(8))
        bar.pack(fill='x')
        tk.Label(bar, text='多相机录像台', bg=PANEL, fg=TEXT, font=(FONT, 15, 'bold')).pack(side='left')
        tk.Label(bar, text=f'v{APP_VERSION}', bg=PANEL, fg=DIM, font=(FONT, 8)).pack(side='left', padx=(px(6), px(16)), pady=(px(6), 0))
        self.status_pill = tk.Label(bar, text='空闲', bg=LINE, fg=MUTED, font=(FONT, 10, 'bold'), padx=px(10), pady=px(2))
        self.status_pill.pack(side='left')
        self.summary_label = tk.Label(bar, text='尚未搜索相机', bg=PANEL, fg=MUTED, font=(FONT, 10))
        self.summary_label.pack(side='left', padx=px(14))
        if self.demo:
            tk.Label(bar, text='模拟模式 · 不连接实际相机', bg=PANEL, fg=ACCENT, font=(FONT, 9)).pack(side='right', padx=(px(14), 0))
        self.disk_label = tk.Label(bar, text='', bg=PANEL, fg=MUTED, font=(FONT, 9))
        self.disk_label.pack(side='right')

    def section(self, parent, number, title, right=None):
        px = self.px
        head = tk.Frame(parent, bg=PANEL)
        head.pack(fill='x', pady=(px(14), px(6)))
        tk.Label(head, text=number, bg=ACCENT, fg='#062a22', font=(FONT, 8, 'bold'), width=2).pack(side='left')
        tk.Label(head, text=title, bg=PANEL, fg=TEXT, font=(FONT, 10, 'bold')).pack(side='left', padx=px(6))
        if right is not None:
            right.pack(in_=head, side='right')
        return head

    def labeled(self, parent, text):
        tk.Label(parent, text=text, bg=PANEL, fg=MUTED, anchor='w', font=(FONT, 9)).pack(fill='x', pady=(self.px(6), self.px(2)))

    def build_sidebar(self, body):
        px = self.px
        side = tk.Frame(body, bg=PANEL, width=px(300), padx=px(14))
        side.pack(side='left', fill='y')
        side.pack_propagate(False)
        # Record button pinned to the bottom so it never scrolls away.
        record = tk.Frame(side, bg=PANEL)
        record.pack(side='bottom', fill='x', pady=(px(8), px(14)))
        self.session_text = tk.Label(record, text='先搜索并连接相机', bg=PANEL, fg=MUTED, anchor='w', justify='left',
                                     wraplength=px(270), font=(FONT, 9))
        self.session_text.pack(fill='x', pady=(0, px(8)))
        self.record_slot = tk.Frame(record, bg=PANEL)
        self.record_slot.pack(fill='x')
        self.start_button = ttk.Button(self.record_slot, text=f'●  开始录像    {SHORTCUT}', style='Start.TButton', command=self.start_recording)
        self.stop_button = ttk.Button(self.record_slot, text=f'■  停止录像    {SHORTCUT}', style='Stop.TButton', command=self.stop_recording)
        self.start_button.pack(fill='x')

        self.count_label = tk.Label(side, text='', bg=PANEL, fg=DIM, font=(FONT, 8))
        self.section(side, '1', '相机', self.count_label)
        buttons = tk.Frame(side, bg=PANEL)
        buttons.pack(fill='x')
        self.scan_button = ttk.Button(buttons, text='搜索', style='Small.TButton', command=self.scan)
        self.scan_button.pack(side='left', fill='x', expand=True)
        self.connect_button = ttk.Button(buttons, text='连接所选', style='Small.TButton', command=self.connect_selected)
        self.connect_button.pack(side='left', fill='x', expand=True, padx=px(4))
        self.disconnect_button = ttk.Button(buttons, text='断开', style='Small.TButton', command=self.disconnect_all)
        self.disconnect_button.pack(side='left', fill='x', expand=True)
        self.device_list = tk.Frame(side, bg=PANEL)
        self.device_list.pack(fill='x', pady=(px(6), 0))
        self.device_hint = tk.Label(self.device_list, text='点“搜索”查找已连接的 MindVision 与 ZED 相机', bg=PANEL, fg=DIM,
                                    font=(FONT, 9), anchor='w', justify='left', wraplength=px(270))
        self.device_hint.pack(fill='x')

        self.section(side, '2', '录制设置')
        row = tk.Frame(side, bg=PANEL)
        row.pack(fill='x')
        left, right = tk.Frame(row, bg=PANEL), tk.Frame(row, bg=PANEL)
        left.pack(side='left', fill='x', expand=True)
        right.pack(side='left', padx=(px(8), 0))
        self.labeled(left, '模式')
        self.mode_box = ttk.Combobox(left, textvariable=self.mode, values=('MP4 视频', '研究 RAW（全部接收帧）'), state='readonly')
        self.mode_box.pack(fill='x')
        self.mode_box.bind('<<ComboboxSelected>>', self.mode_changed)
        self.labeled(right, 'MP4 帧率')
        self.fps_box = ttk.Combobox(right, textvariable=self.fps, values=('30', '60'), state='readonly', width=5)
        self.fps_box.pack()
        self.fps_box.bind('<<ComboboxSelected>>', self.fps_changed)
        self.labeled(side, '保存到')
        folder = tk.Frame(side, bg=PANEL)
        folder.pack(fill='x')
        self.folder_entry = ttk.Entry(folder, textvariable=self.output)
        self.folder_entry.pack(side='left', fill='x', expand=True)
        self.browse_button = ttk.Button(folder, text='…', style='Small.TButton', width=2, command=self.choose_folder)
        self.browse_button.pack(side='left', padx=(px(4), 0))
        self.open_button = ttk.Button(folder, text='打开', style='Small.TButton', command=self.open_folder)
        self.open_button.pack(side='left', padx=(px(4), 0))
        presets = tk.Frame(side, bg=PANEL)
        presets.pack(fill='x', pady=(px(8), 0))
        self.save_preset_button = ttk.Button(presets, text='保存参数预设', style='Small.TButton', command=self.save_preset)
        self.save_preset_button.pack(side='left', fill='x', expand=True)
        self.load_preset_button = ttk.Button(presets, text='加载预设', style='Small.TButton', command=self.load_preset)
        self.load_preset_button.pack(side='left', fill='x', expand=True, padx=(px(4), 0))
        self.labeled(side, 'ZED 录完后自动导出')
        self.export_box = ttk.Combobox(side, textvariable=self.export_choice, values=list(EXPORT_CHOICES), state='readonly')
        self.export_box.pack(fill='x')

        self.section(side, '3', '实验信息')
        self.experiment_entries = []
        form = tk.Frame(side, bg=PANEL)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        for row_index, (key, label) in enumerate((('experiment_id', '实验编号'), ('sample_id', '样品'), ('repeat_id', '重复编号'))):
            tk.Label(form, text=label, bg=PANEL, fg=MUTED, font=(FONT, 9)).grid(row=row_index, column=0, sticky='w', pady=px(2))
            entry = ttk.Entry(form, textvariable=self.experiment[key])
            entry.grid(row=row_index, column=1, sticky='ew', padx=(px(8), 0), pady=px(2))
            self.experiment_entries.append(entry)
        self.experiment_button = ttk.Button(side, text='操作者、镜头、光源、备注…', style='Small.TButton', command=self.edit_experiment)
        self.experiment_button.pack(fill='x', pady=(px(6), 0))

    def build_drawer(self, body):
        px = self.px
        self.drawer = tk.Frame(body, bg=PANEL, width=px(300), padx=px(14), pady=px(12))
        self.drawer.pack_propagate(False)
        head = tk.Frame(self.drawer, bg=PANEL)
        head.pack(fill='x')
        tk.Label(head, text='相机参数', bg=PANEL, fg=DIM, font=(FONT, 9)).pack(side='left')
        ttk.Button(head, text='×', style='Small.TButton', width=2, command=lambda: self.select_card(None)).pack(side='right')
        self.drawer_title = tk.Label(self.drawer, text='', bg=PANEL, fg=TEXT, font=(FONT, 12, 'bold'), anchor='w')
        self.drawer_title.pack(fill='x', pady=(px(4), px(10)))
        self.drawer_body = tk.Frame(self.drawer, bg=PANEL)
        self.drawer_body.pack(fill='both', expand=True)

    def build_grid(self, body):
        px = self.px
        pane = tk.Frame(body, bg=BG)
        pane.pack(side='left', fill='both', expand=True)
        self.canvas = tk.Canvas(pane, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(pane, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.grid_frame = tk.Frame(self.canvas, bg=BG, padx=px(8), pady=px(8))
        self.canvas_window = self.canvas.create_window((0, 0), window=self.grid_frame, anchor='nw')
        self.grid_frame.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self.on_canvas_resize)
        self.canvas.bind('<Enter>', lambda e: self.root.bind_all('<MouseWheel>', self.on_wheel))
        self.canvas.bind('<Leave>', lambda e: self.root.unbind_all('<MouseWheel>'))
        self.empty = tk.Frame(self.grid_frame, bg=BG, pady=px(80))
        tk.Label(self.empty, text='三步开始', bg=BG, fg=TEXT, font=(FONT, 16, 'bold')).pack()
        for step in ('1  在左侧点“搜索”，勾选要用的相机，点“连接所选”',
                     '2  点相机画面打开参数，调曝光、增益、白平衡；双击画面放大对焦',
                     f'3  填写实验信息，按“开始录像”或 {SHORTCUT}；再按一次停止'):
            tk.Label(self.empty, text=step, bg=BG, fg=MUTED, font=(FONT, 11), anchor='w').pack(fill='x', pady=px(4))
        tk.Label(self.empty, text='使用前请关闭官方软件和 ZED Explorer，避免占用相机', bg=BG, fg=DIM, font=(FONT, 9)).pack(pady=(px(18), 0))
        self.empty.grid(row=0, column=0, sticky='ew')

    def build_status_bar(self):
        px = self.px
        bar = tk.Frame(self.root, bg=PANEL, padx=px(14), pady=px(5))
        bar.pack(side='bottom', fill='x')
        top = tk.Frame(bar, bg=PANEL)
        top.pack(fill='x')
        self.note_text = tk.StringVar(value='连接时保留当前分辨率和颜色处理；关闭时恢复连接前的相机参数。')
        self.note_label = tk.Label(top, textvariable=self.note_text, bg=PANEL, fg=MUTED, anchor='w', font=(FONT, 9))
        self.note_label.pack(side='left', fill='x', expand=True)
        self.log_button = ttk.Button(top, text='消息记录 ▴', style='Small.TButton', command=self.toggle_log)
        self.log_button.pack(side='right')
        self.export_label = tk.Label(top, text='', bg=PANEL, fg=ACCENT, font=(FONT, 9))
        self.export_label.pack(side='right', padx=px(12))
        self.log = tk.Text(bar, height=8, bg=FIELD, fg=MUTED, font=(FONT, 9), relief='flat', state='disabled')

    def toggle_log(self):
        if self.log.winfo_ismapped():
            self.log.pack_forget()
            self.log_button.configure(text='消息记录 ▴')
        else:
            self.log.pack(fill='x', pady=(self.px(6), 0))
            self.log_button.configure(text='消息记录 ▾')
            self.log.see('end')

    def on_wheel(self, event):
        self.canvas.yview_scroll(-int(event.delta/120), 'units')

    def on_canvas_resize(self, event):
        self.canvas.itemconfigure(self.canvas_window, width=event.width)
        self.root.after_idle(self.layout_tiles)

    def layout_tiles(self):
        cards = list(self.cards.values())
        if not cards:
            return
        px = self.px
        width = max(self.canvas.winfo_width(), px(400))
        height = max(self.canvas.winfo_height(), px(300))
        gap, chrome, aspect = px(8), px(130), 0.62  # chrome: tile header, footer and padding around the preview

        def fit(columns):
            rows = -(-len(cards) // columns)
            preview_w = (width - px(16) - gap * (columns + 1)) // columns - px(24)
            preview_h = min(int(preview_w * aspect), (height - px(16) - gap * rows) // rows - chrome)
            return min(preview_w, int(preview_h / aspect)), preview_w, preview_h
        # Choose the column count that shows every camera without scrolling at the largest size.
        candidates = [c for c in range(1, min(len(cards), 4) + 1) if (width - px(16)) // c >= px(260)] or [1]
        columns = max(candidates, key=lambda c: fit(c)[0])
        _, preview_w, preview_h = fit(columns)
        preview_h = max(px(110), preview_h)
        for column in range(4):
            self.grid_frame.columnconfigure(column, weight=1 if column < columns else 0, uniform='tile' if column < columns else '')
        for index, card in enumerate(cards):
            card.frame.grid(row=index // columns, column=index % columns, sticky='nsew', padx=gap // 2, pady=gap // 2)
            card.set_preview_size(preview_w, preview_h)
        self.columns = columns

    def select_card(self, card):
        previous, self.selected_card = self.selected_card, card
        for other in self.cards.values():
            other.controls.pack_forget()
        if card is None:
            self.drawer.pack_forget()
        else:
            card.controls.pack(fill='both', expand=True)
            self.drawer_title.configure(text=card.display_name())
            if not self.drawer.winfo_ismapped():
                self.drawer.pack(side='right', fill='y', before=self.canvas.master)
        for other in {previous, card} - {None}:
            other.refresh_style()
        self.root.after_idle(self.layout_tiles)

    # ------------------------------------------------------------ messages
    def note(self, text, error=False):
        self.note_text.set(text)
        self.note_label.configure(fg=DANGER if error else MUTED)
        if error:
            self.last_error = text
        self.log.configure(state='normal')
        self.log.insert('end', f'{datetime.now():%H:%M:%S}  {text}\n')
        if int(self.log.index('end-1c').split('.')[0]) > 500:
            self.log.delete('1.0', '2.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def set_status(self, text, background, foreground):
        self.status_pill.configure(text=text, bg=background, fg=foreground)

    def update_summary(self):
        cards = list(self.cards.values())
        if not cards:
            text = '尚未搜索相机' if not self.scan_complete else '未发现相机'
        elif self.batch:
            ids = self.batch['ids']
            issues = [c for c in cards if c.info['serial'] in ids and c.issue]
            text = f'{len(ids)} 台录制中 · ' + (f'{len(issues)} 台需关注' if issues else '全部正常')
        else:
            text = f'已连接 {sum(c.connected for c in cards)} / {len(cards)} 台'
        self.summary_label.configure(text=text)

    def update_disk(self):
        now = time.monotonic()
        if now - self.disk_checked < 2:
            return
        self.disk_checked = now
        target = Path(self.output.get()).expanduser()
        while not target.exists() and target.parent != target:
            target = target.parent
        try:
            free = shutil.disk_usage(target).free
        except OSError:
            self.disk_label.configure(text='保存位置不可用', fg=DANGER)
            return
        text = f'保存盘剩余 {free/1e9:,.0f} GB'
        if self.batch and self.phase == 'recording':
            written = folder_size(self.batch['folder'])
            if self.written_sample:
                then, before = self.written_sample
                rate = (written - before) / max(now - then, 1e-3)
                if rate > 0:
                    hours = free / rate / 3600
                    text += f' · 写入 {rate/1e6:.1f} MB/s · 约可录 {hours:.1f} 小时' if hours < 100 else f' · 写入 {rate/1e6:.1f} MB/s'
            self.written_sample = (now, written)
        else:
            self.written_sample = None
        self.disk_label.configure(text=text, fg=WARN if free < 20e9 else MUTED)

    # ------------------------------------------------------------ state
    def selected(self):
        return [card for card in self.cards.values() if card.selected.get()]

    def refresh_controls(self):
        busy = (self.phase != 'idle' or self.closing or self.disconnecting or
                any(c.control_pending for c in self.cards.values()))
        live = any(c.worker and c.worker.is_alive() for c in self.cards.values())
        selected = self.selected()
        signature = (busy, live, self.scanning, self.phase, self.closing, self.disconnecting,
                     self.research_mode(), tuple((id(c), c.selected.get(), c.connected, c.ready) for c in self.cards.values()))
        if getattr(self, '_controls_signature', None) == signature:
            return
        self.scan_button.configure(state='normal' if not live and not busy and not self.scanning else 'disabled')
        self.scan_button.configure(text='搜索中…' if self.scanning else '搜索')
        self.connect_button.configure(state='normal' if selected and not busy and not self.scanning else 'disabled')
        self.disconnect_button.configure(state='normal' if live and not busy else 'disabled')
        can_record = bool(selected) and all(c.connected and c.ready for c in selected)
        noun = '采集' if self.research_mode() else '录像'
        self.start_button.configure(state='normal' if can_record and not busy else 'disabled', text=f'●  开始{noun}    {SHORTCUT}')
        self.stop_button.configure(state='normal' if self.phase in ('preparing', 'recording') and not self.closing else 'disabled',
                                   text=f'■  停止{noun}    {SHORTCUT}')
        active = self.phase in ('preparing', 'recording', 'stopping')
        if active and self.start_button.winfo_manager():
            self.start_button.pack_forget()
            self.stop_button.pack(fill='x')
        elif not active and self.stop_button.winfo_manager():
            self.stop_button.pack_forget()
            self.start_button.pack(fill='x')
        if self.phase == 'idle' and not self.closing:
            if not self.cards:
                hint = '先搜索并连接相机'
            elif not selected:
                hint = '在相机列表中勾选要录制的相机'
            elif not can_record:
                hint = '等待所选相机连接并出现画面…' if any(c.worker for c in selected) else '点“连接所选”连接勾选的相机'
            else:
                hint = f'{len(selected)} 台相机就绪 · ' + ('研究 RAW：保存全部接收帧' if self.research_mode() else f'MP4 {self.fps.get()} FPS')
            self.session_text.configure(text=hint)
        for widget in (self.folder_entry, self.browse_button):
            widget.configure(state='disabled' if busy else 'normal')
        self.fps_box.configure(state='disabled' if busy or self.research_mode() else 'readonly')
        self.mode_box.configure(state='disabled' if busy else 'readonly')
        self.export_box.configure(state='disabled' if busy else 'readonly')
        for widget in (*self.experiment_entries, self.experiment_button):
            widget.configure(state='disabled' if busy else 'normal')
        for widget in (self.save_preset_button, self.load_preset_button):
            widget.configure(state='normal' if selected and all(c.connected for c in selected) and not busy else 'disabled')
        for card in self.cards.values():
            card.lock(busy)
        if self.cards:
            self.count_label.configure(text=f'{sum(c.connected for c in self.cards.values())} / {len(self.cards)} 已连接')
        if self.phase == 'idle':
            self.set_status('就绪' if can_record else '空闲', LINE, ACCENT if can_record else MUTED)
        self.update_summary()
        self._controls_signature = signature

    def toggle_recording(self):
        if self.phase == 'idle':
            if str(self.start_button.cget('state')) == 'normal':
                self.start_recording()
        elif self.phase in ('preparing', 'recording'):
            self.stop_recording()

    # ------------------------------------------------------------ actions
    def scan(self):
        if self.scanning or self.closing:
            return
        self.scanning, self.scan_complete = True, False
        self.note('正在搜索相机…')
        self.refresh_controls()

        def task():
            devices, notes = [], []
            try:
                self.system = self.system or (DemoSystem() if self.demo else SDKSystem())
                devices += [dict(info, kind='mindvision') for info in self.system.scan()]
            except Exception as exc:
                notes.append(f'MindVision 搜索失败：{exc}')
            zed, note = list_zed_devices(self.demo)
            devices += zed
            if note:
                notes.append(note)
            if devices or not notes:
                self.events.put(('', 'scanned', dict(devices=devices, notes=notes)))
            else:
                self.events.put(('', 'scan_failed', dict(text='；'.join(notes))))
        threading.Thread(target=task, daemon=True).start()

    def connect_selected(self):
        for card in self.selected():
            if card.worker and card.worker.is_alive():
                continue
            card.show_state('连接中…', MUTED)
            card.clear_preview('正在连接…')
            card.worker = card.create_worker()
            card.worker.start()
        self.refresh_controls()

    def release_cameras(self):
        """Ask connected cameras to close; end the processes of cameras that never connected.

        A camera still opening is blocked inside the vendor SDK (waiting for USB or for another ZED to open)
        and cannot read a close command; it has nothing to save or restore, so its process is ended now."""
        for card in self.cards.values():
            if not (card.worker and card.worker.is_alive()):
                continue
            if card.connected or not hasattr(card.worker, 'terminate'):
                card.worker.command('close')
            else:
                card.worker.terminate()
                card.show_state('已取消连接', DIM)

    def disconnect_all(self):
        self.disconnecting = True
        self.disconnect_since = time.monotonic()
        self.release_cameras()
        self.note('正在断开相机并恢复原参数…')
        self.refresh_controls()

    def end_unresponsive(self, reason):
        for card in self.cards.values():
            if card.worker and card.worker.is_alive() and hasattr(card.worker, 'terminate'):
                card.worker.terminate()
                self.note(f'{self.camera_name(card.info["serial"])} {reason}，已结束该相机进程', True)

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.output.get(), title='选择录像保存文件夹')
        if folder:
            self.output.set(folder)

    def open_folder(self):
        folder = self.last_folder or Path(self.output.get())
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except Exception as exc:
            self.note(str(exc), True)

    def fps_changed(self, event=None):
        fps = int(self.fps.get())
        for card in self.cards.values():
            if card.kind != 'mindvision':
                continue
            try:
                if float(card.exposure.get()) >= 1000/fps:
                    card.exposure.set('10' if fps == 60 else '20')
            except ValueError:
                pass
        self.note(f'已选择 {fps} FPS。开始录像时自动应用各相机参数；画面偏暗优先增加照明或开大光圈。')
        self._controls_signature = None
        self.refresh_controls()

    def research_mode(self):
        return self.mode.get().startswith('研究')

    def mode_changed(self, event=None):
        self.refresh_controls()
        self.note('研究模式保存全部接收的原始帧，不补帧、不抽帧；采集速度由相机和曝光决定，30/60 FPS 选项不适用。ZED 仍保存 SVO2。'
                  if self.research_mode() else 'MP4 模式按 30/60 FPS 选帧；原始接收帧的元数据和选帧记录也会保存。')

    def experiment_data(self):
        return dict({key: value.get().strip() for key, value in self.experiment.items()},
                    camera_positions={c.info['serial']: c.position.get().strip() for c in self.selected()})

    def edit_experiment(self):
        px = self.px
        dialog = tk.Toplevel(self.root)
        dialog.title('实验信息 · 随本批数据保存')
        dialog.configure(bg=PANEL, padx=px(16), pady=px(12))
        dialog.transient(self.root)
        dialog.grab_set()
        for row, (key, label) in enumerate((('operator', '操作者'), ('lens', '镜头 / 焦距'), ('aperture', '光圈'),
                                           ('lighting', '光源 / 照明条件'), ('notes', '备注 / 标定文件位置'))):
            tk.Label(dialog, text=label, bg=PANEL, fg=MUTED).grid(row=row, column=0, sticky='w', pady=px(6))
            ttk.Entry(dialog, textvariable=self.experiment[key], width=48).grid(row=row, column=1, padx=(px(12), 0), pady=px(6))
        tk.Label(dialog, text='镜头、光源等由你填写，不是相机自动测量值。开始录像时随本批数据保存。', bg=PANEL, fg=DIM,
                 font=(FONT, 9)).grid(row=5, column=0, columnspan=2, sticky='w', pady=(px(6), 0))
        ttk.Button(dialog, text='完成', command=dialog.destroy).grid(row=6, column=1, sticky='e', pady=(px(12), 0))

    def save_preset(self):
        try:
            cards = self.selected()
            settings = {c.info['serial']: c.settings() for c in cards}
            folder = ROOT/'presets'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            folder.mkdir(parents=True, exist_ok=False)
            write_json(folder/'preset.json', dict(schema='mvsdk-preset-group/1', app_version=APP_VERSION,
                       simulated=self.demo, cameras=list(settings), experiment=self.experiment_data()))
            for card in cards:
                card.worker.command('save_preset', folder=folder, settings=settings[card.info['serial']])
            self.note(f'正在保存所选相机参数：{folder}')
        except Exception as exc:
            self.note(str(exc), True)

    def load_preset(self):
        folder = filedialog.askdirectory(initialdir=str(ROOT/'presets'), title='选择保存的预设文件夹')
        if not folder:
            return
        try:
            for card in self.selected():
                path = Path(folder)/(safe_name(card.info['serial'])+'.preset.json')
                if not path.is_file():
                    raise ValueError(f'预设中没有相机 {card.info["serial"]}；未加载')
            for card in self.selected():
                card.worker.command('load_preset', folder=folder)
        except Exception as exc:
            self.note(str(exc), True)

    def start_recording(self):
        if self.phase != 'idle' or self.closing:
            return
        try:
            research = self.research_mode()
            if not research and any(c.kind == 'mindvision' for c in self.selected()) and not shutil.which('ffmpeg'):
                raise RuntimeError('找不到 FFmpeg：请把 FFmpeg 的 bin 文件夹加入系统 PATH，或放在程序目录的 ffmpeg\\bin 下，然后重开程序')
            cards = self.selected()
            if not cards or not all(c.connected and c.ready for c in cards):
                raise ValueError('请先连接所选相机，等待预览画面出现')
            fps = int(self.fps.get())
            settings = {c.info['serial']: c.settings() for c in cards}
            for value in settings.values():
                value.validate(None if research else fps)
            experiment = self.experiment_data()
            if research and not experiment['experiment_id']:
                raise ValueError('研究模式请填写实验编号，方便追溯数据')
            names = [safe_name(c.info['serial']).lower() for c in cards]
            if len(set(names)) != len(names):
                raise ValueError('相机序列号无法生成唯一文件名')
            folder = Path(self.output.get()).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(folder).free < 256 * 1024**2:
                raise RuntimeError('保存盘可用空间不足 256 MB，请更换目录')
            folder /= ('DEMO_' if self.demo else '') + datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            folder.mkdir(exist_ok=False)
            self.last_folder = folder
            gate = threading.Event()
            ids = set(settings)
            camera_folders = {c.info['serial']: folder / f'{c.kind}_{safe_name(c.info["serial"])}' for c in cards}
            for camera_folder in camera_folders.values():
                camera_folder.mkdir()
            self.batch = dict(folder=folder, ids=ids, waiting=set(ids), prepared=set(), gate=gate, camera_folders=camera_folders,
                              exports={}, summaries={}, fps=None if research else fps, started=None, errors=[],
                              mode='raw' if research else 'mp4', experiment=experiment, software=software_info(),
                              requested_at=datetime.now().astimezone().isoformat())
            self.batch['export_depth'] = EXPORT_CHOICES[self.export_choice.get()]
            if self.exports.pause():
                self.note('ZED 导出已暂停，避免与录像争用显卡；本批录完后从头继续导出')
            self.phase = 'preparing'
            self.batch['deadline'] = time.monotonic() + self.PREPARE_TIMEOUT_S
            self.write_manifest()
            for card in cards:
                card.issue = None
                card.show_state('准备中…', ACCENT)
                card.record_stats.configure(text='', fg=ACCENT)
                card.worker.command('prepare', folder=camera_folders[card.info['serial']], fps=fps,
                                    settings=settings[card.info['serial']], gate=gate,
                                    mode=self.batch['mode'], experiment=experiment, **card.prepare_extra())
            self.set_status('准备中', LINE, ACCENT)
            self.session_text.configure(text=f'正在准备 {len(cards)} 台相机…')
            estimated = sum(c.last_fps*c.raw_size for c in cards)/1e6
            self.note(f'本批保存到：{folder}' + (f'；预览估算 RAW {estimated:.1f} MB/s（非写盘性能保证）' if research else ''))
        except Exception as exc:
            self.note(str(exc), True)
            if self.phase == 'preparing':
                self.stop_recording()
        self.refresh_controls()

    def stop_recording(self, reason=None):
        if self.phase not in ('preparing', 'recording'):
            return
        self.phase = 'stopping'
        self.batch['deadline'] = time.monotonic() + self.STOP_TIMEOUT_S
        if reason:
            self.batch['errors'].append(reason)
            self.note(reason, True)
        for serial in self.batch['waiting']:
            card = self.cards[serial]
            if card.worker.is_alive():
                card.worker.command('stop')
        self.set_status('正在保存', '#3a3424', WARN)
        self.session_text.configure(text='正在结束录像并保存文件，请稍候…')
        self.refresh_controls()

    def camera_name(self, serial):
        card = self.cards.get(serial)
        return f'{card.display_name()}（{serial}）' if card and card.display_name() != card.info['model'] else serial

    def check_stalls(self):
        """A connected camera that stops reporting is usually stuck in vendor-SDK USB recovery: say so plainly."""
        now = time.monotonic()
        for serial, card in self.cards.items():
            if not card.connected or getattr(card, 'stalled', False) or not getattr(card, 'last_alive', None):
                continue
            if now - card.last_alive > self.NO_IMAGE_WARN_S:
                card.stalled = True
                card.issue = '无画面'
                card.show_state('无画面', DANGER)
                card.clear_preview(f'{self.NO_IMAGE_WARN_S} 秒没有画面\nUSB 连接中断或带宽不足，相机正在重连')
                self.note(f'{self.camera_name(serial)} 已 {self.NO_IMAGE_WARN_S} 秒没有画面：通常是 USB 连接中断或带宽不足'
                          '（ZED 日志会出现 “Connection Failure”）。请把它换到独立的 USB 通道，或减少同一控制器上的相机。', True)

    def check_deadlines(self):
        """Give up on cameras that stop answering instead of leaving the batch stuck in preparing / stopping."""
        if not self.batch or time.monotonic() < self.batch.get('deadline', float('inf')):
            return
        if self.phase == 'preparing':
            late = sorted(self.batch['ids'] - self.batch['prepared'])
            for serial in late:
                self.cards[serial].issue = '准备超时'
                self.cards[serial].show_state('准备超时', DANGER)
            self.stop_recording(f'{"、".join(map(self.camera_name, late))} 在 {self.PREPARE_TIMEOUT_S} 秒内没有准备好，已取消本批。'
                                '常见原因是 USB 连接不稳定或带宽不够（ZED 日志里会有“Connection Failure”）')
        elif self.phase == 'stopping':
            for serial in sorted(self.batch['waiting']):
                card = self.cards[serial]
                if hasattr(card.worker, 'terminate'):
                    card.worker.terminate()  # its log stays in logs\; the recorder cannot release a camera stuck inside the SDK
                card.show_state('无响应，已结束', DANGER)
                self.batch['summaries'][serial] = dict(complete=False, errors=[
                    f'{self.STOP_TIMEOUT_S} 秒内没有响应停止命令，已结束该相机进程；文件可能不完整'])
                self.note(f'{self.camera_name(serial)} 无响应，已结束该相机进程', True)
            self.batch['waiting'].clear()
            self.finish_batch_if_ready()

    def write_manifest(self):
        batch = self.batch
        data = dict(simulated=self.demo, fps=batch['fps'], requested_at=batch['requested_at'],
                    phase=self.phase, cameras=sorted(batch['ids']), errors=batch['errors'],
                    recordings=batch['summaries'], mode=batch['mode'], experiment=batch['experiment'], software=batch['software'],
                    camera_folders={serial: path.name for serial, path in batch.get('camera_folders', {}).items()},
                    exports=batch.get('exports', {}),
                    layout='one subfolder per camera: <kind>_<serial>/; ZED exports in <kind>_<serial>/export/')
        target = batch['folder'] / 'batch.json'
        temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(target)

    def finish_batch_if_ready(self):
        if not self.batch or self.batch['waiting']:
            return
        summaries = self.batch['summaries']
        failed = bool(self.batch['errors']) or any(not s or not s.get('complete') for s in summaries.values())
        repeats = sum(s.get('repeated_frames', 0) for s in summaries.values() if s)
        review = any(s and s.get('integrity', {}).get('status') in ('review', 'failed') for s in summaries.values())
        self.phase = 'idle'
        jobs = []
        for serial, summary in summaries.items():
            video = Path(summary.get('video', '')) if summary else None
            if summary and summary.get('kind') == 'zed' and summary.get('written_frames') and video.is_file():
                card = self.cards.get(serial)
                depth = self.batch.get('export_depth')
                jobs.append(dict(serial=serial, name=card.display_name() if card else serial, svo=str(video), depth=depth,
                                 batch_folder=str(self.batch['folder']), simulated=bool(summary.get('simulated'))))
                self.batch['exports'][serial] = dict(status='queued', depth=depth or 'none')
        try:
            self.write_manifest()
        except Exception as exc:
            failed = True
            self.note(f'批次信息保存失败：{exc}', True)
        for job in jobs:
            self.exports.add(job)
        self.exports.resume()
        self.completed_batches.append(self.batch)
        if repeats:
            self.note(f'MP4 按时间戳选帧时有空缺，共重复 {repeats} 帧以保持时长；这不是相机丢帧数量。录制期间接收帧率和处理耗时见每台相机的 JSON。', True)
        elif review and not failed:
            self.note(f'数据已保存，但完整性报告有待核查项；请查看 {self.batch["folder"]} 中的 integrity 报告。', True)
        elif not failed:
            self.note(f'录像已保存：{self.batch["folder"]}')
        self.show_results(self.batch, failed, review)
        self.batch = None
        self._controls_signature = None
        for card in self.cards.values():
            card.recording = False
            if card.connected:  # live warnings end with the batch; a lost connection stays marked until reconnect
                card.issue = None
            card.refresh_style()
        self.refresh_controls()
        self.session_text.configure(text='本批存在异常，详见结果窗口' if failed else
                                    f'已保存 {len(summaries)} 路数据' + (' · 完整性待核查' if review else ''))

    def show_results(self, batch, failed, review):
        """Non-modal summary of the last batch: one row per camera."""
        px = self.px
        if self.result_window is not None and self.result_window.winfo_exists():
            self.result_window.destroy()
        window = self.result_window = tk.Toplevel(self.root)
        window.title('本批录像结果')
        window.configure(bg=PANEL, padx=px(18), pady=px(14))
        window.transient(self.root)
        headline = '本批存在异常' if failed else '录像已保存，有待核查项' if review else '录像已保存，检查通过'
        tk.Label(window, text=headline, bg=PANEL, fg=DANGER if failed else WARN if review else ACCENT,
                 font=(FONT, 14, 'bold'), anchor='w').pack(fill='x')
        tk.Label(window, text=str(batch['folder']), bg=PANEL, fg=DIM, font=(FONT, 9), anchor='w').pack(fill='x', pady=(px(2), px(10)))
        table = tk.Frame(window, bg=PANEL)
        table.pack(fill='x')
        for column, title in enumerate(('相机', '类型', '帧数', '缺帧 / 补帧', '结果')):
            tk.Label(table, text=title, bg=PANEL, fg=DIM, font=(FONT, 9)).grid(row=0, column=column, sticky='w', padx=(0, px(18)))
        labels = dict(pass_observed=('通过', ACCENT), review=('待核查', WARN), failed=('失败', DANGER))
        for row, (serial, summary) in enumerate(sorted(batch['summaries'].items()), start=1):
            card = self.cards.get(serial)
            summary = summary or {}
            integrity = summary.get('integrity') or {}
            status, color = labels.get(integrity.get('status'), ('失败' if not summary.get('complete') else '—', DANGER))
            if summary.get('kind') == 'zed':
                losses = f'相机 {integrity.get("camera_missing_frames", 0)} · 程序 {integrity.get("app_missing_frames", 0)}'
            else:
                losses = f'补帧 {summary.get("repeated_frames", 0)}'
            values = (card.display_name() if card else serial, card.type_label if card else '', summary.get('written_frames', 0), losses, status)
            for column, value in enumerate(values):
                tk.Label(table, text=value, bg=PANEL, fg=color if column == 4 else TEXT, font=(FONT, 10)).grid(
                    row=row, column=column, sticky='w', padx=(0, px(18)), pady=px(2))
            for error in summary.get('errors', [])[:2]:
                tk.Label(table, text='  ' + error, bg=PANEL, fg=DANGER, font=(FONT, 9), wraplength=px(560), justify='left').grid(
                    row=row, column=5, sticky='w')
        tk.Label(window, text='每台相机的 integrity 报告区分相机缺帧、程序缺帧与 MP4 补帧。', bg=PANEL, fg=DIM,
                 font=(FONT, 9), anchor='w').pack(fill='x', pady=(px(12), 0))
        if batch.get('exports'):
            depth = batch.get('export_depth')
            tk.Label(window, text=f'ZED 正在后台导出{"左右目与深度（" + depth + "）" if depth else "左右目"}视频，进度见窗口底部；'
                                  '完成后在各 ZED 文件夹的 export 子文件夹中。', bg=PANEL, fg=ACCENT, font=(FONT, 9), anchor='w',
                     justify='left', wraplength=px(640)).pack(fill='x', pady=(px(4), 0))
        buttons = tk.Frame(window, bg=PANEL)
        buttons.pack(fill='x', pady=(px(12), 0))
        ttk.Button(buttons, text='关闭', command=window.destroy).pack(side='right')
        ttk.Button(buttons, text='打开目录', command=self.open_folder).pack(side='right', padx=px(8))

    # ------------------------------------------------------------ events
    def handle_event(self, serial, kind, data):
        if kind == 'scanned':
            self.scanning = False
            self.scan_complete = True
            self.select_card(None)
            for card in self.cards.values():
                if card.advanced_panel:
                    card.advanced_panel.close()
                if card.focus_window:
                    card.focus_window.close()
                card.destroy()
            self.cards.clear()
            devices = data['devices']
            if devices:
                self.empty.grid_remove()
                self.device_hint.pack_forget()
            else:
                self.empty.grid()
                self.device_hint.pack(fill='x')
            for index, info in enumerate(devices):
                if not info['serial'] or info['serial'] in self.cards:
                    self.note('发现缺失或重复的相机序列号，已略过该设备', True)
                    continue
                self.cards[info['serial']] = (ZedCard if info.get('kind') == 'zed' else CameraCard)(self, info, index)
            self.layout_tiles()
            self.count_label.configure(text=f'发现 {len(self.cards)} 台')
            zed = sum(c.kind == 'zed' for c in self.cards.values())
            self.note((f'发现 {len(self.cards)} 台相机（ZED {zed} 台）。勾选后点击“连接所选”。' if self.cards else
                       '未发现相机，请检查电源、USB 连接和驱动。') + ''.join('  ' + n for n in data.get('notes', [])))
            self._controls_signature = None
            return
        if kind == 'scan_failed':
            self.scanning = False
            self.note('搜索失败：'+data['text'], True)
            return
        if kind.startswith('export_'):
            self.handle_export(kind, data)
            return
        card = self.cards.get(serial)
        if not card:
            return
        if kind == 'connected':
            card.connected = True
            card.issue = None
            card.last_alive = time.monotonic()
            card.stalled = False
            card.on_connected(data)
            card.show_state('预览中', ACCENT)
            card.clear_preview('等待画面…')
            self.note(f'{serial} 已连接。点画面可调参数，确认画面后开始录像。')
            if self.closing:
                card.worker.command('close')
        elif kind == 'settings':
            card.set_settings(data['settings'])
        elif kind in ('advanced', 'advanced_failed'):
            card.control_pending = False
            if card.advanced_panel:
                card.advanced_panel.receive(kind, data)
        elif kind == 'stats':
            card.last_alive = time.monotonic()
            if getattr(card, 'stalled', False):
                card.stalled = False
                card.issue = None
                card.show_state('● 录像中' if card.recording else '预览中', DANGER if card.recording else ACCENT)
                self.note(f'{self.camera_name(serial)} 画面已恢复')
            card.last_fps = data['fps']
            card.raw_size = data.get('raw_bytes') or card.raw_size
            shape = data['shape']
            card.ready = shape is not None
            card.stats.configure(text=f'{data["fps"]:.1f} FPS · {shape[0]}×{shape[1]}' if shape else '等待图像…')
            if self.batch and serial in self.batch['ids']:
                lost = data.get('missing', data.get('repeats', 0))
                label = '缺帧' if 'missing' in data else '补帧'
                card.issue = f'{label} {lost}' if lost else None
                card.record_stats.configure(text=f'● {data["seconds"]:.1f} s  ·  {data["frames"]} 帧  ·  {label} {lost}',
                                            fg=WARN if lost else ACCENT)
                card.row_info.configure(text=f'{label} {lost}' if lost else f'{data["frames"]} 帧', fg=WARN if lost else DIM)
                card.refresh_style()
        elif kind == 'prepared':
            if self.batch and self.phase == 'preparing':
                self.batch['prepared'].add(serial)
                if self.batch['prepared'] == self.batch['ids']:
                    self.phase = 'recording'
                    self.batch['started'] = time.monotonic()
                    self.batch['gate'].set()
                    self.write_manifest()
        elif kind == 'prepare_failed':
            self.note(f'{serial} 无法开始录像：{data["text"]}', True)
            self.stop_recording(f'{serial}：{data["text"]}')
        elif kind == 'record_started':
            card.recording = True
            card.show_state('● 录像中', DANGER)
        elif kind == 'record_finished':
            card.recording = False
            card.show_state('预览中' if card.connected else '未连接', ACCENT if card.connected else DIM)
            summary = data['summary']
            if summary:
                # The result stays as text under the tile (yellow when frames were lost); the border is for live state only.
                if card.kind == 'zed':
                    integrity = summary.get('integrity') or {}
                    lost = (integrity.get('camera_missing_frames') or 0) + (integrity.get('app_missing_frames') or 0)
                    card.record_stats.configure(text=f'上次：已保存 {summary.get("written_frames", 0)} 帧 · 相机缺帧 {integrity.get("camera_missing_frames", 0)}'
                                                     f' · 程序缺帧 {integrity.get("app_missing_frames", 0)}', fg=WARN if lost else ACCENT)
                else:
                    lost = summary.get('repeated_frames', 0)
                    card.record_stats.configure(text=f'上次：已保存 {summary.get("written_frames", 0)} 帧 · 补帧 {lost}',
                                                fg=WARN if lost else ACCENT)
                if not summary.get('complete'):
                    card.record_stats.configure(fg=DANGER)
                card.row_info.configure(text=card.type_label, fg=DIM)
                for error in summary.get('errors', []):
                    self.note(f'{serial}：{error}', True)
                if summary.get('errors') and self.phase in ('recording', 'preparing'):
                    self.stop_recording(f'{serial} 录制失败，已停止本批其他相机')
            if self.batch and serial in self.batch['waiting']:
                self.batch['waiting'].discard(serial)
                self.batch['summaries'][serial] = summary
                self.finish_batch_if_ready()
        elif kind == 'fault':
            card.ready = False
            card.issue = '连接异常'
            card.show_state('连接异常', DANGER)
            self.note(f'{serial}：{data["text"]}', True)
            if self.batch and serial in self.batch['ids']:
                self.stop_recording(f'{serial} 连接异常，已停止本批录像')
        elif kind == 'closed':
            if card.focus_window:
                card.focus_window.close()
            card.control_pending = False
            card.connected = card.ready = card.recording = False
            card.show_state('已断开', DIM)
            card.stats.configure(text='相机已断开')
            card.clear_preview('已断开')
            if self.batch and serial in self.batch['waiting']:
                self.batch['waiting'].discard(serial)
                self.batch['summaries'][serial] = dict(complete=False, errors=['相机已断开'])
                self.finish_batch_if_ready()
        elif kind in ('notice', 'command_error'):
            self.note(f'{serial}：{data["text"]}', kind == 'command_error')

    def handle_export(self, kind, data):
        job = data['job']
        progress = self.__dict__.setdefault('export_progress', {})
        if kind == 'export_progress':
            done, total = data.get('done') or 0, data.get('total')
            if total:
                progress[job['key']] = f'{job["name"]} {done * 100 // total}%'
            elif job.get('depth'):
                progress[job['key']] = f'{job["name"]} 准备中（首次使用 {job["depth"]} 需下载并优化模型，约 20–30 分钟）'
            else:
                progress[job['key']] = f'{job["name"]} 准备中'
        else:
            progress.pop(job['key'], None)
        if kind == 'export_paused':
            self.export_label.configure(text='ZED 导出已暂停，录完后继续', fg=WARN)
            return
        if kind == 'export_finished':
            result = data['result']
            if result.get('status') == 'completed':
                repeats = f'，相机缺帧处补了 {result["repeated_frames"]} 帧（CSV 已标记）' if result.get('repeated_frames') else ''
                self.note(f'{job["name"]} 导出完成：{result.get("output_frames")} 帧{repeats}。{result.get("folder")}')
            else:
                self.note(f'{job["name"]} 导出失败：{result.get("error")}', True)
        waiting = self.exports.pending() - len(progress)
        text = ('导出 ' + ' · '.join(progress.values())) if progress else ''
        if waiting > 0:
            text += f'{" · " if text else ""}排队 {waiting} 个'
        self.export_label.configure(text=text, fg=ACCENT)

    def poll(self):
        try:
            while True:
                serial, kind, data = self.events.get_nowait()
                self.handle_event(serial, kind, data)
        except queue.Empty:
            pass
        except Exception as exc:
            self.note(f'界面处理失败：{exc}', True)
            self.stop_recording(str(exc))
        for card in self.cards.values():
            card.update_preview()
        live = any(c.worker and c.worker.is_alive() for c in self.cards.values())
        if self.disconnecting and not live:
            self.disconnecting = False
        if self.disconnecting and live and time.monotonic() - self.disconnect_since > self.DISCONNECT_TIMEOUT_S:
            self.end_unresponsive(f'{self.DISCONNECT_TIMEOUT_S} 秒内没有完成断开')
            self.disconnect_since = time.monotonic()  # worker threads exit within a few seconds after the kill
        close_limit = self.CLOSE_TIMEOUT_S if self.batch else self.DISCONNECT_TIMEOUT_S
        if self.closing and live and time.monotonic() - self.closing_since > close_limit:
            self.end_unresponsive('退出时没有响应')
            self.closing_since = time.monotonic()
            live = any(c.worker and c.worker.is_alive() and not hasattr(c.worker, 'terminate') for c in self.cards.values())
        if self.closing and not live and not self.scanning:
            self.root.destroy()
            return
        self.check_deadlines()
        self.check_stalls()
        if self.phase == 'recording':
            elapsed = int(time.monotonic() - self.batch['started'])
            label = 'RAW 全帧' if self.batch['mode'] == 'raw' else f'{self.batch["fps"]} FPS'
            clock = f'{elapsed//3600:d}:{elapsed//60%60:02d}:{elapsed%60:02d}' if elapsed >= 3600 else f'{elapsed//60:02d}:{elapsed%60:02d}'
            self.set_status(f'● 录像中  {clock}', '#4a1f26', DANGER)
            self.session_text.configure(text=f'{len(self.batch["ids"])} 台采集中 · {label} · 再按 {SHORTCUT} 停止')
        self.update_disk()
        self.refresh_controls()
        if self.batch:
            self.update_summary()
        self.root.after(80, self.poll)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.closing_since = time.monotonic()
        if self.exports.busy or self.exports.pending():
            self.note('退出时停止了未完成的 ZED 导出；SVO2 原始文件完整，可用 zed_export.py 重新导出', True)
        self.exports.close()
        for card in self.cards.values():
            if card.focus_window:
                card.focus_window.close()
        self.stop_recording()
        self.release_cameras()
        self.set_status('正在退出', LINE, MUTED)
        self.session_text.configure(text='正在保存录像、断开相机并退出…')
        self.refresh_controls()


def folder_size(folder):
    total = 0
    try:
        for entry in os.scandir(folder):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
            elif entry.is_dir(follow_symlinks=False):
                total += folder_size(entry.path)
    except OSError:
        pass
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', help='Simulated cameras; does not load the camera SDKs')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    enable_dpi_awareness()
    root = tk.Tk()
    RecorderApp(root, args.demo, args.output)
    root.mainloop()


if __name__ == '__main__':
    main()
