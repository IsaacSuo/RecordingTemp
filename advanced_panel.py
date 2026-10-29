"""Nonmodal per-camera advanced controls, populated from SDK readback."""
import tkinter as tk
from tkinter import ttk
import uuid


class AdvancedPanel:
    def __init__(self, card):
        self.card = card
        self.app = card.app
        self.window = tk.Toplevel(self.app.root)
        self.window.title('高级参数 · '+card.info['serial'])
        self.window.geometry('860x680')
        self.window.configure(bg='#101820')
        self.window.transient(self.app.root)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.fields, self.variables, self.widgets, self.choices = {}, {}, {}, {}
        self.pending = None
        self.dirty = set()
        self.token = uuid.uuid4().hex
        self.counter = 0
        self.status = tk.StringVar(value='正在读取相机参数…')
        tk.Label(self.window, text='高级参数', bg='#101820', fg='#edf3f7', font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w', padx=20, pady=(16, 6))
        tk.Label(self.window, text='方向选择立即应用；其他修改点击“应用修改”，随后显示实际读回值。',
                 bg='#101820', fg='#9aafbd').pack(anchor='w', padx=20, pady=(0, 12))
        self.notebook = ttk.Notebook(self.window)
        self.notebook.pack(fill='both', expand=True, padx=20)
        self.tabs = {}
        self.canvases = {}
        for name in ('曝光与方向', '色彩与亮度', '处理与速度'):
            outer = tk.Frame(self.notebook, bg='#192631')
            self.notebook.add(outer, text=name)
            canvas = tk.Canvas(outer, bg='#192631', highlightthickness=0)
            scrollbar = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
            canvas.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side='right', fill='y')
            canvas.pack(side='left', fill='both', expand=True)
            tab = tk.Frame(canvas, bg='#192631', padx=16, pady=10)
            window_id = canvas.create_window((0, 0), window=tab, anchor='nw')
            tab.bind('<Configure>', lambda event, c=canvas: c.configure(scrollregion=c.bbox('all')))
            canvas.bind('<Configure>', lambda event, c=canvas, w=window_id: c.itemconfigure(w, width=event.width))
            self.tabs[name] = tab
            self.canvases[name] = canvas
        self.window.bind('<MouseWheel>', self.scroll)
        self.note = tk.Label(self.window, text=card.advanced_note,
                            bg='#101820', fg='#9aafbd', justify='left', wraplength=810)
        self.note.pack(fill='x', padx=20, pady=12)
        tk.Label(self.window, textvariable=self.status, bg='#101820', fg='#4ed4b1', anchor='w',
                 wraplength=810, justify='left').pack(fill='x', padx=20)
        buttons = tk.Frame(self.window, bg='#101820')
        buttons.pack(fill='x', padx=20, pady=15)
        self.apply_button = ttk.Button(buttons, text='应用修改', command=self.apply)
        self.apply_button.pack(side='right')
        self.refresh_button = ttk.Button(buttons, text='重新读取', command=self.refresh)
        self.refresh_button.pack(side='right', padx=10)
        self.wb_button = ttk.Button(buttons, text='一次白平衡', command=self.white_balance)
        self.wb_button.pack(side='left')
        self.refresh()

    def exists(self):
        return bool(self.window.winfo_exists())

    def scroll(self, event):
        name = self.notebook.tab(self.notebook.select(), 'text')
        self.canvases[name].yview_scroll(-int(event.delta/120), 'units')
        return 'break'

    def close(self):
        self.card.advanced_panel = None
        self.window.destroy()

    def request(self, command, **args):
        self.counter += 1
        self.pending = f'{self.token}:{self.counter}'
        self.card.control_pending = True
        self.card.worker.command(command, request=self.pending, **args)
        self.sync_state()

    def refresh(self):
        self.status.set('正在读取…未应用的编辑将被读回值替换')
        self.request('advanced_get')

    def white_balance(self):
        self.status.set('白平衡已请求，等待下一张处理后的图像再读取参数…')
        self.request('wb')

    def read_value(self, key):
        spec, variable = self.fields[key], self.variables[key]
        if spec['kind'] == 'choice':
            return self.choices[key][variable.get()]
        if spec['kind'] == 'bool':
            return bool(variable.get())
        return float(variable.get()) if spec['kind'] == 'float' else int(variable.get())

    def changed(self, key, immediate=False):
        self.dirty.add(key)
        if immediate:
            self.apply(keys={key})
        else:
            self.sync_state()

    def apply(self, keys=None):
        try:
            selected = self.dirty if keys is None else keys
            changes = {key: self.read_value(key) for key in selected if self.fields[key]['supported']}
            if not changes:
                self.status.set('没有待应用的修改')
                return
            self.submitted = set(changes)
            self.status.set('正在应用并读回…')
            self.request('advanced_apply', changes=changes)
        except Exception as exc:
            self.status.set('输入有误：'+str(exc))

    def receive(self, kind, data):
        if data.get('request') != self.pending:
            return
        self.pending = None
        if kind == 'advanced_failed':
            self.status.set('未应用：'+data['text'])
            self.sync_state()
            return
        keep = {key: self.variables[key].get() for key in self.dirty-self.submitted} if data.get('applied') else {}
        self.fields = data['controls']
        for tab in self.tabs.values():
            for child in tab.winfo_children():
                child.destroy()
        self.variables, self.widgets, self.choices = {}, {}, {}
        rows = {name: 0 for name in self.tabs}
        for key, spec in self.fields.items():
            tab = self.tabs[spec['group']]
            row = rows[spec['group']]
            rows[spec['group']] += 1
            tk.Label(tab, text=spec['label'], bg='#192631', fg='#edf3f7', anchor='w').grid(row=row, column=0, sticky='w', pady=4)
            value = spec['value']
            if spec['kind'] == 'bool':
                variable = tk.BooleanVar(value=bool(value))
                widget = tk.Checkbutton(tab, variable=variable, text='启用', bg='#192631', fg='#edf3f7', selectcolor='#101820',
                                        activebackground='#192631', command=lambda k=key: self.changed(k, k.startswith('mirror_')))
            elif spec['kind'] == 'choice':
                choices = {str(label): val for val, label in (spec['choices'] or [])}
                self.choices[key] = choices
                selected = next((label for label, val in choices.items() if val == value), '')
                variable = tk.StringVar(value=selected)
                widget = ttk.Combobox(tab, textvariable=variable, values=list(choices), state='readonly', width=28)
                widget.bind('<<ComboboxSelected>>', lambda event, k=key: self.changed(k, k == 'rotate180'))
            else:
                variable = tk.StringVar(value='' if value is None else str(value))
                widget = ttk.Entry(tab, textvariable=variable, width=30)
                widget.bind('<KeyRelease>', lambda event, k=key: self.changed(k))
            widget.grid(row=row, column=1, sticky='w', padx=16, pady=4)
            self.variables[key], self.widgets[key] = variable, widget
            if not spec['supported']:
                hint = '不可用：'+str(spec['error'])
            elif spec['min'] is not None:
                hint = f'{spec["min"]:g} – {spec["max"]:g}'
            else:
                hint = ''
            tk.Label(tab, text=hint, bg='#192631', fg='#9aafbd', anchor='w', wraplength=260).grid(row=row, column=2, sticky='w', pady=4)
        self.dirty = set(keep)
        for key, value in keep.items():
            self.variables[key].set(value)
        self.status.set('已应用并读回实际值' if data.get('applied') else '已读取当前值；不可用项已禁用')
        if data.get('readback_differences'):
            self.status.set('已读回；部分值与输入不同（含设备量化），请以当前显示值为准：'+', '.join(data['readback_differences']))
        if self.app.demo:
            self.status.set(self.status.get()+'（模拟：色彩参数仅验证读写，非真实 ISP 效果）')
        self.sync_state()

    def sync_state(self):
        locked = self.pending is not None or self.app.phase != 'idle' or not self.card.connected or self.app.closing or self.app.disconnecting
        for button in (self.apply_button, self.refresh_button, self.wb_button):
            button.configure(state='disabled' if locked else 'normal')
        for key, widget in self.widgets.items():
            disabled = locked or not self.fields[key]['supported']
            lut = self.read_value('lut_mode') if 'lut_mode' in self.choices and self.variables['lut_mode'].get() else None
            if key in ('gamma', 'contrast') and lut != 0:
                disabled = True
            if key == 'lut_preset' and lut != 1:
                disabled = True
            widget.configure(state='disabled' if disabled else 'readonly' if self.fields[key]['kind'] == 'choice' else 'normal')
