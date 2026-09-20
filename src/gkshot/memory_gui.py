"""A separate entry for owned memories. Never treats a six-card memory as a contest loadout."""
import copy
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from PIL import Image, ImageTk

from gkshot.catalog import Catalog, PROJECT_ROOT
from gkshot.memory_library import MemoryLibrary

STATUS = {"reviewed": "已人工核对", "auto-resolved":"已自动识别", "needs-review": "有待解决项", "incomplete": "缺少画面 / 有冲突"}


def unresolved_label(record):
    """Explain pending fields without overriding an authoritative review."""
    if record["status"] in ("reviewed", "auto-resolved"):
        return STATUS[record["status"]]
    report = record["report"]
    fields = report.get("automatic_check", {}).get("unresolved", [])
    fields = list(dict.fromkeys(fields + report.get("missing_fields", []) + report.get("conflicts", [])))
    groups = {"customizations":"卡片自定义", "card_ids":"卡片身份", "item_ids":"道具"}
    parameters = {"vocal":"Vo", "dance":"Da", "visual":"Vi", "stamina":"体力"}
    labels = []
    for field in fields:
        group, _, slot = field.partition(".")
        if group in groups and slot.isdigit():
            labels.append(f"{groups[group]} {int(slot)+1}")
        elif group == "parameters":
            labels.append("属性 "+parameters.get(slot,slot))
        elif group == "p_idol_id":
            labels.append("偶像版本")
        elif group != "loadout-validation":
            labels.append(field)
    return "待解决："+("、".join(labels) if labels else "数据缺失或不一致，请查看原图。")


def entity_id(text):
    return int(text.split("·", 1)[0].strip())


def selector(parent, variable, catalog, kind):
    choices = [catalog.label(kind, 0)] + [catalog.label(kind, key) for key, value in getattr(catalog, kind).items()
                                        if kind != "p_items" or value["mode"] == "stage"]
    box = ttk.Combobox(parent, textvariable=variable, values=choices, width=37)
    def filter_choices(event):
        if event.keysym not in ("Up", "Down", "Return", "Escape", "Tab"):
            box["values"] = [label for label in choices if variable.get().strip().lower() in label.lower()] or choices
    box.bind("<KeyRelease>", filter_choices)
    return box


