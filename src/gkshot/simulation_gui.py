"""Independent comparison window; all engine work runs outside Tk's event loop."""
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from gkshot.app_paths import USER_ROOT
from gkshot.catalog import PROJECT_ROOT
from gkshot.models import Loadout
from gkshot.simulation import atomic_json, compare


class SimulationWindow:
    def __init__(self, root):
        self.root = root
        self.paths = []
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.report = None
        root.title('GKTools 本地模拟 · 单编成 / 候选比较')
        root.geometry('1000x620')
        body = ttk.Frame(root, padding=14)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='添加完整 Loadout JSON；同一次比较必须使用相同场地。', font=('', 12)).pack(anchor='w')
        ttk.Label(body, text='主副记忆可先用 simulate.py compose 合成；未知强化不能作为空强化运行。').pack(anchor='w', pady=(3, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill='x')
        self.add_button = ttk.Button(buttons, text='添加编成 JSON', command=self.add)
        self.add_button.pack(side='left')
        self.clear_button = ttk.Button(buttons, text='清空候选', command=self.clear)
        self.clear_button.pack(side='left', padx=8)
        self.files = tk.Listbox(body, height=6)
        self.files.pack(fill='x', pady=10)
        options = ttk.Frame(body)
        options.pack(fill='x')
        self.trials = tk.StringVar(value='1000')
        self.seed = tk.StringVar(value='610397104')
        ttk.Label(options, text='每候选次数').pack(side='left')
        ttk.Entry(options, textvariable=self.trials, width=9).pack(side='left', padx=8)
        ttk.Label(options, text='随机种子').pack(side='left')
        ttk.Entry(options, textvariable=self.seed, width=14).pack(side='left', padx=8)
        self.run_button = ttk.Button(options, text='开始模拟', command=self.start)
        self.run_button.pack(side='left', padx=8)
        self.cancel_button = ttk.Button(options, text='取消', command=self.cancel.set, state='disabled')
        self.cancel_button.pack(side='left')
        ttk.Button(options, text='保存结果 JSON', command=self.save).pack(side='right')
        self.status = tk.StringVar(value='等待添加编成。运行完全离线，采用上游自动出牌策略。')
        ttk.Label(body, textvariable=self.status).pack(anchor='w', pady=10)
        self.bar = ttk.Progressbar(body, maximum=100)
        self.bar.pack(fill='x')
        columns = ('name', 'mean', 'sd', 'n', 'min', 'max', 'state')
        self.table = ttk.Treeview(body, columns=columns, show='headings', height=10)
        for key, label in zip(columns, ('候选', '平均分', '单次标准差', '次数', '最低', '最高', '状态')):
            self.table.heading(key, text=label)
            self.table.column(key, width=115 if key != 'name' else 250, stretch=True)
        self.table.pack(fill='both', expand=True, pady=10)
        ttk.Label(body, text='按样本均值排序，波动和采样误差可能影响名次。种子仅在同版本、同输入下复现。').pack(anchor='w')
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(100, self.poll)

    def add(self):
        for path in filedialog.askopenfilenames(title='选择完整 Loadout', filetypes=[('JSON', '*.json')]):
            path = Path(path).resolve()
            if path not in self.paths:
                self.paths.append(path)
                self.files.insert('end', str(path))

    def clear(self):
        self.paths.clear()
        self.files.delete(0, 'end')

    def start(self):
        try:
            candidates = [(str(path), Loadout.load(path)) for path in self.paths]
            trials, seed = int(self.trials.get()), int(self.seed.get())
            if not candidates or not 1 <= trials <= 100000 or not 0 <= seed <= 0xffffffff:
                raise ValueError('添加候选；次数范围 1–100000，种子范围 0–4294967295。')
            if len({l.stage_id for _, l in candidates}) != 1:
                raise ValueError('请只比较相同场地的候选。')
        except (ValueError, OSError) as error:
            messagebox.showerror('无法运行', str(error))
            return
        self.cancel.clear()
        self.running, self.report = True, None
        self.table.delete(*self.table.get_children())
        for button in (self.add_button, self.clear_button, self.run_button):
            button.configure(state='disabled')
        self.cancel_button.configure(state='normal')
        self.status.set('正在启动本地引擎…')
        def work():
            try:
                report = compare(candidates, trials=trials, seed=seed, cancel=self.cancel,
                    cache=USER_ROOT / 'work/simulation/cache',
                    progress=lambda name, n, total: self.events.put(('progress', (name, n, total))),
                    on_result=lambda entry: self.events.put(('entry', entry)))
                self.events.put(('done', report))
            except Exception as error:
                self.events.put(('error', str(error)))
        threading.Thread(target=work, daemon=True).start()

    def add_entry(self, entry):
        if entry['status'] == 'ok':
            s = entry['summary']
            sd = '-' if s['sample_stddev'] is None else f"{s['sample_stddev']:.2f}"
            values = (Path(entry['name']).name, f"{s['mean']:.2f}", sd, s['trials'], s['min'], s['max'], '缓存' if entry['cached'] else '完成')
        else:
            values = (Path(entry['name']).name, '-', '-', '-', '-', '-', entry['error'])
        self.table.insert('', 'end', values=values)

    def poll(self):
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == 'progress':
                name, n, total = value
                self.status.set(f'{Path(name).name}：{n}/{total}')
                self.bar['value'] = n / total * 100
            elif kind == 'entry':
                self.add_entry(value)
            else:
                self.running = False
                for button in (self.add_button, self.clear_button, self.run_button):
                    button.configure(state='normal')
                self.cancel_button.configure(state='disabled')
                if kind == 'done':
                    self.report = value
                    self.table.delete(*self.table.get_children())
                    ordered = sorted(value['results'], key=lambda r: r.get('summary', {}).get('mean', float('-inf')), reverse=True)
                    for entry in ordered:
                        self.add_entry(entry)
                    self.status.set({'complete': '完成', 'cancelled': '已取消，保留完成的候选', 'completed-with-errors': '部分候选失败'}[value['status']] + '；可保存完整结果。')
                else:
                    self.status.set('运行失败：' + value)
        self.root.after(100, self.poll)

    def save(self):
        if self.report is None:
            messagebox.showinfo('暂无结果', '请先完成或取消当前比较。')
            return
        path = filedialog.asksaveasfilename(defaultextension='.json', initialfile='comparison.json')
        if path:
            try:
                if Path(path).resolve() in self.paths:
                    raise ValueError('结果不能覆盖输入编成。')
                atomic_json(Path(path), self.report)
            except (ValueError, OSError) as error:
                messagebox.showerror('保存失败', str(error))

    def close(self):
        self.cancel.set()
        if self.running:
            self.root.after(100, self.close)
        else:
            self.root.destroy()


def launch():
    root = tk.Tk()
    SimulationWindow(root)
    root.mainloop()
