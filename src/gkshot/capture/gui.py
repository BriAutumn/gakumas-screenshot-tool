"""Standalone collection UI; the user can close Codex before using this window."""
import copy
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import ImageTk

from gkshot.catalog import PROJECT_ROOT
from gkshot.capture.store import CaptureSession, DEFAULT_CAPTURES, atomic_json, exclusive_collector_lock
from gkshot.capture.vision import load_profile, CaptureVision
from gkshot.capture.windows import WindowsBackend, list_game_windows, dpi_aware

from gkshot.app_paths import USER_ROOT
PROFILE_PATH = USER_ROOT / "data/capture-profile.json"
LABELS = {
    "list_region": "列表：完整缩略图区域（点左上、右下）",
    "list_scrollbar": "列表：右侧滚动条轨道（点左上、右下）",
    "open_detail": "列表：橙色『詳細を見る』按钮中心",
    "list_scroll": "列表：缩略图区域内的滚动位置",
    "stage_tab": "详情：『ステージ』标签中心",
    "detail_scroll": "详情：下方内容区域的滚动位置",
    "dismiss_tooltip": "详情：关闭弹框的空白处（避开按钮和卡片）",
    "back": "详情：底部双箭头返回按钮中心",
    "stage_region": "详情：『ステージ』橙色标签区域（点左上、右下）",
    "list_button_region": "列表：橙色详情按钮区域（点左上、右下）",
    "header_region": "详情：固定人物头像与姓名区域（点左上、右下）",
    "detail_region": "详情：下方可滚动内容区域（点左上、右下）",
}


class Calibration:
    def __init__(self, parent, image, profile, on_save):
        self.profile, self.on_save = copy.deepcopy(profile), on_save
        self.pending = []
        self.window = tk.Toplevel(parent)
        self.window.title("在静态截图上标定，不会点击游戏")
        self.window.geometry("770x910")
        frame = ttk.Frame(self.window,padding=12); frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="选择要调整的项目，再在截图上点位置。区域项目依次点左上、右下。保留正确的默认值即可。",wraplength=720).pack(anchor="w")
        self.choice = tk.StringVar(value=LABELS["list_region"])
        combo = ttk.Combobox(frame,textvariable=self.choice,values=list(LABELS.values()),state="readonly",width=65)
        combo.pack(fill="x",pady=8); combo.bind("<<ComboboxSelected>>",lambda e:self.select())
        scale = min(650/image.width, 715/image.height)
        preview = image.resize((round(image.width*scale),round(image.height*scale)))
        self.width,self.height = preview.size
        self.photo = ImageTk.PhotoImage(preview)
        self.canvas = tk.Canvas(frame,width=self.width,height=self.height,highlightthickness=1)
        self.canvas.pack(); self.canvas.create_image(0,0,image=self.photo,anchor="nw")
        self.canvas.bind("<Button-1>",self.click)
        self.status = tk.StringVar()
        ttk.Label(frame,textvariable=self.status).pack(anchor="w",pady=6)
        ttk.Button(frame,text="保存标定",command=self.save).pack(anchor="e")
        self.select()

    def key(self):
        return next(key for key,label in LABELS.items() if label==self.choice.get())

    def select(self):
        self.pending = []
        self.draw()

    def draw(self):
        self.canvas.delete("marker")
        values = self.profile[self.key()]
        coords = [n*(self.width if i%2==0 else self.height) for i,n in enumerate(values)]
        if len(coords)==4:
            self.canvas.create_rectangle(*coords,outline="#ff6200",width=3,tags="marker")
        else:
            x,y=coords
            self.canvas.create_oval(x-7,y-7,x+7,y+7,outline="#ff6200",width=3,tags="marker")
        self.status.set("已标记位置；保存后用于新会话。已有会话继续使用原标定。")

    def click(self,event):
        point=[event.x/self.width,event.y/self.height]
        if not all(0<n<1 for n in point):return
        key=self.key()
        if len(self.profile[key])==2:
            self.profile[key]=point
            self.draw()
        else:
            self.pending.append(point)
            if len(self.pending)==1:
                self.status.set("现在点区域右下角。")
            else:
                a,b=self.pending
                self.profile[key]=[min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1])]
                self.pending=[];self.draw()

    def save(self):
        try:
            PROFILE_PATH.parent.mkdir(exist_ok=True)
            # Validate before replacing a previously usable profile.
            temporary=PROFILE_PATH.with_suffix(".candidate.json")
            atomic_json(temporary,self.profile)
            validated=load_profile(temporary)
            atomic_json(PROFILE_PATH,validated)
            temporary.unlink(missing_ok=True)
            self.on_save(validated)
            self.window.destroy()
        except (ValueError,OSError) as error:
            messagebox.showerror("标定无效",str(error),parent=self.window)