class MemoryEditor:
    def __init__(self, parent, library, memory_id, catalog, on_saved):
        self.library, self.memory_id, self.catalog, self.on_saved = library, memory_id, catalog, on_saved
        self.record = library.get(memory_id)
        self.draft = copy.deepcopy((self.record["review"] or {}).get("draft", self.record["report"]["draft"]))
        self.window = tk.Toplevel(parent)
        from gkshot.ui_theme import apply_theme, size_window
        apply_theme(self.window)
        self.window.title("核对一份记忆")
        size_window(self.window,850,820)
        self.confirmed = tk.BooleanVar(value=False)
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        identity = self.record["report"]["identity"]
        ttk.Label(frame, text=f"{identity['title']} / {identity['idol_name']}  ·  综合力 {identity['power']}").pack(anchor="w")
        if self.record["status"] not in ("reviewed", "auto-resolved"):
            ttk.Label(frame, text=unresolved_label(self.record), wraplength=800).pack(anchor="w", pady=4)
        if self.record["status"] == "auto-resolved":
            ttk.Label(frame,text="本份已自动识别，无需再次确认；下方仅用于查看或手动修正。").pack(anchor="w",pady=4)
        ttk.Label(frame, text="每份记忆最多 6 张卡，不足六张是合法的；确认不存在的卡槽用 0 表示。场地与支援加成留给后续队伍配置。", wraplength=800).pack(anchor="w", pady=6)
        params = ttk.LabelFrame(frame, text="记忆自身属性", padding=8)
        params.pack(fill="x")
        self.parameters = {}
        for column, (key, label) in enumerate((("vocal", "Vo"), ("dance", "Da"), ("visual", "Vi"), ("stamina", "体力"))):
            ttk.Label(params, text=label).grid(row=0, column=column * 2)
            variable = tk.StringVar(value="" if self.draft["parameters"][key] is None else str(self.draft["parameters"][key]))
            self.parameters[key] = variable
            ttk.Entry(params, textvariable=variable, width=8).grid(row=0, column=column * 2 + 1, padx=8)
            variable.trace_add("write", self.invalidate)
        self.item_vars, self.card_vars, self.custom_buttons = [], [], []
        items = ttk.LabelFrame(frame, text="道具", padding=8)
        items.pack(fill="x", pady=8)
        for index, value in enumerate(self.draft["item_ids"]):
            row = ttk.Frame(items)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"道具 {index + 1}", width=10).pack(side="left")
            variable = tk.StringVar(value="" if value is None else catalog.label("p_items", value))
            self.item_vars.append(variable)
            selector(row, variable, catalog, "p_items").pack(side="left", fill="x", expand=True)
            variable.trace_add("write", self.invalidate)
        cards = ttk.LabelFrame(frame, text="卡片（名字末尾 + 表示强化；可输入名称筛选）", padding=8)
        cards.pack(fill="x")
        self.images = []
        for index, value in enumerate(self.draft["card_ids"]):
            row = ttk.Frame(cards)
            row.pack(fill="x", pady=4)
            crop = self.card_image(index)
            icon = ttk.Label(row, text=str(index + 1), width=5)
            icon.pack(side="left")
            if crop is not None:
                photo = ImageTk.PhotoImage(crop.resize((46, 46)))
                self.images.append(photo)
                icon.configure(image=photo)
            variable = tk.StringVar(value="" if value is None else catalog.label("skill_cards", value))
            self.card_vars.append(variable)
            selector(row, variable, catalog, "skill_cards").pack(side="left", fill="x", expand=True, padx=8)
            button = ttk.Button(row, width=17, command=lambda i=index: self.edit_custom(i))
            button.pack(side="right")
            self.custom_buttons.append(button)
            self.update_custom_label(index)
            variable.trace_add("write", lambda *args, i=index: self.card_changed(i))
        ttk.Button(frame, text="确认其余卡片无自定义", command=self.clear_unknown_custom).pack(anchor="w", pady=8)
        ttk.Checkbutton(frame, text="我已核对身份、数值、道具、卡片强化和所有自定义效果", variable=self.confirmed).pack(anchor="w")
        ttk.Button(frame, text="保存为已核对记忆", command=self.save).pack(anchor="e", pady=8)

    def invalidate(self, *args):
        self.confirmed.set(False)

    def card_changed(self, index):
        self.invalidate()
        self.draft["customizations"][index] = None
        self.update_custom_label(index)

    def update_custom_label(self, index):
        token = self.draft["customizations"][index]
        evidence = self.record["report"]["evidence"].get(f"customizations.{index}", [])
        automatic = token is not None and not self.record["review"] and any(e.get("value") == token and e.get("method") in ("tooltip-rule-match", "catalog-no-options", "central-green-counter") for e in evidence)
        text = "自定义未确定" if token is None else "已确认无自定义" if token == "" else "已设置自定义"
        if automatic:
            text = "规则判定：无自定义" if token == "" else "已自动识别自定义"
        self.custom_buttons[index].configure(text=text)

    def card_image(self, index):
        for page in reversed(self.record["report"]["pages"]):
            entry = next((card for card in page["cards"] if card["slot"] == index), None)
            if entry:
                with Image.open(self.library.source_path(page)) as source:
                    f = page["frame"]
                    image = source.convert("RGB").crop((f["left"], f["top"], f["right"], source.height)).resize(tuple(f["normalized_size"]))
                    return image.crop(entry["box"])
        return None

    def clear_unknown_custom(self):
        self.invalidate()
        for index, token in enumerate(self.draft["customizations"]):
            if token is None:
                self.draft["customizations"][index] = ""
                self.update_custom_label(index)

    def edit_custom(self, index):
        try:
            card_id = entity_id(self.card_vars[index].get())
            if card_id != 0 and card_id not in self.catalog.skill_cards:
                raise ValueError()
        except ValueError:
            messagebox.showerror("选择卡片", "请先选择有效卡片。", parent=self.window)
            return
        card = self.catalog.skill_cards.get(card_id, {})
        allowed = [int(value) for value in card.get("availableCustomizations", "").split(",") if value]
        dialog = tk.Toplevel(self.window)
        dialog.title("自定义效果等级")
        dialog.transient(self.window)
        dialog.grab_set()
        box = ttk.Frame(dialog, padding=16)
        box.pack()
        ttk.Label(box, text=self.catalog.label("skill_cards", card_id)).pack(anchor="w")
        ttk.Label(box, text="0 表示没有效果；请对照游戏中的卡片详情。", wraplength=430).pack(anchor="w", pady=8)
        old = {int(part.split("x")[0]): int(part.split("x")[1]) for part in (self.draft["customizations"][index] or "").split("e") if part}
        variables = {}
        for effect_id in allowed:
            effect = self.catalog.customizations[effect_id]
            row = ttk.Frame(box)
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=effect["name"], width=30).pack(side="left")
            variable = tk.StringVar(value=str(old.get(effect_id, 0)))
            variables[effect_id] = variable
            ttk.Spinbox(row, from_=0, to=effect["max"], width=5, textvariable=variable).pack(side="right")
        if not allowed:
            ttk.Label(box, text="该版本没有自定义选项；请确认卡片强化状态。", wraplength=430).pack(pady=6)
        def save():
            try:
                values = {key: int(variable.get()) for key, variable in variables.items()}
                if any(not 0 <= value <= self.catalog.customizations[key]["max"] for key, value in values.items()): raise ValueError()
            except ValueError:
                messagebox.showerror("等级", "等级须在允许范围内。", parent=dialog)
                return
            self.invalidate()
            self.draft["customizations"][index] = "e".join(f"{key}x{value}" for key, value in values.items() if value)
            self.update_custom_label(index)
            dialog.destroy()
        ttk.Button(box, text="确认", command=save).pack(anchor="e", pady=8)

    def save(self):
        try:
            draft = {"parameters": {key: int(v.get()) for key, v in self.parameters.items()},
                     "item_ids": [entity_id(v.get()) for v in self.item_vars], "card_ids": [entity_id(v.get()) for v in self.card_vars],
                     "customizations": self.draft["customizations"], "p_idol_id": self.draft["p_idol_id"]}
            p_ids = {self.catalog.skill_cards[value]["pIdolId"] for value in draft["card_ids"]
                     if value in self.catalog.skill_cards and self.catalog.skill_cards[value]["pIdolId"]}
            if len(p_ids) == 1: draft["p_idol_id"] = next(iter(p_ids))
            self.library.save_review(self.memory_id, draft, confirmed=self.confirmed.get(), catalog=self.catalog)
        except (ValueError, OSError) as error:
            messagebox.showerror("还不能保存", str(error), parent=self.window)
            return
        self.on_saved()
        self.window.destroy()


