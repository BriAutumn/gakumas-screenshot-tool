"""Local Tk review window. Recognition runs off the UI thread; no server or uploads."""
import json
import queue
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from gkshot.adapters.gktools import GKToolsAdapter
from gkshot.catalog import Catalog, PROJECT_ROOT
from gkshot.review import reviewed_loadout


class ReviewWindow:
    def __init__(self, root, base_url):
        self.root, self.adapter = root, GKToolsAdapter(base_url)
        self.catalog = Catalog()
        self.scanner = None
        self.report = None
        self.screenshot = None
        self.busy = False
        self.results = queue.Queue()
        self.images = []
        self.custom_tokens = [[None] * 6 for _ in range(2)]
        self.confirmed = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="请选择与示例裁剪范围一致的竞技场编成截图。")
        root.title("学マス截图助手 · A 阶段预览版")
        root.geometry("1220x860")
        root.minsize(1080, 720)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=5)
        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x")
        self.select_button = ttk.Button(toolbar, text="选择截图并识别", command=self.choose)
        self.select_button.pack(side="left")
        self.sample_button = ttk.Button(toolbar, text="试用示例截图", command=lambda: self.begin(PROJECT_ROOT / "tests/fixtures/original-screenshot.png"))
        if (PROJECT_ROOT / "tests/fixtures/original-screenshot.png").is_file():
            self.sample_button.pack(side="left", padx=8)
        ttk.Button(toolbar, text="持有记忆库", command=self.open_memory_library).pack(side="left", padx=8)
        ttk.Label(toolbar, text="识别在本机运行 · 不上传截图").pack(side="right")
        ttk.Label(outer, textvariable=self.status, wraplength=1160).pack(fill="x", pady=(10, 8))
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, width=520)
        left.pack(side="left", fill="y", padx=(0, 16))
        left.pack_propagate(False)
        self.preview = ttk.Label(left, text="截图预览", anchor="center")
        self.preview.pack(fill="x")
        self.param_vars = {}
        params = ttk.LabelFrame(left, text="属性：请核对黑色数字与旁边的比较差值", padding=10)
        params.pack(fill="x", pady=10)
        for i, (key, label) in enumerate((("vocal", "Vo"), ("dance", "Da"), ("visual", "Vi"), ("stamina", "体力"))):
            ttk.Label(params, text=label).grid(row=0, column=i, padx=6)
            variable = tk.StringVar()
            self.param_vars[key] = variable
            ttk.Entry(params, textvariable=variable, width=9).grid(row=1, column=i, padx=6)
            variable.trace_add("write", self.invalidate)
        self.delta_label = ttk.Label(params, text="差值另行显示，不自动叠加。", wraplength=470)
        self.delta_label.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        settings = ttk.LabelFrame(left, text="截图外信息", padding=10)
        settings.pack(fill="x", pady=(0, 10))
        ttk.Label(settings, text="竞技场场地").grid(row=0, column=0, sticky="w")
        self.stage_var = tk.StringVar()
        self.stage = ttk.Combobox(settings, textvariable=self.stage_var, width=40)
        self.stage["values"] = [f"{s['id']} · 第{s['season']}赛季 / 场地{s['stage']}"
                                for s in reversed(list(self.catalog.stages.values())) if s["type"] == "contest"]
        self.stage.grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        self.stage_var.trace_add("write", self.invalidate)
        ttk.Label(settings, text="支援加成（%）").grid(row=2, column=0, sticky="w")
        self.bonus_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.bonus_var, width=12).grid(row=2, column=1, sticky="w")
        self.bonus_var.trace_add("write", self.invalidate)
        ttk.Label(left, text="固定道具区不计入下面的 4 个普通道具槽；请选对场地。\n强化 / 自定义角标需要核对，尤其是低分候选。\n目前仅支持示例裁剪布局和等比例缩放。", wraplength=500).pack(fill="x", pady=5)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        canvas = tk.Canvas(right, highlightthickness=0)
        scrollbar = ttk.Scrollbar(right, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        rows = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=rows, anchor="nw")
        rows.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.item_vars, self.card_vars, self.custom_buttons, self.crop_labels, self.result_labels = [], [], [], [], []
        self.combos = []
        for group, title in enumerate(("普通道具（4 槽）", "主卡组（6 张）", "副卡组（6 张）")):
            section = ttk.LabelFrame(rows, text=title, padding=7)
            section.pack(fill="x", pady=(0, 10))
            count = 4 if group == 0 else 6
            for index in range(count):
                row = ttk.Frame(section)
                row.pack(fill="x", pady=3)
                icon = ttk.Label(row, text=str(index + 1), width=5)
                icon.pack(side="left")
                self.crop_labels.append(icon)
                variable = tk.StringVar()
                kind = "p_items" if group == 0 else "skill_cards"
                choices = [self.catalog.label(kind, 0)] + [self.catalog.label(kind, i)
                           for i, item in getattr(self.catalog, kind).items()
                           if kind != "p_items" or item["mode"] == "stage"]
                combo = ttk.Combobox(row, textvariable=variable, values=choices, width=26)
                combo.pack(side="left", fill="x", expand=True, padx=5)
                combo.bind("<KeyRelease>", lambda e, c=combo, all_choices=choices: self.filter_choices(c, all_choices, e))
                self.combos.append((combo, choices))
                if group == 0:
                    self.item_vars.append(variable)
                    variable.trace_add("write", self.invalidate)
                else:
                    r, c = group - 1, index
                    self.card_vars.append(variable)
                    button = ttk.Button(row, text="自定义待核对", width=14, command=lambda r=r, c=c: self.edit_custom(r, c))
                    button.pack(side="right")
                    self.custom_buttons.append(button)
                    ttk.Button(row, text="±强化", width=6, command=lambda r=r, c=c: self.toggle_upgrade(r, c)).pack(side="right")
                    variable.trace_add("write", lambda *args, r=r, c=c: self.card_changed(r, c))
                evidence = ttk.Label(section, text="", foreground="#795500", wraplength=580)
                evidence.pack(fill="x")
                self.result_labels.append(evidence)
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(8, 0))
        ttk.Button(footer, text="确认其余卡片无自定义", command=self.clear_unreviewed_custom).pack(side="left")
        ttk.Checkbutton(footer, text="我已核对所有数值、道具、卡片强化与自定义效果", variable=self.confirmed).pack(side="left", padx=12)
        self.export_button = ttk.Button(footer, text="保存编成并生成链接", command=self.export, state="disabled")
        self.export_button.pack(side="right")
        self.link_var = tk.StringVar()
        output = ttk.Frame(outer)
        output.pack(fill="x", pady=(8, 0))
        ttk.Entry(output, textvariable=self.link_var, state="readonly").pack(side="left", fill="x", expand=True)
        self.open_button = ttk.Button(output, text="打开 GKTools", command=self.open_url, state="disabled")
        self.open_button.pack(side="right", padx=(8, 0))
        root.after(100, self.poll)

    def open_memory_library(self):
        from gkshot.memory_gui import MemoryLibraryWindow
        MemoryLibraryWindow(tk.Toplevel(self.root))

    def invalidate(self, *args):
        self.confirmed.set(False)
        if hasattr(self, "link_var"):
            self.link_var.set("")
            self.open_button.configure(state="disabled")

    @staticmethod
    def filter_choices(combo, choices, event):
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):
            return
        query = combo.get().strip().lower()
        combo["values"] = [value for value in choices if query in value.lower()] or choices

    @staticmethod
    def entity_id(value):
        return int(value.split("·", 1)[0].strip())

    def choose(self):
        path = filedialog.askopenfilename(title="选择编成截图", filetypes=[("图片", "*.png *.jpg *.jpeg *.webp"), ("所有文件", "*.*")])
        if path:
            self.begin(Path(path))

    def begin(self, path):
        if self.busy:
            return
        self.busy = True
        self.report = None
        self.invalidate()
        self.export_button.configure(state="disabled")
        self.select_button.configure(state="disabled")
        self.sample_button.configure(state="disabled")
        self.status.set("正在读取图片并识别；首次加载模型通常需要数秒……")
        def work():
            try:
                from gkshot.recognition.automatic import ScreenshotScanner
                if self.scanner is None:
                    self.scanner = ScreenshotScanner(catalog=self.catalog)
                self.results.put((path, self.scanner.scan(path), None))
            except Exception as error:
                self.results.put((path, None, str(error)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            path, report, error = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.select_button.configure(state="normal")
            self.sample_button.configure(state="normal")
            if error:
                self.status.set("识别未完成：" + error)
                messagebox.showerror("识别未完成", error)
            else:
                self.display(path, report)
        self.root.after(100, self.poll)

    def display(self, path, report):
        self.screenshot, self.report = path, report
        with Image.open(path) as source:
            preview = source.convert("RGB")
            preview.thumbnail((510, 370))
            self.preview_image = ImageTk.PhotoImage(preview)
            self.preview.configure(image=self.preview_image, text="")
            normalized = source.convert("RGB").resize(tuple(self.scanner.layout["reference_size"]))
        boxes = self.scanner.layout["items"] + [box for row in self.scanner.layout["cards"] for box in row]
        self.images = [ImageTk.PhotoImage(normalized.crop(box).resize((32, 32))) for box in boxes]
        for label, image in zip(self.crop_labels, self.images):
            label.configure(image=image, text="")
        for name, result in report["numbers"].items():
            self.param_vars[name].set("" if result["value"] is None else str(result["value"]))
        deltas = report["displayed_deltas"]
        self.delta_label.configure(text="图中比较差值（未叠加）：" + " / ".join(f"{key} {value['value']:+}" if value['value'] is not None else f"{key} 未读出" for key, value in deltas.items()))
        records = report["items"] + [card for row in report["cards"] for card in row]
        for index, (variable, record) in enumerate(zip(self.item_vars + self.card_vars, records)):
            kind = "p_items" if index < 4 else "skill_cards"
            variable.set(self.catalog.label(kind, record["id"]))
            combo, choices = self.combos[index]
            candidates = [self.catalog.label(kind, candidate["id"]) for candidate in record["candidates"]]
            combo["values"] = list(dict.fromkeys([variable.get()] + candidates + choices))
            if record["method"] == "same-image-duplicate":
                hint = f"图像与主卡 {record['duplicate_of']} 相似；强化与自定义仍需核对。"
            elif record["method"] == "empty-slot":
                hint = "检测为空槽，请核对。"
            else:
                hint = "候选：" + " / ".join(self.catalog.label(kind, c["id"]) for c in record["candidates"][:2])
            self.result_labels[index].configure(text=hint)
        self.status.set("识别完成：右侧可修改候选。请填写场地与支援加成，并核对强化 / 自定义后导出。")
        self.export_button.configure(state="normal")

    def card_changed(self, row, column):
        self.invalidate()
        self.custom_tokens[row][column] = None
        index = row * 6 + column
        if index < len(self.custom_buttons):
            self.custom_buttons[index].configure(text="自定义待核对")

    def toggle_upgrade(self, row, column):
        try:
            index = row * 6 + column
            current = self.catalog.skill_cards[self.entity_id(self.card_vars[index].get())]
            target_name = current["name"].removesuffix("+") + ("" if current["upgraded"] else "+")
            target = next(card for card in self.catalog.skill_cards.values()
                          if card["name"] == target_name and card["pIdolId"] == current["pIdolId"])
            self.card_vars[index].set(self.catalog.label("skill_cards", target["id"]))
        except (KeyError, ValueError, StopIteration):
            messagebox.showinfo("强化", "未找到对应的强化 / 未强化卡，请从列表中选择。")

    def edit_custom(self, row, column):
        try:
            entity_id = self.entity_id(self.card_vars[row * 6 + column].get())
            card = self.catalog.skill_cards.get(entity_id, {})
            allowed = [int(value) for value in card.get("availableCustomizations", "").split(",") if value]
        except ValueError:
            messagebox.showerror("自定义", "请先选择有效卡片。")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("核对卡片自定义效果")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=self.catalog.label("skill_cards", entity_id)).pack(anchor="w")
        ttk.Label(frame, text="按截图 / 游戏详情设置各效果等级；0 表示没有该效果。", wraplength=430).pack(anchor="w", pady=8)
        existing = {int(part.split("x")[0]): int(part.split("x")[1])
                    for part in (self.custom_tokens[row][column] or "").split("e") if part}
        variables = {}
        for effect_id in allowed:
            effect = self.catalog.customizations[effect_id]
            line = ttk.Frame(frame)
            line.pack(fill="x", pady=4)
            ttk.Label(line, text=effect["name"], width=30).pack(side="left")
            variable = tk.StringVar(value=str(existing.get(effect_id, 0)))
            variables[effect_id] = variable
            ttk.Spinbox(line, from_=0, to=effect["max"], width=5, textvariable=variable).pack(side="right")
        if not allowed:
            ttk.Label(frame, text="该卡没有可用自定义；若截图有自定义角标，请先检查强化状态。", wraplength=430).pack(pady=8)
        def save():
            try:
                levels = {effect: int(variable.get()) for effect, variable in variables.items()}
                if any(not 0 <= level <= self.catalog.customizations[effect]["max"] for effect, level in levels.items()):
                    raise ValueError()
            except ValueError:
                messagebox.showerror("等级", "请输入允许范围内的整数。", parent=dialog)
                return
            token = "e".join(f"{effect}x{level}" for effect, level in levels.items() if level)
            self.set_custom(row, column, token)
            dialog.destroy()
        ttk.Button(frame, text="确认并保存", command=save).pack(anchor="e", pady=(14, 0))

    def set_custom(self, row, column, token):
        self.invalidate()
        self.custom_tokens[row][column] = token
        self.custom_buttons[row * 6 + column].configure(text="已设置自定义" if token else "已确认无自定义")

    def clear_unreviewed_custom(self):
        if self.report is None:
            return
        for row in range(2):
            for column in range(6):
                if self.custom_tokens[row][column] is None:
                    self.set_custom(row, column, "")
        self.status.set("已将尚未设置的卡片标为无自定义；有角标的卡片请点对应按钮设置。")

    def export(self):
        if self.report is None:
            return
        try:
            draft = {
                "schema_version": 1,
                "stage_id": self.entity_id(self.stage_var.get()) if self.stage_var.get().strip() else None,
                "support_bonus": float(self.bonus_var.get()) / 100 if self.bonus_var.get().strip() else None,
                "parameters": {name: int(variable.get()) for name, variable in self.param_vars.items()},
                "item_ids": [self.entity_id(variable.get()) for variable in self.item_vars],
                "card_ids": [[self.entity_id(variable.get()) for variable in self.card_vars[i:i + 6]] for i in (0, 6)],
                "customizations": self.custom_tokens,
            }
            loadout = reviewed_loadout(draft, confirmed=self.confirmed.get(), catalog=self.catalog)
            url = self.adapter.to_url(loadout)
            folder = PROJECT_ROOT / "work" / "exports" / str(time.time_ns())
            folder.mkdir(parents=True)
            (folder / "loadout.json").write_text(json.dumps(loadout.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (folder / "recognition-report.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (folder / "review.json").write_text(json.dumps({"image_sha256": self.report["image_sha256"], "reviewed": True,
                "recognition_revision": self.report["recognition_revision"], "url": url}, indent=2) + "\n", encoding="utf-8")
            self.link_var.set(url)
            self.open_button.configure(state="normal")
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.status.set(f"编成已保存，链接已复制。文件位置：{folder}")
        except (ValueError, KeyError, OSError) as error:
            messagebox.showerror("还不能导出", str(error))

    def open_url(self):
        if self.link_var.get():
            webbrowser.open(self.link_var.get())


def launch(base_url):
    root = tk.Tk()
    ReviewWindow(root, base_url)
    root.mainloop()
