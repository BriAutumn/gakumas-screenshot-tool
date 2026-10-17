"""Small desktop shell for native exports and the existing search website."""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from gkshot.app_paths import USER_ROOT, RESOURCE_ROOT
from gkshot.data_updates import DataUpdater
from gkshot.native_service import NativeService
from gkshot.plugin_manager import PluginManager, detect_game_dirs
from gkshot.program_updates import (check_release, download_release, stage_archive,
                                    activate_staged, rollback)
from gkshot.simulation import atomic_json
from gkshot.simulation_web import SimulationServer


class Launcher:
    def __init__(self, root, baseline_exe=None):
        self.root = root
        self.baseline_exe = baseline_exe or sys.executable
        self.config = json.loads((RESOURCE_ROOT / 'configs/product.json').read_text(encoding='utf8'))
        self.home = USER_ROOT
        self.home.mkdir(parents=True, exist_ok=True)
        self.manager = PluginManager(RESOURCE_ROOT / 'assets/plugin', self.home)
        self.native = NativeService()
        self.server = None
        self.events = queue.Queue()
        self.busy = False
        self.closing = False
        self.restart = False
        self.last_plugin_action = None
        self.helper_pending = None
        self._data_state = None
        self._maintenance_server = None
        self.data_pending_restart = False
        self.settings_path = self.home / 'settings.json'
        try: self.settings = json.loads(self.settings_path.read_text(encoding='utf8'))
        except (OSError, ValueError): self.settings = {}
        self.game_dir = tk.StringVar(value=self.settings.get('game_dir', ''))
        self.export_status = tk.StringVar(value='正在读取导出状态…')
        self.plugin_status = tk.StringVar(value='请选择游戏安装目录')
        self.service_status = tk.StringVar(value='本地网页尚未启动')
        self.data_status = tk.StringVar(value='模拟数据状态读取中…')
        self.message = tk.StringVar(value='安装插件 → 启动游戏到主页面 → 网页读取最新回忆 → 搜索编成')
        self.buttons = []
        self.window_icons = None
        self._build()
        self.root.after(100, self._poll)
        self.run(self._startup, self._started)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def _build(self):
        root = self.root
        scale = root.winfo_fpixels('1i') / 96
        root.title('学マス回忆工具（启动修复测试版） · ' + self.config['version'])
        if sys.platform == 'win32':
            icon_path = RESOURCE_ROOT / 'assets/app-icon.ico'
            root.iconbitmap(default=str(icon_path))
            try:
                from gkshot.window_icons import WindowIcons
                self.window_icons = WindowIcons(root, icon_path)
            except Exception:
                self.window_icons = None
        root.geometry(f'{round(700 * scale)}x{round(720 * scale)}')
        root.minsize(round(650 * scale), round(480 * scale))
        root.configure(bg='#f3f5f7')
        style = ttk.Style(root); style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#f3f5f7')
        style.configure('TFrame', background='#f3f5f7')
        style.configure('TLabel', foreground='#253045', background='#f3f5f7')
        style.configure('Muted.TLabel', foreground='#697386')
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 21, 'bold'))
        style.configure('TButton', padding=(round(12 * scale), round(9 * scale)), background='white')
        style.configure('Primary.TButton', background='#167d91', foreground='white')
        style.map('Primary.TButton', background=[('active', '#126476')])
        scrolling = ttk.Frame(root); scrolling.pack(fill='both', expand=True)
        canvas = tk.Canvas(scrolling, background='#f3f5f7', highlightthickness=0)
        scrollbar = ttk.Scrollbar(scrolling, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side='left', fill='both', expand=True)
        body = ttk.Frame(canvas, padding=round(24 * scale))
        content = canvas.create_window((0, 0), window=body, anchor='nw')
        self.canvas, self.body, self.scrollbar = canvas, body, scrollbar
        self._overflow = False
        self._sync_pending = False
        self._syncing = False

        def sync(*_args):
            # Coalesced, offset-independent layout pass. Flush pending geometry so
            # the requested height is current even while scrolled offscreen, then
            # compare it (not a scroll-offset bbox) against the live viewport.
            self._sync_pending = False
            if self._syncing: return
            self._syncing = True
            try:
                body.update_idletasks()
                width, viewport = canvas.winfo_width(), canvas.winfo_height()
                if width <= 1 or viewport <= 1: return
                canvas.itemconfigure(content, width=width)
                height = body.winfo_reqheight()
                if height <= viewport:
                    self._overflow = False
                    canvas.configure(scrollregion=(0, 0, width, viewport))
                    canvas.coords(content, 0, 0)
                    canvas.yview_moveto(0)
                    if scrollbar.winfo_ismapped(): scrollbar.pack_forget()
                else:
                    self._overflow = True
                    canvas.configure(scrollregion=(0, 0, width, height))
                    if not scrollbar.winfo_ismapped(): scrollbar.pack(side='right', fill='y', before=canvas)
            finally:
                self._syncing = False

        def schedule(*_args):
            if self._sync_pending: return
            self._sync_pending = True
            root.after_idle(sync)

        def on_wheel(event):
            if not self._overflow or event.widget is scrollbar: return
            widget = event.widget
            while widget is not None and widget is not canvas and widget is not body:
                widget = getattr(widget, 'master', None)
            if widget is None: return  # footer, dialogs and other toplevels are ignored
            canvas.yview_scroll(int(-event.delta / 120), 'units')

        def watch(widget):
            # Re-evaluate layout when any content widget (re)configures or maps;
            # body Configure alone can miss size changes while scrolled offscreen.
            widget.bind('<Configure>', schedule, add='+')
            widget.bind('<Map>', schedule, add='+')
            widget.bind('<Unmap>', schedule, add='+')
            for child in widget.winfo_children(): watch(child)

        canvas.bind('<Configure>', schedule)
        root.bind('<Configure>', schedule, add='+')
        root.bind_all('<MouseWheel>', on_wheel, add='+')
        root.after_idle(sync)
        ttk.Label(body, text='回忆工具', style='Title.TLabel').pack(anchor='w')
        ttk.Label(body, text='本地导出 · 全库搜索 · 编成比较', style='Muted.TLabel').pack(anchor='w', pady=(3,16))
        game = ttk.LabelFrame(body, text=' 1   游戏与插件 ', padding=14); game.pack(fill='x')
        row = ttk.Frame(game); row.pack(fill='x')
        ttk.Entry(row, textvariable=self.game_dir).pack(side='left', fill='x', expand=True)
        self.button(row, '选择游戏', self.choose_game).pack(side='left', padx=(8,0))
        self.button(row, '自动查找', lambda:self.run(lambda: detect_game_dirs(self.home), self._detected)).pack(side='left', padx=(6,0))
        ttk.Label(game, textvariable=self.plugin_status, wraplength=round(605 * scale), style='Muted.TLabel').pack(anchor='w', pady=8)
        row = ttk.Frame(game); row.pack(fill='x')
        self.button(row, '安装 / 更新插件', lambda:self.plugin_action('install')).pack(side='left')
        self.button(row, '卸载插件', lambda:self.plugin_action('uninstall')).pack(side='left', padx=8)
        export = ttk.LabelFrame(body, text=' 2   回忆数据 ', padding=14); export.pack(fill='x', pady=12)
        ttk.Label(export, textvariable=self.export_status, wraplength=round(605 * scale)).pack(anchor='w')
        ttk.Label(export, text='插件在每次游戏启动后导出一次；新增回忆后重新启动游戏即可刷新。', style='Muted.TLabel', wraplength=round(605 * scale)).pack(anchor='w', pady=6)
        row = ttk.Frame(export); row.pack(fill='x')
        self.button(row, '读取最新回忆', lambda:self.run(self.native.import_latest, self._imported)).pack(side='left')
        self.button(row, '导入旧导出文件', self.import_file).pack(side='left', padx=8)
        self.button(row, '数据目录', lambda:os.startfile(self.home)).pack(side='left')
        web = ttk.Frame(body); web.pack(fill='x', pady=(0,12))
        self.button(web, '打开模拟网页', self.open_web, primary=True).pack(side='left')
        self.service_button = self.button(web, '停止网页服务', self.toggle_service); self.service_button.pack(side='left', padx=8)
        ttk.Label(web, textvariable=self.service_status, style='Muted.TLabel').pack(side='left')
        updates = ttk.LabelFrame(body, text=' 3   程序更新 ', padding=12); updates.pack(fill='x')
        row = ttk.Frame(updates); row.pack(fill='x')
        self.button(row, '检查程序更新', self.check_updates).pack(side='left')
        self.button(row, '安装本地更新包', self.local_update).pack(side='left', padx=8)
        self.button(row, '回退上一版本', self.rollback_update).pack(side='left')
        ttk.Label(updates, text=f"版本 {self.config['version']} · 更新工具和导出插件", style='Muted.TLabel').pack(anchor='w', pady=(7,0))
        data = ttk.LabelFrame(body, text=' 4   模拟数据 ', padding=12); data.pack(fill='x', pady=12)
        ttk.Label(data, textvariable=self.data_status, wraplength=round(605 * scale)).pack(anchor='w')
        ttk.Label(data, text='赛季更新时请同步上游', style='Muted.TLabel', wraplength=round(605 * scale)).pack(anchor='w', pady=(6,0))
        row = ttk.Frame(data); row.pack(fill='x', pady=(8,0))
        self.button(row, '同步上游数据', self.sync_data).pack(side='left')
        self.button(row, '回退模拟数据', self.rollback_data).pack(side='left', padx=8)
        ttk.Label(data, text='直接同步 GKTools 的卡片、道具、赛季和模拟引擎。完成后重启工具，再读取最新回忆。', style='Muted.TLabel', wraplength=round(605 * scale)).pack(anchor='w', pady=(7,0))
        footer=ttk.Frame(root,padding=(24,12)); footer.pack(fill='x')
        ttk.Separator(footer).pack(fill='x',pady=(0,10))
        ttk.Label(footer, textvariable=self.message, wraplength=round(610 * scale)).pack(anchor='w')
        ttk.Label(footer, text='关闭窗口会停止本地服务并保留搜索进度。', style='Muted.TLabel').pack(anchor='w', pady=(5,0))
        watch(body)

    def button(self, parent, text, command, primary=False):
        button = ttk.Button(parent, text=text, command=command, style='Primary.TButton' if primary else 'TButton')
        self.buttons.append(button); return button

    def run(self, action, done=None, plugin_action=None):
        if self.busy or self.closing: return
        self.last_plugin_action = plugin_action
        self.busy = True
        for button in self.buttons: button.configure(state='disabled')
        def worker():
            try: self.events.put(('done', done, action()))
            except Exception as error: self.events.put(('error', None, error))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        if self.helper_pending:
            path, started = self.helper_pending
            result = None
            try:
                if path.is_file(): result = json.loads(path.read_text(encoding='utf8'))
            except (OSError, ValueError): pass
            if result is not None or time.monotonic() - started > 120:
                self.helper_pending = None
                self.busy = False
                for button in self.buttons: button.configure(state='normal')
                if result and result.get('ok'): self._plugin_result(result)
                else: self.message.set((result or {}).get('error', '管理员操作暂未完成，请检查权限提示并重新检测插件。'))
        while not self.events.empty():
            kind, done, value = self.events.get()
            if kind == 'progress':
                self.message.set(str(value))
                continue
            self.busy = False
            for button in self.buttons: button.configure(state='normal')
            if kind == 'error':
                self.message.set(str(value))
                # Elevate only the narrow installer helper and pass this user's data home.
                if isinstance(value, PermissionError) and self.last_plugin_action:
                    if messagebox.askyesno('需要游戏目录写入权限', '插件安装需要写入游戏目录。是否仅以管理员权限运行本次插件操作？'):
                        self._elevate_plugin()
            elif done:
                try: done(value)
                except Exception as error: self.message.set(str(error))
            if not self.root.winfo_exists(): return
        if self.server and self.server.workbench_requested.is_set():
            self.server.workbench_requested.clear(); self.root.deiconify(); self.root.lift()
        if not self.busy and not self.closing:
            try:
                info = self.native.status()
                label = {'completed':'导出完成', 'running':'游戏数据读取中', 'stale_running':'旧导出状态未更新，请提供诊断日志', 'not_found':'尚无导出', 'not_loaded':'等待游戏数据失败', 'incomplete':'导出未完成', 'io_error':'导出写入失败', 'timeout':'导出超时，请提供诊断日志'}.get(info.get('status'),info.get('status','未知'))
                stamp = datetime.fromtimestamp(info['exported_at']).strftime('%m-%d %H:%M') if info.get('exported_at') else ''
                self.export_status.set(f"{label} · {info.get('records', 0)} 份  {stamp}")
            except (ValueError, OSError): self.export_status.set('导出状态暂不可读')
            self._apply_data_status()
        self.root.after(500, self._poll)

    def _startup(self):
        directories = detect_game_dirs(self.home)
        self._start_server()
        try: self._data_state = DataUpdater(user_home=self.home).status()
        except Exception as error: self._data_state = {'error': str(error)}
        return directories

    def _started(self, directories):
        if not self.game_dir.get() and directories: self.game_dir.set(str(directories[0]))
        self._apply_data_status()
        self._update_service_label()
        if self.game_dir.get(): self._save_game(); self.run(lambda:self.manager.status(Path(self.game_dir.get())), self._plugin_result)

    def _start_server(self):
        if self.server: return self.server
        try: server = SimulationServer(('127.0.0.1', 8768))
        except OSError: server = SimulationServer(('127.0.0.1', 0))
        server.workbench_requested = threading.Event()
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _update_service_label(self):
        self.service_status.set(f'127.0.0.1:{self.server.server_port}' if self.server else '网页服务已停止')
        self.service_button.configure(text='停止网页服务' if self.server else '启动网页服务')

    def _stop_server(self):
        if not self.server: return
        server = self.server
        server.shutdown()
        deadline = time.monotonic() + 15
        while server.busy() and time.monotonic() < deadline: time.sleep(.05)
        if server.busy(): raise RuntimeError('模拟仍在停止中，请稍后重试')
        server.server_close(); self.server = None

    def toggle_service(self):
        self.run(self._stop_server if self.server else self._start_server, lambda _:self._update_service_label())

    def open_web(self):
        self.run(lambda:self._start_server().origin, lambda url:(self._update_service_label(), webbrowser.open(url)))

    def _save_game(self):
        self.settings['game_dir'] = self.game_dir.get().strip()
        atomic_json(self.settings_path, self.settings)

    def choose_game(self):
        filename = filedialog.askopenfilename(title='选择 gakumas.exe', filetypes=[('游戏程序','gakumas.exe')])
        if filename:
            self.game_dir.set(str(Path(filename).parent)); self._save_game()
            self.run(lambda:self.manager.status(Path(self.game_dir.get())), self._plugin_result)

    def _detected(self, directories):
        if not directories: self.message.set('未找到游戏，请选择安装目录里的 gakumas.exe'); return
        self.game_dir.set(str(directories[0])); self._save_game()
        self.run(lambda:self.manager.status(directories[0]), self._plugin_result)

    def _plugin_result(self, info):
        labels = {'installed':'插件已安装','not_installed':'未安装插件','prototype':'旧版插件，可更新','update_available':'插件或导出配置需要更新','conflict':'与现有插件文件冲突'}
        state = info.get('state')
        if state == 'installed' and info.get('dll_name') == 'winhttp.dll':
            label = '插件已安装（兼容模式，已保留原有插件）'
        else:
            label = labels.get(state, state)
        self.plugin_status.set(label + (' · 游戏正在运行' if info.get('running') else ''))
        self.message.set('插件操作完成；启动游戏进入主页面后，读取最新回忆。' if info.get('changed') else self.plugin_status.get())

    def plugin_action(self, action):
        if not self.game_dir.get().strip():
            self.message.set('请先选择 gakumas.exe 所在目录'); return
        game = Path(self.game_dir.get().strip()); self._save_game()
        self.run(lambda:getattr(self.manager, action)(game), self._plugin_result, plugin_action=action)

    def _elevate_plugin(self):
        import ctypes
        result_path = self.home / 'plugin-action-result.json'
        result_path.unlink(missing_ok=True)
        args = ['--plugin-action', self.last_plugin_action, '--game-dir', self.game_dir.get(), '--user-home', str(self.home), '--active-child']
        if not getattr(sys, 'frozen', False): args.insert(0, str(RESOURCE_ROOT / 'packaging/entry.py'))
        status = ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable, subprocess.list2cmdline(args), str(RESOURCE_ROOT), 0)
        if status > 32:
            self.helper_pending = (result_path, time.monotonic())
            self.busy = True
            for button in self.buttons: button.configure(state='disabled')
            self.message.set('正在等待管理员插件操作完成…')
        else: self.message.set('管理员操作未启动')

    def import_file(self):
        path = filedialog.askopenfilename(title='选择原生 memories.json', filetypes=[('JSON 导出','*.json')])
        if path: self.run(lambda:self.native.import_file(path), self._imported)

    def _imported(self, result):
        if self.server:
            with self.server.guard: self.server.discover_libraries()
        self.message.set(f"已读取 {result['total']} 份：Stage 可用 {result['ready']}，跳过 {result['skipped_count']}，转换失败 {result['rejected_count']}。网页刷新后可选择此库。")

    def _can_update(self):
        if self.server and self.server.busy():
            self.message.set('请先暂停网页里的搜索任务，再更新程序。'); return False
        return True

    def check_updates(self):
        if self._can_update(): self.message.set('正在检查程序更新…'); self.run(lambda:check_release(self.config['version'], self.config['release_repository'], self.config['release_tag_prefix']), self._release_found)

    def _release_found(self, release):
        if not release['available']: self.message.set(release['message']); return
        if messagebox.askyesno('发现更新', f"版本 {release['version']} 可用，是否下载并安装？"):
            self.run(lambda:self._prepare_online(release), self._updated)

    def _prepare_online(self, release):
        downloaded = download_release(release, self.home / 'update-downloads')
        return self._prepare_zip(downloaded['zip_path'], downloaded['sha256'])

    def local_update(self):
        if not self._can_update(): return
        path = filedialog.askopenfilename(title='选择完整工具更新包', filetypes=[('完整更新包','*.zip')])
        if path: self.run(lambda:self._prepare_zip(path), self._updated)

    def _prepare_zip(self, path, expected=None):
        staged = stage_archive(Path(path), self.home / 'program-updates', expected_sha256=expected)
        if self.server and self.server.busy(): raise RuntimeError('搜索正在运行，更新包已暂存，请暂停后重试')
        activate_staged(Path(staged['directory']), self.home)
        return staged

    def _updated(self, staged):
        self.message.set(f"已安装 {staged['version']}。重启工具后生效；进入工具后可更新游戏插件。")
        if messagebox.askyesno('更新完成', '现在重启工具以使用新版？'): self.restart = True; self.close()

    def rollback_update(self):
        if self._can_update(): self.run(lambda:rollback(self.home), lambda _:self._updated({'version':'上一版本'}))

    # ---- upstream simulation data -----------------------------------------
    @staticmethod
    def _short_sha(value):
        text = str(value or '').strip()
        return text[:8] + '…' if text else '内置'

    @staticmethod
    def _format_counts(counts):
        if not isinstance(counts, dict) or not counts:
            return '已完成'
        return '、'.join(f'{key} {value}' for key, value in counts.items())

    def _data_progress(self, text):
        # Called from the updater worker thread; only the thread-safe queue is touched.
        self.events.put(('progress', None, str(text)))

    def _updater(self):
        return DataUpdater(user_home=self.home, progress=self._data_progress)

    def _apply_data_status(self):
        state = self._data_state
        if not isinstance(state, dict):
            return
        if state.get('error'):
            self.data_status.set('模拟数据状态不可读：' + str(state['error'])[:80])
            return
        revision = state.get('revision')
        source = '上游数据' if state.get('source') == 'upstream' else '内置数据'
        label = self._short_sha(revision) if revision else '内置基线'
        text = f"当前模拟数据 {label}（{source}）"
        if state.get('running_revision') != revision or self.data_pending_restart:
            text += ' · 已更新，重启后生效'
        self.data_status.set(text)

    def _refresh_data_state(self):
        """Read the on-disk data status on the worker thread; never from Tk."""
        try: self._data_state = self._updater().status()
        except Exception as error: self._data_state = {'error': str(error)}

    def sync_data(self):
        self.message.set('正在检查上游模拟数据…')
        self.run(self._check_data, self._data_checked)

    def _check_data(self):
        result = self._updater().check()
        self._refresh_data_state()
        return result

    def _data_checked(self, result):
        current = result.get('current_revision')
        revision = result.get('revision')
        if not result.get('update_available'):
            self.message.set(f"模拟数据已是最新（{self._short_sha(current or revision)}）。")
            return
        detail = self._format_changes(result.get('change_summary'))
        if messagebox.askyesno('发现模拟数据更新',
                f"当前数据：{self._short_sha(current)}\n上游数据：{self._short_sha(revision)}\n{detail}\n\n是否下载并安装该版本？"):
            master = result.get('master_revision')
            self.run(lambda: self._install_data(str(revision), str(master or '')), self._data_installed)
            self.message.set('正在下载并安装上游模拟数据…')
        else:
            self.message.set('已取消模拟数据同步。')

    @staticmethod
    def _format_changes(changes):
        return '将同步卡片、道具、赛季和模拟引擎，并更新回忆对应关系。'

    def _install_data(self, revision, master_revision):
        server = self._start_server()
        if not server.begin_maintenance():
            raise RuntimeError('搜索、模拟或赛季任务正在运行，请先暂停后再同步模拟数据。')
        try:
            result = self._updater().install(revision, master_revision)
        except BaseException:
            server.end_maintenance()
            raise
        self._maintenance_server = server
        self._refresh_data_state()
        return result

    def _data_installed(self, result):
        self.data_pending_restart = True
        self.message.set(f"已安装模拟数据 {self._short_sha(result.get('revision'))}"
                         f"（{self._format_counts(result.get('counts'))}）。重启工具后生效。")
        self._release_maintenance_after_restart_choice('模拟数据已更新', '现在重启工具以使用新数据？重启后请重新点击“读取最新回忆”。')

    def rollback_data(self):
        self.run(self._rollback_data, self._data_rolled_back)

    def _rollback_data(self):
        server = self._start_server()
        if not server.begin_maintenance():
            raise RuntimeError('搜索、模拟或赛季任务正在运行，请先暂停后再回退模拟数据。')
        try:
            result = self._updater().rollback()
        except BaseException:
            server.end_maintenance()
            raise
        self._maintenance_server = server
        self._refresh_data_state()
        return result

    def _data_rolled_back(self, result):
        self.data_pending_restart = True
        self.message.set(f"已回退模拟数据到 {self._short_sha(result.get('revision'))}。重启工具后生效。")
        self._release_maintenance_after_restart_choice('模拟数据已回退', '现在重启工具以使用该数据？')

    def _release_maintenance_after_restart_choice(self, title, prompt):
        if messagebox.askyesno(title, prompt):
            self._release_maintenance()
            self.restart = True
            self.close()
        else:
            self._release_maintenance()
            self.message.set(self.message.get() + ' 已选择稍后重启，当前进程仍使用固定版本。')

    def _release_maintenance(self):
        server = self._maintenance_server
        self._maintenance_server = None
        if server is not None:
            server.end_maintenance()

    def close(self):
        if self.closing: return
        if self.busy: self.message.set('正在处理，请完成后关闭窗口。'); return
        self.closing = True
        self._release_maintenance()
        for button in self.buttons: button.configure(state='disabled')
        self.message.set('正在停止本地服务并保存进度…')
        def stop():
            try: self._stop_server(); self.events.put(('done', lambda _:self._closed(), None))
            except Exception as error:
                self.closing = False; self.events.put(('error', None, error))
        threading.Thread(target=stop, daemon=True).start()

    def _closed(self):
        if self.restart:
            command = [self.baseline_exe, '--user-home', str(self.home)]
            if not getattr(sys, 'frozen', False): command.insert(1, str(RESOURCE_ROOT / 'packaging/entry.py'))
            subprocess.Popen(command, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.root.destroy()


def launch(baseline_exe=None):
    if sys.platform == 'win32':
        import ctypes
        # Before creating Tk, use native screen pixels so Windows does not
        # enlarge 16/32-pixel icons and a rasterised 96-DPI window at 200%.
        user32 = ctypes.windll.user32
        if hasattr(user32, 'SetProcessDpiAwarenessContext'):
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        else:
            user32.SetProcessDPIAware()
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('gk-memory-tool.desktop')
    root = tk.Tk(); Launcher(root, baseline_exe); root.mainloop()