class MemoryLibraryWindow:
    def __init__(self, root, library=None):
        self.root = root
        self.library = library or MemoryLibrary()
        self.catalog = Catalog()
        self.scanner = None
        self.detail_scanner = None
        self.details, self.context = [], None
        self.busy = False
        self.results = queue.Queue()
        from gkshot.ui_theme import apply_theme, heading, size_window
        apply_theme(root)
        root.title("GKTool · 持有记忆库")
        size_window(root,1160,760,1000,700)
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        heading(frame, "持有记忆库", "03 查看结果与证据 · 已可用的记忆无需逐份确认，优先处理待解决项。")
        ttk.Label(frame, text=f"当前库：{self.library.root}", style="Muted.TLabel", wraplength=1080).pack(anchor="w")
        import_box = ttk.LabelFrame(frame, text="手动补充截图 · 同一份记忆的ステージ详情页", padding=8)
        import_box.pack(fill="x", pady=(10, 0))
        controls = ttk.Frame(import_box)
        controls.pack(fill="x", pady=12)
        self.buttons = []
        for title, command in (("选择详情截图（多选）", self.choose_details), ("选择列表定位图（可选）", self.choose_context), ("清除定位图", self.clear_context), ("导入这一份记忆", self.begin), ("试用新示例", self.sample)):
            if command == self.sample and not (PROJECT_ROOT / "tests/fixtures/memory-library").is_dir():
                continue
            button = ttk.Button(controls, text=title, command=command)
            button.pack(side="left", padx=(0, 8))
            self.buttons.append(button)
        self.selection = tk.StringVar(value="尚未选择详情截图。")
        ttk.Label(import_box, textvariable=self.selection, wraplength=1080).pack(anchor="w")
        self.status = tk.StringVar(value="已自动识别的记忆无需逐张确认；只有待解决项需要补图、补充规则或修正。")
        ttk.Label(frame, textvariable=self.status, wraplength=1100).pack(anchor="w", pady=10)
        filters = ttk.Frame(frame)
        filters.pack(fill="x", pady=(0, 8))
        ttk.Label(filters, text="查找记忆").pack(side="left")
        self.query = tk.StringVar()
        ttk.Entry(filters, textvariable=self.query, width=24).pack(side="left", padx=8)
        self.filter_status = tk.StringVar(value="全部")
        choice = ttk.Combobox(filters, textvariable=self.filter_status, values=("全部", "待解决", "可用于模拟"), state="readonly", width=14)
        choice.pack(side="left")
        self.count_text = tk.StringVar()
        ttk.Label(filters, textvariable=self.count_text, style="Muted.TLabel").pack(side="right")
        self.query.trace_add("write", lambda *args: self.refresh())
        choice.bind("<<ComboboxSelected>>", lambda event: self.refresh())
        grid = ttk.Frame(frame)
        grid.pack(fill="both", expand=True)
        # Treeview's default 20px row does not grow with the DPI-scaled font,
        # especially when this window is opened from the DPI-aware collector.
        style = ttk.Style(root)
        table_font = tkfont.Font(root=root, font=style.lookup("Treeview", "font") or "TkDefaultFont")
        line_height = table_font.metrics("linespace")
        style.configure("MemoryLibrary.Treeview", rowheight=line_height + max(6, round(line_height * 0.3)))
        self.table = ttk.Treeview(grid, style="MemoryLibrary.Treeview", columns=("identity", "power", "parameters", "status"), show="headings", selectmode="browse")
        for name, title, width in (("identity", "记忆", 390), ("power", "综合力", 90), ("parameters", "Vo / Da / Vi / 体力", 250), ("status", "状态", 260)):
            self.table.heading(name, text=title)
            self.table.column(name, width=width)
        scroll = ttk.Scrollbar(grid, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<Double-1>", lambda e: self.edit())
        self.table.bind("<<TreeviewSelect>>",lambda e:self.show_unresolved())
        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=12)
        ttk.Button(footer, text="核对选中记忆", command=self.edit).pack(side="left")
        ttk.Button(footer, text="查看归档原图", command=self.view_sources).pack(side="left", padx=10)
        detail_button = ttk.Button(footer, text="读取选中记忆的卡片详情", command=self.choose_card_details)
        detail_button.pack(side="left")
        self.buttons.append(detail_button)
        ttk.Button(footer, text="查看最近采集结果", command=self.open_capture_results).pack(side="left",padx=10)
        ttk.Label(frame, text=f"记忆库保存在：{self.library.root}", wraplength=1100).pack(anchor="w")
        self.refresh()
        root.after(100, self.poll)

    def open_capture_results(self):
        from gkshot.capture.process import latest_result_library
        from gkshot.capture.store import DEFAULT_CAPTURES
        path = latest_result_library(DEFAULT_CAPTURES)
        if path is None:
            messagebox.showinfo("暂无采集结果", "尚无成功入库的离线采集结果。请在采集器里查看离线处理日志。",parent=self.root)
            return
        MemoryLibraryWindow(tk.Toplevel(self.root), MemoryLibrary(path))

    def choose_details(self):
        paths = filedialog.askopenfilenames(parent=self.root, title="选择同一份记忆的详情上半页和下半页", filetypes=[("截图", "*.png *.jpg *.jpeg *.webp")])
        if paths:
            self.details = list(map(Path, paths))
            self.context = None
            self.update_selection()

    def choose_context(self):
        path = filedialog.askopenfilename(parent=self.root, title="选择列表中选中这份记忆时的截图", filetypes=[("截图", "*.png *.jpg *.jpeg *.webp")])
        if path:
            self.context = Path(path)
            self.update_selection()

    def clear_context(self):
        self.context = None
        self.update_selection()

    def update_selection(self):
        self.selection.set(f"详情图 {len(self.details)} 张：" + "、".join(p.name for p in self.details) + f"；定位图：{self.context.name if self.context else '未选择'}")

    def sample(self):
        folder = PROJECT_ROOT / "tests/fixtures/memory-library"
        self.details = [folder / "detail-top.png", folder / "detail-bottom.png"]
        self.context = folder / "list.png"
        self.update_selection()
        self.begin()

    def begin(self):
        if self.busy:
            return
        if not self.details:
            messagebox.showinfo("选择截图", "请先选择同一份记忆的详情截图。", parent=self.root)
            return
        self.busy = True
        for button in self.buttons: button.configure(state="disabled")
        self.status.set("正在识别并合并详情页，原图会归档到记忆库……")
        details, context = self.details[:], self.context
        def work():
            try:
                from gkshot.recognition.memory import MemoryScanner
                if self.scanner is None: self.scanner = MemoryScanner()
                report = self.scanner.scan(details, context_path=context)
                result = self.library.import_report(report)
                self.results.put((result, None))
            except Exception as error:
                self.results.put((None, str(error)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            result, error = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            for button in self.buttons: button.configure(state="normal")
            if error:
                self.status.set("导入未完成：" + error)
                messagebox.showerror("导入未完成", error, parent=self.root)
            else:
                self.refresh()
                if self.table.exists(result["memory_id"]):
                    self.table.selection_set(result["memory_id"])
                if result.get("operation") == "card-details":
                    self.status.set(result["summary"])
                    if result.get("errors"):
                        messagebox.showwarning("部分详情需要补采", "\n".join(result["errors"]), parent=self.root)
                else:
                    self.status.set(("已导入一份记忆。" if result["created"] else "这组截图已入库，未重复创建。") + "可继续读取卡片详情，自动填写支持的自定义效果。")
        if self.root.winfo_exists(): self.root.after(100, self.poll)

    def choose_card_details(self):
        if self.busy:
            return
        memory_id = self.selected()
        if not memory_id:
            messagebox.showinfo("选择记忆", "请先选中所属记忆，再选择点击卡片后展开详情的截图。", parent=self.root)
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="选择同一份记忆的卡片浮动详情截图", filetypes=[("截图", "*.png *.jpg *.jpeg *.webp")])
        if paths:
            self.begin_card_details(memory_id, list(map(Path, paths)))

    def begin_card_details(self, memory_id, paths):
        if self.busy:
            return
        self.busy = True
        for button in self.buttons: button.configure(state="disabled")
        self.status.set("正在本地读取卡片详情、匹配强化规则；不需要联网……")
        def work():
            results, errors = [], []
            try:
                from gkshot.recognition.card_detail import CardDetailScanner
                if self.detail_scanner is None:
                    self.detail_scanner = CardDetailScanner(self.scanner)
                    self.scanner = self.detail_scanner.memory
                for path in paths:
                    try:
                        report = self.detail_scanner.scan(path)
                        result = self.library.add_card_detail(memory_id, report, catalog=self.catalog)
                        results.append(result)
                        if result["detail_status"] == "conflict":
                            errors.append(f"{path.name}：与已有强化证据冲突，保留为未知，请补采。")
                        elif report["status"] != "matched":
                            errors.append(f"{path.name}：{report['reason']}")
                    except Exception as error:
                        errors.append(f"{path.name}：{error}")
                count = sum(r["applied"] for r in results)
                summary = f"详情处理完成：自动填写 {count} 个强化字段，{len(errors)} 张需要补采或补充规则。已人工核对的记录保留原值。"
                self.results.put(({"operation": "card-details", "memory_id": memory_id, "summary": summary, "errors": errors}, None))
            except Exception as error:
                self.results.put((None, str(error)))
        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        selection = self.selected()
        for item in self.table.get_children(): self.table.delete(item)
        records = self.library.list()
        ready = sum(record["status"] in ("auto-resolved", "reviewed") for record in records)
        shown = 0
        for record in records:
            identity = record["report"]["identity"]
            usable = record["status"] in ("auto-resolved", "reviewed")
            if self.filter_status.get() == "待解决" and usable: continue
            if self.filter_status.get() == "可用于模拟" and not usable: continue
            if self.query.get().strip().casefold() not in f"{identity['title']} {identity['idol_name']} {record['memory_id']}".casefold(): continue
            draft = (record["review"] or {}).get("draft", record["report"]["draft"])
            params = " / ".join(str(draft["parameters"][key]) if draft["parameters"][key] is not None else "?" for key in ("vocal", "dance", "visual", "stamina"))
            self.table.insert("", "end", iid=record["memory_id"], values=(f"{identity['title']} / {identity['idol_name']}", identity["power"], params, unresolved_label(record)))
            shown += 1
        self.count_text.set(f"显示 {shown} / {len(records)} 份 · 可用 {ready} · 待解决 {len(records)-ready}")
        if selection and self.table.exists(selection):self.table.selection_set(selection)
        if not records:
            self.status.set("这个库还没有记忆。请打开最近采集结果，或在上方导入同一份记忆的详情截图。")
        elif not shown:
            self.status.set("没有符合条件的记忆。可清空查找文字或切换为「全部」。")
        elif not self.selected():
            self.status.set("选择一份记忆查看待解决项；双击可查看或核对详情。可用数量不代表自动识别准确率。")

    def selected(self):
        selection = self.table.selection()
        return selection[0] if selection else None

    def show_unresolved(self):
        if not self.selected():return
        record=self.library.get(self.selected())
        if record["status"] in ("auto-resolved","reviewed"):
            self.status.set(STATUS[record["status"]]+"：无需逐张确认，可查看原图或按需修正。")
            return
        self.status.set(unresolved_label(record))

    def edit(self):
        if not self.busy and self.selected(): MemoryEditor(self.root, self.library, self.selected(), self.catalog, self.refresh)

    def view_sources(self):
        if not self.selected(): return
        record = self.library.get(self.selected())
        sources = record["report"]["pages"] + ([record["report"]["list_context"]] if record["report"].get("list_context") else []) + record["report"].get("card_details", [])
        window = tk.Toplevel(self.root)
        window.title("归档原图 · 可滚动查看")
        window.geometry("760x850")
        notebook = ttk.Notebook(window)
        notebook.pack(fill="both", expand=True)
        window.photos = []
        for index, source in enumerate(sources):
            page = ttk.Frame(notebook)
            notebook.add(page, text=f"卡片 {source['slot'] + 1} 弹框" if source.get("kind") == "card-detail" else "列表定位" if source.get("role") == "list-context" else f"详情 {index + 1}")
            with Image.open(self.library.source_path(source)) as image:
                preview = image.convert("RGB")
                preview = preview.resize((700, round(preview.height * 700 / preview.width)))
            photo = ImageTk.PhotoImage(preview)
            window.photos.append(photo)
            canvas = tk.Canvas(page, highlightthickness=0)
            scrollbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
            scrollbar.pack(side="right", fill="y")
            canvas.pack(fill="both", expand=True)
            canvas.configure(yscrollcommand=scrollbar.set, scrollregion=(0, 0, preview.width, preview.height))
            canvas.create_image(0, 0, anchor="nw", image=photo)


def launch():
    from gkshot.capture.windows import dpi_aware
    dpi_aware()
    root = tk.Tk()
    ttk.Style(root).theme_use("clam")
    MemoryLibraryWindow(root)
    root.mainloop()