class CaptureWindow:
    def __init__(self,root,initial_tab="capture"):
        from gkshot.ui_theme import apply_theme, heading, size_window
        self.root=root
        apply_theme(root)
        self.profile=load_profile(PROFILE_PATH if PROFILE_PATH.exists() else PROJECT_ROOT/"configs/capture/pc-memory-v1.json")
        self.session=None;self.windows=[];self.busy=False;self.latest_library=None
        self.messages=queue.Queue();self.pause_event=threading.Event();self.stop_event=threading.Event()
        self.controls=[]
        root.title("GKTool · 采集与离线识别")
        size_window(root,960,800,880,740)
        body=ttk.Frame(root,padding=20);body.pack(fill="both",expand=True)
        heading(body,"采集与离线识别","01 保存原图 → 02 关游戏后识别 → 03 查看本次结果库")
        session_box=ttk.LabelFrame(body,text="当前会话",padding=12);session_box.pack(fill="x",pady=(0,12))
        row=ttk.Frame(session_box);row.pack(fill="x")
        self.button(row,"新建采集会话",self.new_session)
        self.button(row,"打开已有会话",self.open_session)
        self.session_text=tk.StringVar(value="尚未选择会话。已有原图请打开 session.json，无需重新采集。")
        ttk.Label(session_box,textvariable=self.session_text,wraplength=840,style="Muted.TLabel").pack(anchor="w",pady=(8,0))
        self.notebook=ttk.Notebook(body);self.notebook.pack(fill="x")
        capture=ttk.Frame(self.notebook,padding=14)
        offline=ttk.Frame(self.notebook,padding=14)
        self.notebook.add(capture,text="01  原图采集")
        self.notebook.add(offline,text="02  离线识别")
        row=ttk.Frame(capture);row.pack(fill="x",pady=(0,10))
        self.window_choice=tk.StringVar()
        self.window_box=ttk.Combobox(row,textvariable=self.window_choice,state="readonly",width=38);self.window_box.pack(side="left",padx=(0,8))
        self.button(row,"刷新游戏窗口",self.refresh_windows)
        self.button(row,"标定当前画面",self.calibrate)
        ttk.Label(capture,text="游戏停在独立 memory 列表，保持筛选、排序和窗口大小。开始后请让游戏保持前台。",wraplength=830).pack(anchor="w")
        row=ttk.Frame(capture);row.pack(fill="x",pady=12)
        self.button(row,"一份试采",lambda:self.start(1))
        ttk.Label(row,text="本批最多").pack(side="left",padx=(8,5))
        self.limit=tk.StringVar(value="20")
        limit=ttk.Spinbox(row,from_=1,to=2000,textvariable=self.limit,width=7);limit.pack(side="left");self.controls.append(limit)
        ttk.Label(row,text="份").pack(side="left",padx=5)
        self.button(row,"开始 / 续采",self.start_batch)
        self.retry=tk.BooleanVar(value=False)
        self.from_top=tk.BooleanVar(value=False)
        self.all_details=tk.BooleanVar(value=False)
        self.manual_focus=tk.BooleanVar(value=False)
        advanced=ttk.LabelFrame(capture,text="采集选项 · 通常保留默认",padding=8);advanced.pack(fill="x")
        for title,variable in (("重试当前位置之后的待补记忆",self.retry),
                               ("从顶部重新核对（默认从当前位置附近续采）",self.from_top),
                               ("采集所有卡片详情（默认只采有角标或看不清的卡）",self.all_details),
                               ("手动切换到游戏（点击开始后留 3 秒切换）",self.manual_focus)):
            option=ttk.Checkbutton(advanced,text=title,variable=variable);option.pack(anchor="w");self.controls.append(option)
        ttk.Label(offline,text="使用已保存的原图，完全在本机识别。",style="Heading.TLabel").pack(anchor="w")
        ttk.Label(offline,text="先在上方打开已有会话，再开始识别。规则更新后可重跑同一批原图。\n每次处理单独保存结果；旧结果与已核对记录保留。",wraplength=830,style="Muted.TLabel").pack(anchor="w",pady=12)
        row=ttk.Frame(offline);row.pack(fill="x",pady=8)
        self.button(row,"离线识别本会话",self.process)
        self.button(row,"查看本会话最近结果",self.view_results)
        ttk.Label(offline,text="处理完成后会打开本次结果库。待解决不等于失败，请在记忆库查看缺失字段和证据。",wraplength=830).pack(anchor="w",pady=12)
        if initial_tab=="offline":self.notebook.select(offline)
        self.summary=tk.StringVar(value="尚无会话统计")
        ttk.Label(body,textvariable=self.summary,wraplength=880).pack(anchor="w",pady=(12,6))
        row=ttk.Frame(body);row.pack(fill="x",pady=(0,8))
        self.activity=tk.StringVar(value="准备就绪")
        ttk.Label(row,textvariable=self.activity).pack(side="left")
        self.stop_button=ttk.Button(row,text="停止（F9）",command=lambda:self.request_stop(False),state="disabled");self.stop_button.pack(side="right")
        self.pause_button=ttk.Button(row,text="暂停（F8）",command=lambda:self.request_stop(True),state="disabled");self.pause_button.pack(side="right",padx=8)
        self.progress=ttk.Progressbar(body,mode="indeterminate");self.progress.pack(fill="x",pady=(0,8))
        logs=ttk.Frame(body);logs.pack(fill="both",expand=True)
        self.log=tk.Text(logs,height=6,wrap="word",state="disabled",background="white",foreground="#202e43",relief="flat",padx=10,pady=8)
        scroll=ttk.Scrollbar(logs,command=self.log.yview);scroll.pack(side="right",fill="y")
        self.log.configure(yscrollcommand=scroll.set);self.log.pack(fill="both",expand=True)
        ttk.Label(body,text=f"原图位置：{DEFAULT_CAPTURES}",style="Muted.TLabel",wraplength=880).pack(anchor="w",pady=(8,0))
        root.bind("<F8>",lambda event:self.request_stop(True))
        root.bind("<F9>",lambda event:self.request_stop(False))
        root.protocol("WM_DELETE_WINDOW",self.close)
        root.after(100,self.poll)
        self.refresh_windows()

    def request_stop(self,pause=False):
        if not self.busy:return
        (self.pause_event if pause else self.stop_event).set()
        self.activity.set("已请求暂停，等待当前步骤安全结束…" if pause else "已请求停止，等待当前步骤安全结束…")

    def button(self,parent,text,command):
        button=ttk.Button(parent,text=text,command=command);button.pack(side="left",padx=(0,8));self.controls.append(button)

    def write(self,text):
        self.log.configure(state="normal");self.log.insert("end",text+"\n");self.log.see("end");self.log.configure(state="disabled")
        self.last_log_message=text

    def selected_window(self):
        index=self.window_box.current()
        if index<0 or index>=len(self.windows):raise ValueError("没有选中的 gakumas 游戏窗口。请打开游戏后刷新。")
        return self.windows[index]

    def refresh_windows(self):
        if self.busy:return
        try:
            self.windows=list_game_windows(self.profile["window_title"])
            self.window_box["values"]=[f"{w['title']} · 窗口 {w['hwnd']}" for w in self.windows]
            if len(self.windows)==1:self.window_box.current(0)
            if not self.windows:self.window_choice.set("");self.write("未找到游戏窗口。离线识别已采素材不需要打开游戏。")
        except (ValueError,OSError) as error:self.write(str(error))

    def new_session(self):
        if self.busy:return
        self.session=CaptureSession.create(copy.deepcopy(self.profile))
        self.latest_library=None
        self.update_summary()
        self.write("新会话已创建。首次使用先点击『一份试采』，检查原图后再批量。")

    def open_session(self):
        if self.busy:return
        filename=filedialog.askopenfilename(parent=self.root,title="选择采集会话的 session.json",initialdir=DEFAULT_CAPTURES,filetypes=[("采集会话","session.json")])
        if filename:
            try:
                self.session=CaptureSession(Path(filename).parent);self.latest_library=None;self.update_summary()
                self.write("会话已加载。默认从当前列表附近定位断点；无需回顶部。日志保存在会话目录的 capture.log。")
            except (ValueError,OSError) as error:messagebox.showerror("打开失败",str(error),parent=self.root)

    def update_summary(self):
        if not self.session:return
        self.session_text.set(str(self.session.root))
        entries=self.session.data["entries"]
        counts={s:sum(e["status"]==s for e in entries) for s in ("captured","needs-recapture","skipped-production-only","skipped-incomplete")}
        state={"new":"待开始","running":"采集中","paused":"已暂停","complete":"列表遍历结束","error":"发生错误"}.get(self.session.data["status"],self.session.data["status"])
        self.summary.set(f"已采完整 {counts['captured']} 份；缺少详情 {counts['needs-recapture']} 份；跳过待补 {counts['skipped-incomplete']} 份；制作专用跳过 {counts['skipped-production-only']} 份。状态：{state}")

    def calibrate(self):
        if self.busy:return
        try:
            backend=WindowsBackend(self.selected_window());backend.activate()
            image=backend.screenshot()
            self.root.lift()
            Calibration(self.root,image,self.profile,self.save_profile)
        except (ValueError,OSError,RuntimeError) as error:messagebox.showerror("无法截图",str(error),parent=self.root)

    def save_profile(self,profile):
        self.profile=profile
        self.write("标定已保存；新建会话时生效。已有会话保持原标定，避免改变续采位置。")

    def set_busy(self,value):
        self.busy=value
        for button in self.controls:button.configure(state="disabled" if value else "normal")
        self.window_box.configure(state="disabled" if value else "readonly")
        for button in (self.pause_button,self.stop_button):button.configure(state="normal" if value else "disabled")
        if value:
            self.progress.start(15)
            self.activity.set("处理中 · 详细进度见下方日志")
        else:
            self.progress.stop()
            self.activity.set("任务已结束 · 请查看结果摘要或日志")

    def start_batch(self):
        try:self.start(int(self.limit.get()))
        except ValueError:messagebox.showerror("数量无效","请填写 1 至 2000。",parent=self.root)

    def start(self,limit):
        if self.busy:return
        try:
            if not 1<=limit<=2000:raise ValueError("请填写 1 至 2000。")
            window=self.selected_window()
            backend=WindowsBackend(window)
            backend.verify(foreground=False)
            permissions=backend.preflight()
            self.write(permissions["description"])
            if self.session is None:self.new_session()
            self.pause_event.clear();self.stop_event.clear()
            retry=self.retry.get()
            from_top=self.from_top.get()
            all_details=self.all_details.get()
            session=self.session
            self.set_busy(True)
            self.write("3 秒后开始。F8 暂停、F9 停止；进度和原图会自动保存。")
            self.root.update_idletasks()
            if not self.manual_focus.get():
                backend.activate()
            def work():
                try:
                    from gkshot.capture.runner import Collector
                    with exclusive_collector_lock():
                        # Re-read after locking; a previous process may have updated the ledger.
                        active=CaptureSession(session.root)
                        collector=Collector(backend,CaptureVision(active.data["profile"]),active,
                            notify=lambda text:self.messages.put(("log",text)),pause=self.pause_event,stop=self.stop_event,
                            all_details=all_details)
                        collector.wait(3)
                        collector.run(limit,retry_issues=retry,foreground_ready=True,from_top=from_top)
                    self.messages.put(("finished",str(session.root)))
                except Exception as error:self.messages.put(("error",str(error)))
            threading.Thread(target=work,daemon=True).start()
        except (ValueError,OSError,RuntimeError) as error:
            self.set_busy(False)
            self.write("未开始："+str(error))
            messagebox.showerror("无法开始",str(error),parent=self.root)

    def process(self):
        if self.busy:return
        if self.session is None:
            messagebox.showinfo("选择会话","先打开已采集的会话。",parent=self.root);return
        self.set_busy(True)
        self.activity.set("离线识别中 · 无需打开游戏")
        folder=self.session.root
        self.pause_event.clear();self.stop_event.clear()
        self.write("开始离线处理；无需打开游戏，原始截图不会被改写。")
        def work():
            try:
                from gkshot.capture.process import process_session
                result=process_session(folder,notify=lambda text:self.messages.put(("log",text)),
                                       cancelled=lambda:self.pause_event.is_set() or self.stop_event.is_set())
                self.messages.put(("processed",result))
            except Exception as error:self.messages.put(("error",str(error)))
        threading.Thread(target=work,daemon=True).start()

    def view_results(self):
        if self.busy:return
        if not self.latest_library and self.session:
            folders=sorted((self.session.root/"processing").glob("*/memory-library/library.sqlite3"))
            if folders:self.latest_library=folders[-1].parent
        if self.latest_library:
            from gkshot.memory_gui import MemoryLibraryWindow
            from gkshot.memory_library import MemoryLibrary
            MemoryLibraryWindow(tk.Toplevel(self.root),MemoryLibrary(self.latest_library))
        else:messagebox.showinfo("暂无结果","先点击『离线识别本会话』。",parent=self.root)

    def poll(self):
        try:
            while True:
                kind,value=self.messages.get_nowait()
                if kind=="log":self.write(value)
                else:
                    self.set_busy(False)
                    if kind=="processed":
                        from gkshot.capture.process import processing_summary
                        self.latest_library=Path(value["library_directory"])
                        self.write(processing_summary(value["results"]))
                        for result in value["results"]:
                            for error in result.get("errors",[]):
                                self.write(f"第 {result['ordinal']} 份：{error['error']}")
                        if any(r.get("memory_id") for r in value["results"]):
                            self.write("已打开本次采集结果库；它与手动导入的默认记忆库分开保存。")
                            self.view_results()
                    elif kind=="error":
                        self.write("已停止："+value)
                        self.activity.set("任务未完成 · 请查看下方错误原因")
                    if self.session:
                        self.session=CaptureSession(self.session.root);self.update_summary()
                        if self.session.data.get("events") and kind=="finished":
                            message=self.session.data['events'][-1]['message']
                            if message!=getattr(self,'last_log_message',None):self.write(message)
        except queue.Empty:pass
        if self.root.winfo_exists():self.root.after(100,self.poll)

    def close(self):
        if self.busy:
            self.stop_event.set();self.write("已请求停止，请等待当前截图写入后关闭窗口。")
            return
        self.root.destroy()


def launch(initial_tab="capture"):
    dpi_aware()
    root=tk.Tk()
    CaptureWindow(root,initial_tab=initial_tab);root.mainloop()
