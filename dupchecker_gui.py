#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重复文件检查器 - 原生桌面版
===========================
纯 Python 标准库（tkinter）实现的独立窗口程序，不需要浏览器、不需要联网、
不依赖任何第三方库。

用法:  python dupchecker_gui.py
打包:  pyinstaller --onefile --windowed --name 重复文件检查器 dupchecker_gui.py
"""

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from dupcore import (ScanCancelled, delete_files, human_size, scan_folder)
except ImportError:  # 打包成单文件时同目录导入
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dupcore import (ScanCancelled, delete_files, human_size, scan_folder)

APP_TITLE = "重复文件检查器"
CHECKED = "\u2611"      # ☑
UNCHECKED = "\u2610"    # ☐

# 配色（浅色，贴近 Windows 11 观感）
C_BG = "#f5f6f8"
C_CARD = "#ffffff"
C_BORDER = "#e2e5ea"
C_TEXT = "#1f2328"
C_MUTED = "#6b7280"
C_ACCENT = "#2563eb"
C_DANGER = "#dc2626"
C_HASH = "#0f766e"
C_NAME = "#b45309"


def resource_path(rel):
    """兼容 PyInstaller 单文件：运行时从临时目录取资源。"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, rel)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)


def enable_dpi_awareness():
    """高分屏下避免界面模糊。"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Win8.1+
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()       # Win7
    except Exception:
        pass


def set_app_user_model_id():
    """让任务栏显示程序自身图标，而不是 Python/tkinter 默认图标。"""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "DupChecker.Application.1"
        )
    except Exception:
        pass


def reveal_in_explorer(path):
    """在资源管理器中定位文件。"""
    try:
        if os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            messagebox.showwarning(APP_TITLE, "文件已不存在：\n" + path)
    except Exception as e:
        messagebox.showerror(APP_TITLE, "无法打开资源管理器：\n%s" % e)


class DupCheckerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1000x680")
        self.minsize(860, 560)
        self.configure(bg=C_BG)

        self.scan_thread = None
        self.cancel_flag = threading.Event()
        self.msg_queue = queue.Queue()
        self.groups = []
        self.checked = set()        # 已勾选的文件路径
        self.item_path = {}         # treeview item id -> 文件路径
        self.group_items = []       # 组节点 item id 列表
        self._last_dir = ""

        self._set_window_icon()
        self._build_style()
        self._build_ui()
        self._bind_events()
        self.after(80, self._pump_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_window_icon(self):
        """
        按容器尺寸加载单一多尺寸 ICO，实现等比缩放、无拉伸。

        Windows 会按当前 DPI 下的标题栏/任务栏图标尺寸（SM_CXSMICON、SM_CXICON 等）
        从 ICO 中挑选最合适的内嵌档位；如果精确尺寸缺失，LoadImageW 也会保持宽高比
        等比缩放，而不是生硬拉伸。
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32

            ico = resource_path("appicon.ico")
            if not os.path.exists(ico):
                return

            hwnd = ctypes.c_void_p(self.winfo_id())
            user32.LoadImageW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            ]
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.SendMessageW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_void_p, ctypes.c_void_p,
            ]
            user32.SendMessageW.restype = ctypes.c_void_p

            # 容器尺寸：标题栏小图标 / 任务栏大图标（已随 DPI 缩放）
            cx_small = user32.GetSystemMetrics(49)   # SM_CXSMICON
            cy_small = user32.GetSystemMetrics(50)   # SM_CYSMICON
            cx_big = user32.GetSystemMetrics(11)     # SM_CXICON
            cy_big = user32.GetSystemMetrics(12)     # SM_CYICON

            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x10

            # 按容器尺寸加载图标，ICO 内含 16/24/32/40/48/56/64/72/96/128/256，
            # 任意 DPI 下都能找到匹配档，避免变形。
            h_big = user32.LoadImageW(None, ico, IMAGE_ICON, cx_big, cy_big, LR_LOADFROMFILE)
            h_small = user32.LoadImageW(None, ico, IMAGE_ICON, cx_small, cy_small, LR_LOADFROMFILE)

            # ICON_BIG=1（任务栏/Alt+Tab），ICON_SMALL=0（标题栏/窗口角标）
            if h_big:
                user32.SendMessageW(hwnd, 0x80, ctypes.c_void_p(1), h_big)
            if h_small:
                user32.SendMessageW(hwnd, 0x80, ctypes.c_void_p(0), h_small)

            # 设置类图标，防止 tkinter 后续刷新把图标覆盖回去
            if h_big:
                user32.SetClassLongPtrW(hwnd, -14, h_big)    # GCLP_HICON
            if h_small:
                user32.SetClassLongPtrW(hwnd, -34, h_small)  # GCLP_HICONSM

            # iconbitmap 兜底：确保边框、任务栏预览等位置也使用同一 ICO
            self.iconbitmap(ico)
        except Exception:
            pass

    # ------------------------------------------------------------------ 样式
    def _build_style(self):
        st = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            if theme in st.theme_names():
                st.theme_use(theme)
                break
        base = ("Microsoft YaHei UI", 10)
        self.option_add("*Font", base)
        st.configure(".", background=C_BG, foreground=C_TEXT, font=base)
        st.configure("Card.TFrame", background=C_CARD, relief="flat")
        st.configure("TFrame", background=C_BG)
        st.configure("TLabel", background=C_BG, foreground=C_TEXT)
        st.configure("Card.TLabel", background=C_CARD, foreground=C_TEXT)
        st.configure("Muted.TLabel", background=C_BG, foreground=C_MUTED,
                     font=("Microsoft YaHei UI", 9))
        st.configure("CardMuted.TLabel", background=C_CARD, foreground=C_MUTED,
                     font=("Microsoft YaHei UI", 9))
        st.configure("Title.TLabel", background=C_BG, foreground=C_TEXT,
                     font=("Microsoft YaHei UI", 15, "bold"))
        st.configure("TCheckbutton", background=C_CARD)
        st.configure("Bg.TCheckbutton", background=C_BG)
        st.configure("TScale", background=C_BG)
        st.configure("Treeview", rowheight=26, fieldbackground=C_CARD,
                     background=C_CARD, borderwidth=0,
                     font=("Microsoft YaHei UI", 9))
        st.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        st.map("Treeview", background=[("selected", "#dbeafe")],
               foreground=[("selected", C_TEXT)])
        st.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    # --------------------------------------------------------------- 界面搭建
    def _build_ui(self):
        pad = dict(padx=16)

        # ---------- 标题 ----------
        head = ttk.Frame(self)
        head.pack(fill="x", pady=(14, 6), **pad)
        ttk.Label(head, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="按内容哈希或文件名相似度找出重复文件",
                  style="Muted.TLabel").pack(side="left", padx=(10, 0), pady=(6, 0))

        # ---------- 路径选择 ----------
        row = ttk.Frame(self)
        row.pack(fill="x", pady=(6, 8), **pad)
        ttk.Label(row, text="文件夹").pack(side="left")
        self.var_folder = tk.StringVar()
        self.ent_folder = ttk.Entry(row, textvariable=self.var_folder)
        self.ent_folder.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self.btn_browse = ttk.Button(row, text="浏览…", width=9,
                                     command=self.on_browse)
        self.btn_browse.pack(side="left")
        self.btn_scan = ttk.Button(row, text="开始扫描", width=11,
                                   style="Accent.TButton", command=self.on_scan)
        self.btn_scan.pack(side="left", padx=(8, 0))

        # ---------- 选项 ----------
        opt = ttk.Frame(self)
        opt.pack(fill="x", pady=(0, 10), **pad)
        self.var_recursive = tk.BooleanVar(value=False)
        self.var_hash = tk.BooleanVar(value=True)
        self.var_name = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="包含子文件夹", variable=self.var_recursive,
                        style="Bg.TCheckbutton").pack(side="left")
        ttk.Checkbutton(opt, text="按内容判重", variable=self.var_hash,
                        style="Bg.TCheckbutton").pack(side="left", padx=(16, 0))
        ttk.Checkbutton(opt, text="按文件名判重", variable=self.var_name,
                        command=self._sync_threshold_state,
                        style="Bg.TCheckbutton").pack(side="left", padx=(16, 0))

        ttk.Label(opt, text="名称相似度").pack(side="left", padx=(24, 6))
        self.var_thr = tk.IntVar(value=80)
        self.scale_thr = ttk.Scale(opt, from_=50, to=95, orient="horizontal",
                                   length=130, command=self._on_thr_move)
        self.scale_thr.pack(side="left")
        self.lbl_thr = ttk.Label(opt, text="80%", width=5)
        self.lbl_thr.pack(side="left", padx=(6, 0))
        # 必须等 lbl_thr 建好再 set，否则 command 回调会引用到尚未创建的控件
        self.scale_thr.set(80)

        # ---------- 进度 ----------
        prog = ttk.Frame(self)
        prog.pack(fill="x", **pad)
        self.lbl_progress = ttk.Label(prog, text="", style="Muted.TLabel")
        self.lbl_progress.pack(anchor="w", pady=(0, 3))
        self.pbar = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.pbar.pack(fill="x")
        self.lbl_status = ttk.Label(prog, text="选择一个文件夹开始。建议先拿测试文件夹试一次。",
                                    style="Muted.TLabel")
        self.lbl_status.pack(anchor="w", pady=(4, 0))

        # ---------- 结果树 ----------
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, pady=(8, 6), **pad)

        cols = ("size", "mtime", "folder")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings",
                                 selectmode="extended")
        self.tree.heading("#0", text="文件")
        self.tree.heading("size", text="大小")
        self.tree.heading("mtime", text="修改时间")
        self.tree.heading("folder", text="所在文件夹")
        self.tree.column("#0", width=360, minwidth=220, stretch=True)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("mtime", width=140, anchor="center", stretch=False)
        self.tree.column("folder", width=340, stretch=True)

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self.tree.tag_configure("group_hash", foreground=C_HASH,
                                font=("Microsoft YaHei UI", 9, "bold"))
        self.tree.tag_configure("group_name", foreground=C_NAME,
                                font=("Microsoft YaHei UI", 9, "bold"))
        self.tree.tag_configure("keep", foreground="#15803d")
        self.tree.tag_configure("file", foreground=C_TEXT)

        # 右键菜单
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="在资源管理器中显示", command=self._ctx_reveal)
        self.menu.add_command(label="复制完整路径", command=self._ctx_copy)
        self.menu.add_separator()
        self.menu.add_command(label="勾选 / 取消勾选", command=self._ctx_toggle)

        # ---------- 底部操作栏 ----------
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 14), **pad)

        left = ttk.Frame(bar)
        left.pack(side="left")
        ttk.Button(left, text="全选建议项", width=11,
                   command=self.select_suggested).pack(side="left")
        ttk.Button(left, text="清空勾选", width=10,
                   command=self.clear_selection).pack(side="left", padx=(8, 0))
        ttk.Button(left, text="展开全部", width=10,
                   command=lambda: self._expand_all(True)).pack(side="left", padx=(8, 0))
        ttk.Button(left, text="折叠全部", width=10,
                   command=lambda: self._expand_all(False)).pack(side="left", padx=(8, 0))

        right = ttk.Frame(bar)
        right.pack(side="right")
        self.var_perm = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="永久删除", variable=self.var_perm,
                        style="Bg.TCheckbutton").pack(side="left", padx=(0, 14))
        self.btn_skip = ttk.Button(right, text="跳过", width=8,
                                   command=self.on_skip)
        self.btn_skip.pack(side="left")
        self.btn_delete = ttk.Button(right, text="删除选中", width=14,
                                     style="Accent.TButton", command=self.on_delete)
        self.btn_delete.pack(side="left", padx=(8, 0))

        self.lbl_summary = ttk.Label(self, text="", style="Muted.TLabel")
        self.lbl_summary.pack(anchor="w", padx=16, pady=(0, 10))
        self._update_summary()

    def _bind_events(self):
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        self.tree.bind("<Double-1>", self._on_tree_double, add="+")
        self.tree.bind("<Button-3>", self._on_tree_rclick, add="+")
        self.tree.bind("<space>", self._on_space, add="+")
        self.ent_folder.bind("<Return>", lambda e: self.on_scan())
        self.bind("<F5>", lambda e: self.on_scan())
        self.bind("<Escape>", lambda e: self._cancel_scan())

    # ------------------------------------------------------------ 小工具方法
    def _on_thr_move(self, val):
        v = int(round(float(val)))
        self.var_thr.set(v)
        if hasattr(self, "lbl_thr"):     # 构造期间可能尚未创建
            self.lbl_thr.configure(text="%d%%" % v)

    def _sync_threshold_state(self):
        state = "normal" if self.var_name.get() else "disabled"
        self.scale_thr.configure(state=state)

    def _set_status(self, text):
        self.lbl_status.configure(text=text)

    def _busy(self, busy):
        state = "disabled" if busy else "normal"
        for w in (self.btn_browse, self.ent_folder, self.btn_delete, self.btn_skip):
            w.configure(state=state)
        self.btn_scan.configure(text="停止扫描" if busy else "开始扫描")

    # -------------------------------------------------------------- 浏览按钮
    def on_browse(self):
        init = self.var_folder.get().strip() or self._last_dir or os.path.expanduser("~")
        if not os.path.isdir(init):
            init = os.path.expanduser("~")
        d = filedialog.askdirectory(title="选择要检查重复文件的文件夹",
                                    initialdir=init, mustexist=True)
        if d:
            d = os.path.normpath(d)
            self.var_folder.set(d)
            self._last_dir = d
            self._set_status("已选择：%s" % d)

    # -------------------------------------------------------------- 扫描流程
    def on_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            self._cancel_scan()
            return

        folder = self.var_folder.get().strip().strip('"')
        if not folder:
            messagebox.showinfo(APP_TITLE, "请先选择要检查的文件夹。")
            return
        if not os.path.isdir(folder):
            messagebox.showerror(APP_TITLE, "文件夹不存在或无法访问：\n" + folder)
            return
        if not self.var_hash.get() and not self.var_name.get():
            messagebox.showinfo(APP_TITLE, "请至少选择一种判重方式（按内容 / 按文件名）。")
            return

        self._clear_results()
        self.cancel_flag.clear()
        self._busy(True)
        self.pbar.configure(value=0, mode="indeterminate")
        self.pbar.start(12)
        self.lbl_progress.configure(text="准备中…")
        self._set_status("正在统计文件…")

        args = (folder, self.var_recursive.get(), self.var_hash.get(),
                self.var_name.get(), self.var_thr.get() / 100.0)
        self.scan_thread = threading.Thread(target=self._scan_worker, args=args,
                                            daemon=True)
        self.scan_thread.start()

    def _cancel_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            self.cancel_flag.set()
            self.lbl_progress.configure(text="正在停止…")
            self._set_status("正在停止…")

    def _scan_worker(self, folder, recursive, use_hash, use_name, thr):
        q = self.msg_queue
        last = [0.0]

        def progress(stage, done, total, cur):
            now = time.time()
            if stage in ("done",) or now - last[0] > 0.08:
                last[0] = now
                q.put(("progress", stage, done, total, cur))

        try:
            t0 = time.time()
            res = scan_folder(folder, recursive=recursive, check_hash=use_hash,
                              check_name=use_name, threshold=thr,
                              progress=progress, cancel=self.cancel_flag.is_set)
            res["elapsed"] = time.time() - t0
            q.put(("done", res))
        except ScanCancelled:
            q.put(("cancelled", None))
        except Exception as e:
            q.put(("error", str(e)))

    def _pump_queue(self):
        """主线程轮询后台消息，保证 UI 不卡死。"""
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, stage, done, total, cur = msg
                    self._on_progress(stage, done, total, cur)
                elif kind == "done":
                    self._on_scan_done(msg[1])
                elif kind == "cancelled":
                    self.pbar.stop()
                    self.pbar.configure(mode="determinate", value=0)
                    self._busy(False)
                    self.lbl_progress.configure(text="已停止")
                    self._set_status("已停止扫描。")
                elif kind == "error":
                    self.pbar.stop()
                    self.pbar.configure(mode="determinate", value=0)
                    self._busy(False)
                    self.lbl_progress.configure(text="扫描出错")
                    self._set_status("扫描出错。")
                    messagebox.showerror(APP_TITLE, msg[1])
        except queue.Empty:
            pass
        self.after(80, self._pump_queue)

    def _on_progress(self, stage, done, total, cur):
        if stage == "collect":
            # 收集阶段使用不确定进度条动画 + 实时文件计数，
            # 让用户感知到扫描正在进行。
            if self.pbar["mode"] != "indeterminate":
                self.pbar.configure(mode="indeterminate")
                self.pbar.start(12)
            self.lbl_progress.configure(text="收集文件：已发现 %d 个" % done)
            self._set_status("正在收集文件…已发现 %d 个" % done)
        elif stage == "hash":
            if self.pbar["mode"] != "determinate":
                self.pbar.stop()
                self.pbar.configure(mode="determinate")
            if total:
                self.pbar.configure(maximum=total, value=done)
                pct = int(round(100.0 * done / total)) if total else 0
                self.lbl_progress.configure(
                    text="比对内容：%d / %d  (%d%%)" % (done, total, pct))
                self._set_status("正在比对内容 %d / %d  ·  %s"
                                 % (done, total, os.path.basename(cur)))
            else:
                self.pbar.configure(maximum=100, value=0)
                self.lbl_progress.configure(text="比对内容：0 个候选文件")
                self._set_status("正在比对内容…")
        elif stage == "name":
            if self.pbar["mode"] != "determinate":
                self.pbar.stop()
                self.pbar.configure(mode="determinate")
            self.pbar.configure(maximum=100, value=0)
            self.lbl_progress.configure(text="比对文件名…")
            self._set_status("正在比对文件名…")

    def _on_scan_done(self, res):
        self.pbar.stop()
        self.pbar.configure(mode="determinate", maximum=100, value=100)
        self._busy(False)
        self.groups = res["groups"]
        self._render_groups()
        n_files = sum(len(g["files"]) for g in self.groups)
        waste = sum(g.get("waste", 0) for g in self.groups)
        if not self.groups:
            self.lbl_progress.configure(text="扫描完成")
            self._set_status("扫描完成：共检查 %d 个文件，没有发现重复。用时 %.1f 秒"
                             % (res["total_files"], res.get("elapsed", 0)))
            messagebox.showinfo(APP_TITLE, "没有发现重复文件。\n\n共检查 %d 个文件。"
                                % res["total_files"])
        else:
            self.lbl_progress.configure(text="扫描完成：发现 %d 组重复" % len(self.groups))
            self._set_status(
                "扫描完成：%d 组重复，涉及 %d 个文件，最多可释放 %s。用时 %.1f 秒"
                % (len(self.groups), n_files, human_size(waste), res.get("elapsed", 0)))
        self._update_summary()

    # -------------------------------------------------------------- 结果渲染
    def _clear_results(self):
        self.tree.delete(*self.tree.get_children())
        self.groups = []
        self.checked.clear()
        self.item_path.clear()
        self.group_items = []
        self._update_summary()

    def _render_groups(self):
        self.tree.delete(*self.tree.get_children())
        self.item_path.clear()
        self.checked.clear()
        self.group_items = []

        for gi, g in enumerate(self.groups, 1):
            tag = "group_hash" if g["reason"] == "hash" else "group_name"
            label = "第 %d 组 · %s · %d 个文件" % (gi, g["detail"], len(g["files"]))
            gid = self.tree.insert("", "end", text=label, open=True,
                                   values=("", "", ""), tags=(tag,))
            self.group_items.append(gid)

            for fi, f in enumerate(g["files"]):
                keep = (fi == 0)     # 默认保留每组第一个（最早修改的）
                mark = UNCHECKED if keep else CHECKED
                suffix = "   ← 建议保留" if keep else ""
                iid = self.tree.insert(
                    gid, "end",
                    text="%s %s%s" % (mark, f["name"], suffix),
                    values=(human_size(f["size"]),
                            time.strftime("%Y-%m-%d %H:%M",
                                          time.localtime(f["mtime"])),
                            os.path.dirname(f["path"])),
                    tags=("keep" if keep else "file",))
                self.item_path[iid] = f["path"]
                if not keep:
                    self.checked.add(f["path"])
        self._refresh_marks()

    def _refresh_marks(self):
        """根据 self.checked 刷新所有行的勾选符号。"""
        for iid, path in self.item_path.items():
            txt = self.tree.item(iid, "text")
            body = txt[2:] if txt[:1] in (CHECKED, UNCHECKED) else txt
            mark = CHECKED if path in self.checked else UNCHECKED
            self.tree.item(iid, text="%s %s" % (mark, body.lstrip()))
        self._update_summary()

    def _update_summary(self):
        n = len(self.checked)
        size = 0
        for g in self.groups:
            for f in g["files"]:
                if f["path"] in self.checked:
                    size += f["size"]
        if n:
            self.lbl_summary.configure(
                text="已勾选 %d 个文件，删除后可释放约 %s。点「删除选中」执行，或点「跳过」全部保留。"
                     % (n, human_size(size)))
        else:
            self.lbl_summary.configure(
                text="未勾选任何文件。单击文件行前的方框可勾选／取消，双击可在资源管理器中定位。")

    # -------------------------------------------------------------- 交互事件
    def _on_tree_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid in self.item_path:
            # 点在名称列才切换勾选，避免误触
            if self.tree.identify_column(event.x) == "#0":
                self._toggle(iid)
                return "break"
        elif iid in self.group_items:
            if self.tree.identify_column(event.x) == "#0":
                region = self.tree.identify_region(event.x, event.y)
                if region == "tree":     # 点在展开箭头上，交给默认行为
                    return

    def _toggle(self, iid):
        p = self.item_path.get(iid)
        if not p:
            return
        if p in self.checked:
            self.checked.discard(p)
        else:
            self.checked.add(p)
        self._refresh_marks()

    def _on_space(self, event):
        for iid in self.tree.selection():
            if iid in self.item_path:
                p = self.item_path[iid]
                if p in self.checked:
                    self.checked.discard(p)
                else:
                    self.checked.add(p)
        self._refresh_marks()
        return "break"

    def _on_tree_double(self, event):
        iid = self.tree.identify_row(event.y)
        if iid in self.item_path:
            reveal_in_explorer(self.item_path[iid])
            return "break"

    def _on_tree_rclick(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            try:
                self.menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.menu.grab_release()

    def _ctx_reveal(self):
        sel = self.tree.selection()
        if sel and sel[0] in self.item_path:
            reveal_in_explorer(self.item_path[sel[0]])

    def _ctx_copy(self):
        sel = self.tree.selection()
        if sel and sel[0] in self.item_path:
            self.clipboard_clear()
            self.clipboard_append(self.item_path[sel[0]])
            self._set_status("已复制路径到剪贴板。")

    def _ctx_toggle(self):
        for iid in self.tree.selection():
            if iid in self.item_path:
                self._toggle(iid)

    def _expand_all(self, opened):
        for gid in self.group_items:
            self.tree.item(gid, open=opened)

    def select_suggested(self):
        """每组保留第一个，其余全部勾选。"""
        self.checked.clear()
        for g in self.groups:
            for f in g["files"][1:]:
                self.checked.add(f["path"])
        self._refresh_marks()
        self._set_status("已按「每组保留第一个」勾选。")

    def clear_selection(self):
        self.checked.clear()
        self._refresh_marks()
        self._set_status("已清空勾选，不会删除任何文件。")

    # -------------------------------------------------------------- 删除/跳过
    def on_skip(self):
        if not self.groups:
            return
        self.clear_selection()
        self._set_status("已跳过：所有文件保留不动。")

    def on_delete(self):
        paths = sorted(self.checked)
        if not paths:
            messagebox.showinfo(APP_TITLE, "还没有勾选任何文件。\n\n"
                                           "单击文件名前的方框即可勾选，"
                                           "或点「全选建议项」自动勾选。")
            return

        # 安全检查：不允许把某一组全部删光
        risky = []
        for gi, g in enumerate(self.groups, 1):
            all_paths = [f["path"] for f in g["files"]]
            if all(p in self.checked for p in all_paths):
                risky.append(gi)

        total_size = 0
        for g in self.groups:
            for f in g["files"]:
                if f["path"] in self.checked:
                    total_size += f["size"]

        perm = self.var_perm.get()
        head = "永久删除" if perm else "移入回收站"
        lines = [
            "即将%s %d 个文件，释放约 %s。" % (head, len(paths), human_size(total_size)),
            "",
        ]
        if perm:
            lines += ["⚠ 永久删除无法恢复！建议改用回收站。", ""]
        else:
            lines += ["文件会进入回收站，随时可以还原。", ""]
        if risky:
            lines += ["⚠ 注意：第 %s 组的文件被全部勾选了，"
                      "这会导致该组一个副本都不剩。" % "、".join(map(str, risky)), ""]
        preview = paths[:8]
        lines += ["将删除："] + ["    " + os.path.basename(p) for p in preview]
        if len(paths) > len(preview):
            lines.append("    …… 还有 %d 个" % (len(paths) - len(preview)))
        lines += ["", "确定继续吗？"]

        ok = messagebox.askyesno(APP_TITLE + " - 请确认", "\n".join(lines),
                                 icon="warning" if (perm or risky) else "question",
                                 default="no" if (perm or risky) else "yes")
        if not ok:
            self._set_status("已取消删除，文件未改动。")
            return

        if perm:
            again = messagebox.askyesno(
                APP_TITLE + " - 二次确认",
                "这是永久删除，%d 个文件将无法恢复。\n\n真的要继续吗？" % len(paths),
                icon="warning", default="no")
            if not again:
                self._set_status("已取消删除，文件未改动。")
                return

        self._set_status("正在删除…")
        self.update_idletasks()
        res = delete_files(paths, to_trash=not perm)
        ok_n, fail = len(res["deleted"]), res["failed"]

        # 从结果里移除已删掉的文件
        done = set(res["deleted"])
        for g in self.groups:
            g["files"] = [f for f in g["files"] if f["path"] not in done]
        self.groups = [g for g in self.groups if len(g["files"]) > 1]
        self.checked -= done
        self._render_groups()

        if fail:
            detail = "\n".join("· %s —— %s" % (os.path.basename(k), v)
                               for k, v in list(fail.items())[:10])
            messagebox.showwarning(
                APP_TITLE,
                "成功删除 %d 个，%d 个失败。\n\n%s" % (ok_n, len(fail), detail))
        else:
            messagebox.showinfo(
                APP_TITLE,
                "已成功%s %d 个文件。" % ("永久删除" if perm else "移入回收站", ok_n))
        self._set_status("完成：%s %d 个文件，失败 %d 个。剩余 %d 组重复。"
                         % (head, ok_n, len(fail), len(self.groups)))

    def _on_close(self):
        if self.scan_thread and self.scan_thread.is_alive():
            if not messagebox.askyesno(APP_TITLE, "扫描还在进行中，确定要退出吗？"):
                return
            self.cancel_flag.set()
        self.destroy()


def main():
    # 任务栏图标必须在窗口创建前指定
    set_app_user_model_id()
    enable_dpi_awareness()
    app = DupCheckerApp()
    # 居中显示
    app.update_idletasks()
    w, h = app.winfo_width(), app.winfo_height()
    x = (app.winfo_screenwidth() - w) // 2
    y = (app.winfo_screenheight() - h) // 3
    app.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
    app.mainloop()


if __name__ == "__main__":
    main()
