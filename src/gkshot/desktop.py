"""Unified portable entry point; all engines remain local and offline."""
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback


def prepare():
    from gkshot.app_paths import RESOURCE_ROOT, USER_ROOT
    USER_ROOT.mkdir(parents=True, exist_ok=True)
    (USER_ROOT / "logs").mkdir(exist_ok=True)
    # Source CLI keeps caller-relative paths; frozen CLI uses its writable home.
    if getattr(sys, "frozen", False):
        node = RESOURCE_ROOT / "runtime/node-bin/node.exe"
        if not node.is_file():
            raise RuntimeError("发行包缺少 Node 运行时，请完整解压下载的压缩包。")
        os.environ["PATH"] = str(node.parent) + os.pathsep + os.environ.get("PATH", "")
        os.chdir(USER_ROOT)
    return RESOURCE_ROOT, USER_ROOT


def diagnose():
    from gkshot.recognition.card_detail import CardDetailScanner
    from gkshot.adapters.local_engine import LocalSimulator
    from gkshot.models import Loadout
    import sqlite3
    import tempfile
    import tkinter as tk
    root, home = prepare()
    report = {"resources": str(root), "user_home": str(home), "frozen": bool(getattr(sys, "frozen", False)), "checks": {}}
    def check(name, function):
        try:
            report["checks"][name] = {"ok": True, "detail": function()}
        except Exception as error:
            report["checks"][name] = {"ok": False, "error": str(error)}
    def writable():
        with tempfile.TemporaryFile(dir=home) as stream:
            stream.write(b"GKTool")
        return "可写"
    def gui():
        window = tk.Tk()
        window.withdraw()
        version = window.tk.call("info", "patchlevel")
        window.destroy()
        return version
    def database():
        with sqlite3.connect(":memory:") as connection:
            return connection.execute("select sqlite_version()").fetchone()[0]
    def ocr():
        scanner = CardDetailScanner()
        image = scanner.memory.Image.new("RGB", (64, 32), "white")
        scanner.ocr(scanner.memory.np.asarray(image))
        return {"japanese_model": scanner.model_sha256, "providers": scanner.memory.scanner.card_model.get_providers()}
    def engine():
        with LocalSimulator() as simulator:
            result = simulator.simulate(Loadout.load(root / "examples/simulation/original.loadout.json"), trials=1)
            return {"node": simulator.identity()["node"], "result": result}
    check("writable_home", writable)
    check("tk", gui)
    check("sqlite", database)
    check("onnx_and_japanese_ocr", ocr)
    check("local_simulation", engine)
    report["ok"] = all(item["ok"] for item in report["checks"].values())
    path = home / "logs/diagnostics.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def menu():
    import tkinter as tk
    from tkinter import ttk, messagebox
    root, home = prepare()
    from gkshot.ui_theme import apply_theme, heading, size_window
    from gkshot.capture.windows import dpi_aware
    dpi_aware()
    window = tk.Tk()
    apply_theme(window)
    window.title("GKTool · 工作台")
    size_window(window, 900, 680, 800, 620)
    body = ttk.Frame(window, padding=24)
    body.pack(fill="both", expand=True)
    heading(body, "GKTool 工作台", "原图留在本机。从采集到成绩比较，按自己的进度继续。")
    status = tk.StringVar(value="准备就绪 · 可直接打开已有记忆库或模拟网页")
    def open_tool(args):
        command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(root / "packaging/entry.py")]
        try:
            with (home / "logs/desktop.log").open("a", encoding="utf-8") as log:
                process = subprocess.Popen(command + args, cwd=home, stdout=log, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            status.set("工具进程已启动 · 窗口加载可能需要几秒" if args != ["diagnose"] else "诊断进行中：正在检查本地模型与模拟引擎…")
            def check():
                code = process.poll()
                if code is None:
                    window.after(800, check)
                elif code:
                    status.set(f"工具退出（代码 {code}）。请查看日志目录中的 desktop.log / startup-error.txt。")
                else:
                    status.set("诊断通过，报告已保存到 logs/diagnostics.json。" if args == ["diagnose"] else "工具已关闭，可以继续下一步。")
            window.after(800, check)
        except OSError as error:
            status.set("启动失败，请检查错误详情。")
            messagebox.showerror("无法启动", f"{error}\n日志目录：{home / 'logs'}", parent=window)
    steps = ttk.Frame(body)
    steps.pack(fill="both", expand=True)
    for index, (title, detail, label, args) in enumerate((
        ("01  原图采集", "打开游戏，停在独立 memory 列表。\n先试采一份，再开始批量采集。", "打开采集器", ["capture-gui"]),
        ("02  离线识别", "已有原图？关掉游戏也能处理。\n打开会话，使用当前规则重新识别。", "处理已有会话", ["capture-offline"]),
        ("03  持有记忆库", "查看识别结果与原图证据。\n优先处理待解决项，保留每个持有副本。", "打开记忆库", ["memory-gui"]),
        ("04  本地模拟与比较", "选择结果库、主副记忆和场地。\n在浏览器里运行模拟、比较成绩。", "打开模拟网页", ["web-desktop"]),
    )):
        card = ttk.LabelFrame(steps, text=title, padding=18)
        card.grid(row=index // 2, column=index % 2, sticky="nsew", padx=(0, 12), pady=(0, 12))
        description = ttk.Label(card, text=detail, wraplength=330, style="Muted.TLabel")
        description.pack(anchor="w", fill="x", pady=(0, 14))
        description.bind("<Configure>", lambda event: event.widget.configure(wraplength=max(100, event.width)))
        ttk.Button(card, text=label, style="Accent.TButton", command=lambda a=args: open_tool(a)).pack(anchor="w", side="bottom")
    for index in range(2):
        steps.columnconfigure(index, weight=1, uniform="steps")
        steps.rowconfigure(index, weight=1)
    extras = ttk.Frame(body)
    extras.pack(fill="x", pady=(0, 12))
    ttk.Button(extras, text="竞技场截图识别", command=lambda: open_tool(["gui"])).pack(side="left")
    ttk.Button(extras, text="运行诊断", command=lambda: open_tool(["diagnose"])).pack(side="left", padx=8)
    def open_folder(path):
        try:
            os.startfile(path)
        except OSError as error:
            messagebox.showerror("无法打开目录", str(error), parent=window)
    ttk.Button(extras, text="数据目录", command=lambda: open_folder(home)).pack(side="right")
    ttk.Button(extras, text="日志目录", command=lambda: open_folder(home / "logs")).pack(side="right", padx=8)
    ttk.Label(body, textvariable=status, wraplength=820).pack(anchor="w")
    ttk.Label(body, text=f"数据位置：{home}\n各工具独立运行；关闭此工作台不会中断采集。模拟服务请从服务窗口停止。", style="Muted.TLabel", wraplength=820).pack(anchor="w", pady=(10, 0))
    window.mainloop()
    return 0


def web_window(port=8765):
    """Give a windowed executable's loopback server an explicit stop control."""
    import threading
    import tkinter as tk
    from tkinter import ttk
    import webbrowser
    from gkshot.simulation_web import SimulationServer
    # Stable origin preserves existing localStorage history between launches.
    try:
        server = SimulationServer(('127.0.0.1', port))
    except OSError as error:
        raise RuntimeError(f"无法启动本地服务，端口 {port} 可能已被占用。请关闭已有模拟服务窗口后重试。\n{error}") from error
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    from gkshot.capture.windows import dpi_aware
    dpi_aware()
    window = tk.Tk()
    from gkshot.ui_theme import apply_theme, heading, size_window
    apply_theme(window)
    window.title("GKTool · 本地模拟服务")
    size_window(window, 560, 290, 520, 280)
    body = ttk.Frame(window, padding=22)
    body.pack(fill="both", expand=True)
    heading(body, "本地模拟服务运行中", f"{server.origin} · 仅此电脑访问")
    ttk.Label(body, text="关闭浏览器不会停止服务。可重新打开网页，继续查看结果。", wraplength=500).pack(anchor="w")
    ttk.Button(body, text="打开模拟网页", style="Accent.TButton", command=lambda: webbrowser.open(server.origin)).pack(anchor="w", pady=12)
    status = tk.StringVar(value="停止服务前将取消尚未完成的模拟。")
    ttk.Label(body, textvariable=status, wraplength=500).pack(anchor="w", pady=4)
    stopping = False
    def stop():
        nonlocal stopping
        if stopping:
            return
        stopping = True
        status.set("正在取消模拟并停止服务…")
        server.shutdown()
        with server.guard:
            for job in list(server.jobs.values()) + list(server.searches.values()):
                if job.get('cancel'):
                    job['cancel'].set()
        def finish():
            with server.guard:
                busy = server.busy()
            if busy:
                window.after(100, finish)
            else:
                server.server_close()
                window.destroy()
        finish()
    ttk.Button(body, text="停止服务并关闭", command=stop).pack(anchor="e")
    window.protocol("WM_DELETE_WINDOW", stop)
    webbrowser.open(server.origin)
    window.mainloop()
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        _, home = prepare()
        if sys.stdout is None:
            sys.stdout = (home / "logs/desktop.log").open("a", encoding="utf-8", buffering=1)
        if sys.stderr is None:
            sys.stderr = sys.stdout
        if not args:
            return menu()
        if args[0] == "diagnose":
            return diagnose()
        if args[0] == "web-desktop":
            return web_window()
        if args[0] == "capture-offline":
            from gkshot.capture.gui import launch
            launch(initial_tab="offline")
            return 0
        if args[0] == "simulation":
            from gkshot.simulation_cli import main as run
            return run(args[1:])
        from gkshot.cli import main as run
        return run(args)
    except Exception as error:
        detail = traceback.format_exc()
        try:
            from gkshot.app_paths import USER_ROOT
            log = USER_ROOT / "logs/startup-error.txt"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(detail, encoding="utf-8")
            detail = f"{error}\n\n详细日志：{log}"
        except Exception:
            detail = str(error)
        if getattr(sys, "frozen", False) and os.environ.get("GKTOOL_NO_DIALOG") != "1":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, detail, "GKTool 启动失败", 0x10)
        else:
            print(detail, file=sys.stderr)
        return 1
