"""Full-resolution live focus viewer. Zoom changes display only, never capture."""
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk


class FocusPreview:
    def __init__(self, card):
        self.card = card
        self.worker = card.worker
        self.frame = None
        self.photo = None
        self.center = (0.5, 0.5)
        self.scale = 1.0
        self.drag = None
        self.window = tk.Toplevel(card.app.root)
        self.window.title('放大对焦 · '+card.info['serial'])
        self.window.geometry('1100x780')
        self.window.minsize(640, 400)
        self.window.configure(bg='#101820')
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        bar = tk.Frame(self.window, bg='#101820', padx=12, pady=10)
        bar.pack(fill='x')
        self.mode = tk.StringVar(value='100% 原始像素')
        self.zoom = ttk.Combobox(bar, textvariable=self.mode, state='readonly', width=18,
                                values=('适应窗口', '100% 原始像素', '200%', '400%'))
        self.zoom.pack(side='left')
        self.zoom.bind('<<ComboboxSelected>>', lambda event: self.render())
        ttk.Button(bar, text='回到中心', command=self.recenter).pack(side='left', padx=8)
        ttk.Button(bar, text='全屏 / F11', command=self.fullscreen).pack(side='right')
        tk.Label(self.window, text='拖动画面查看局部 · 滚轮缩放 · Esc 退出全屏 · 仅放大显示，不裁剪录像',
                 bg='#101820', fg='#9aafbd').pack(anchor='w', padx=12, pady=(0, 8))
        self.canvas = tk.Canvas(self.window, bg='#080d12', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.item = self.canvas.create_image(0, 0, anchor='nw')
        self.status = tk.StringVar(value='等待原始分辨率预览…')
        tk.Label(self.window, textvariable=self.status, bg='#101820', fg='#4ed4b1', anchor='w').pack(fill='x', padx=12, pady=8)
        self.canvas.bind('<Configure>', lambda event: self.render())
        self.canvas.bind('<ButtonPress-1>', self.begin_drag)
        self.canvas.bind('<B1-Motion>', self.pan)
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.window.bind('<F11>', self.fullscreen)
        self.window.bind('<Escape>', lambda event: self.window.attributes('-fullscreen', False))
        self.worker.full_preview.set()

    def close(self):
        self.worker.full_preview.clear()
        self.card.focus_window = None
        self.frame = self.photo = None
        self.window.destroy()

    def fullscreen(self, event=None):
        self.window.attributes('-fullscreen', not self.window.attributes('-fullscreen'))

    def recenter(self):
        self.center = (0.5, 0.5)
        self.render()

    def begin_drag(self, event):
        self.drag = (event.x, event.y, self.center)

    def pan(self, event):
        if self.frame is None or self.drag is None or self.mode.get() == '适应窗口':
            return
        x, y, center = self.drag
        w, h = self.frame.size
        self.center = (center[0]-(event.x-x)/self.scale/w, center[1]-(event.y-y)/self.scale/h)
        self.render()

    def wheel(self, event):
        choices = list(self.zoom['values'])
        index = choices.index(self.mode.get())
        self.mode.set(choices[max(0, min(len(choices)-1, index+(1 if event.delta > 0 else -1)))])
        self.render()
        return 'break'

    def update(self, frame):
        self.frame = frame
        self.render()

    def render(self):
        if self.frame is None:
            return
        width, height = self.frame.size
        vw, vh = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        mode = self.mode.get()
        if mode == '适应窗口':
            self.scale = min(vw/width, vh/height)
            shown = self.frame.resize((max(1, round(width*self.scale)), max(1, round(height*self.scale))), Image.Resampling.BILINEAR)
        else:
            self.scale = {'100% 原始像素': 1, '200%': 2, '400%': 4}[mode]
            cw, ch = min(width, max(1, vw//self.scale)), min(height, max(1, vh//self.scale))
            left = max(0, min(width-cw, round(self.center[0]*width-cw/2)))
            top = max(0, min(height-ch, round(self.center[1]*height-ch/2)))
            self.center = ((left+cw/2)/width, (top+ch/2)/height)
            shown = self.frame.crop((left, top, left+cw, top+ch))
            if self.scale != 1:
                shown = shown.resize((cw*self.scale, ch*self.scale), Image.Resampling.NEAREST)
        self.photo = ImageTk.PhotoImage(shown)
        self.canvas.itemconfigure(self.item, image=self.photo)
        self.canvas.coords(self.item, max(0, (vw-shown.width)//2), max(0, (vh-shown.height)//2))
        self.status.set(f'源画面 {width} × {height}  ·  显示 {self.scale*100:.0f}%  ·  实时预览约 10 FPS')
