"""Shared local Tk presentation; no assets or runtime downloads."""
from tkinter import font, ttk


def apply_theme(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        font.nametofont(name, root=root).configure(family="Microsoft YaHei UI", size=10)
    root.configure(background="#f3f5f9")
    style.configure(".", background="#f3f5f9", foreground="#202e43")
    style.configure("TButton", padding=(12, 7))
    style.configure("TNotebook.Tab", padding=(16, 8))
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
    style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
    style.configure("Muted.TLabel", foreground="#59677b")
    style.configure("Accent.TButton", background="#265cc5", foreground="white")
    style.map("Accent.TButton", background=[("disabled", "#dfe5ef"), ("active", "#19499f")],
              foreground=[("disabled", "#788496")])
    style.configure("TLabelframe", padding=12)
    style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("TEntry", fieldbackground="white")
    style.configure("Treeview", background="white", fieldbackground="white")


def heading(parent, title, subtitle):
    ttk.Label(parent, text=title, style="Title.TLabel").pack(anchor="w")
    ttk.Label(parent, text=subtitle, style="Muted.TLabel", wraplength=820).pack(anchor="w", pady=(4, 16))


def size_window(root, width, height, minimum_width=None, minimum_height=None):
    """Tk fonts scale with DPI; explicit window geometry otherwise does not."""
    scale = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))
    available_width = root.winfo_screenwidth() - 60
    available_height = root.winfo_screenheight() - 100
    width_px = min(round(width * scale), available_width)
    height_px = min(round(height * scale), available_height)
    root.geometry(f"{width_px}x{height_px}")
    root.minsize(min(round((minimum_width or width) * scale), width_px),
                 min(round((minimum_height or height) * scale), height_px))
