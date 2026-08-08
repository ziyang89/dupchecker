#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重复文件检查器 - 核心逻辑（无界面）
==================================
职责：扫描文件夹、判定重复、删除文件。与界面完全解耦，便于测试。

判定重复的两种方式（满足任一即视为重复）:
  1. 哈希相同   : 文件内容完全一致（SHA-256）。
  2. 文件名相似 : 归一化后相同（如 photo.jpg 与 photo(1).jpg），
                 或模糊相似度达到阈值（如 report.pdf 与 report_v2.pdf）。

性能优化：只对「大小相同」的文件计算哈希——大小不同的文件内容必然不同，
无需读盘。对大文件夹可节省绝大部分 IO。
"""

import difflib
import hashlib
import os
import re

__all__ = [
    "compute_hash", "normalize_name", "human_size", "scan_folder",
    "recycle_one", "delete_files", "ScanCancelled",
]


class ScanCancelled(Exception):
    """扫描被用户取消。"""
    pass


def human_size(n):
    """把字节数转成易读字符串。"""
    if n is None or n < 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    if i == 0:
        return "%d B" % n
    return "%.1f %s" % (f, units[i])


def compute_hash(path, algo="sha256", chunk=1024 * 1024):
    """分块计算文件哈希，避免大文件占满内存。"""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


_RE_NUM_SUFFIX = re.compile(r"[\(\（\[【]\s*\d+\s*[\)\）\]】]")
_RE_COPY_WORD = re.compile(
    r"(\s*-\s*副本|\s*-\s*copy|副本|复制|拷贝|_copy|\bcopy\b|"
    r"\bfinal\b|最终|\bnew\b|[_\-\s]v\d+$|\bv\d+\b|_\d+$|-\d+$)",
    re.IGNORECASE,
)
_RE_SEP = re.compile(r"[\s\-_\.]+")


def normalize_name(filename):
    """把文件名归一化，去掉副本标记/空格/连字符，用于「文件名相似」判定。"""
    name = os.path.splitext(filename)[0].lower()
    name = _RE_NUM_SUFFIX.sub("", name)
    name = _RE_COPY_WORD.sub("", name)
    name = _RE_SEP.sub("", name)
    return name


def _cluster_by_name(records, threshold):
    """用并查集把「文件名相同/相似」的记录聚成簇。返回簇列表（每簇是下标列表）。"""
    parent = list(range(len(records)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # 1) 归一化完全相同的直接归为一簇
    by_norm = {}
    for i, r in enumerate(records):
        if r["norm"]:
            by_norm.setdefault(r["norm"], []).append(i)
    for idxs in by_norm.values():
        for k in idxs[1:]:
            union(idxs[0], k)

    # 2) 同目录 + 同扩展名内做模糊匹配（限制规模，避免全量 O(n^2) 爆炸）
    by_de = {}
    for i, r in enumerate(records):
        key = (os.path.dirname(r["path"]), os.path.splitext(r["name"])[1].lower())
        by_de.setdefault(key, []).append(i)

    for idxs in by_de.values():
        n = len(idxs)
        if n < 2 or n > 400:      # 超大目录跳过模糊匹配，避免卡死
            continue
        for i in range(n):
            a = records[idxs[i]]["norm"]
            if not a:
                continue
            for j in range(i + 1, n):
                b = records[idxs[j]]["norm"]
                if not b or a == b:
                    continue
                # 剪枝：SequenceMatcher.ratio() 的数学上界是 2*min/(la+lb)，
                # 上界都够不到阈值就不必做昂贵的相似度计算。
                la, lb = len(a), len(b)
                if 2.0 * min(la, lb) / (la + lb) < threshold:
                    continue
                if difflib.SequenceMatcher(None, a, b).ratio() >= threshold:
                    union(idxs[i], idxs[j])

    clusters = {}
    for i in range(len(records)):
        clusters.setdefault(find(i), []).append(i)
    return [sorted(c) for c in clusters.values() if len(c) > 1]


def scan_folder(folder, recursive=False, check_hash=True, check_name=True,
                threshold=0.8, min_size=0, progress=None, cancel=None):
    """
    扫描文件夹并返回重复分组。

    参数:
        folder     : 目标文件夹
        recursive  : 是否递归子文件夹
        check_hash : 是否按内容（哈希）判重
        check_name : 是否按文件名相似判重
        threshold  : 文件名相似度阈值 0~1
        min_size   : 忽略小于该字节数的文件
        progress   : 回调 progress(stage, done, total, current_path)
        cancel     : 可调用对象，返回 True 表示请求取消

    返回: {"groups": [...], "total_files": n, "scanned": n}
        每个 group: {"reason": "hash"/"name", "detail": str, "files": [rec...]}
        每个 rec  : {"path","name","size","mtime","hash","norm"}
    """
    def _tick(stage, done, total, cur=""):
        if progress:
            progress(stage, done, total, cur)

    def _check_cancel():
        if cancel and cancel():
            raise ScanCancelled()

    if not os.path.isdir(folder):
        raise ValueError("文件夹不存在或无法访问：\n" + str(folder))

    # ---- 阶段 1: 收集文件 ----
    _tick("collect", 0, 0, folder)
    paths = []
    if recursive:
        for root, _dirs, fs in os.walk(folder):
            _check_cancel()
            for fn in fs:
                paths.append(os.path.join(root, fn))
                _tick("collect", len(paths), 0, os.path.join(root, fn))
    else:
        try:
            for fn in os.listdir(folder):
                _check_cancel()
                p = os.path.join(folder, fn)
                if os.path.isfile(p):
                    paths.append(p)
                    _tick("collect", len(paths), 0, p)
        except Exception as e:
            raise ValueError("无法读取文件夹内容：\n%s" % e)

    records = []
    for p in paths:
        _check_cancel()
        try:
            st = os.stat(p)
            size, mtime = st.st_size, st.st_mtime
        except Exception:
            continue
        if size < min_size:
            continue
        records.append({
            "path": p,
            "name": os.path.basename(p),
            "size": size,
            "mtime": mtime,
            "hash": None,
            "norm": normalize_name(os.path.basename(p)),
        })

    total = len(records)
    groups = []

    # ---- 阶段 2: 内容判重（仅对大小相同的文件计算哈希）----
    if check_hash and total > 1:
        by_size = {}
        for r in records:
            by_size.setdefault(r["size"], []).append(r)
        candidates = [r for rs in by_size.values() if len(rs) > 1 for r in rs]

        need = len(candidates)
        _tick("hash", 0, need, "")
        for i, r in enumerate(candidates):
            _check_cancel()
            try:
                r["hash"] = compute_hash(r["path"])
            except Exception:
                r["hash"] = None
            _tick("hash", i + 1, need, r["path"])

        by_hash = {}
        for r in candidates:
            if r["hash"]:
                by_hash.setdefault(r["hash"], []).append(r)
        for _h, rs in by_hash.items():
            if len(rs) > 1:
                rs = sorted(rs, key=lambda x: (x["mtime"], len(x["path"])))
                waste = rs[0]["size"] * (len(rs) - 1)
                groups.append({
                    "reason": "hash",
                    "detail": "内容完全相同（SHA-256 一致）· 可释放 %s" % human_size(waste),
                    "files": rs,
                    "waste": waste,
                })

    # ---- 阶段 3: 文件名判重 ----
    if check_name and total > 1:
        _tick("name", 0, total, "")
        _check_cancel()
        clusters = _cluster_by_name(records, threshold)
        # 已被哈希组完全覆盖的簇不再重复列出
        hash_sets = [set(id(f) for f in g["files"]) for g in groups]
        for c in clusters:
            _check_cancel()
            files = [records[i] for i in c]
            ids = set(id(f) for f in files)
            if any(ids <= hs for hs in hash_sets):
                continue
            files = sorted(files, key=lambda x: (x["mtime"], len(x["path"])))
            waste = sum(f["size"] for f in files[1:])
            groups.append({
                "reason": "name",
                "detail": "文件名高度相似（≥ %.0f%%）· 内容未必相同" % (threshold * 100),
                "files": files,
                "waste": waste,
            })
        _tick("name", total, total, "")

    # 哈希组排在前面（更可靠），组内按可释放空间降序
    groups.sort(key=lambda g: (0 if g["reason"] == "hash" else 1, -g.get("waste", 0)))
    _tick("done", total, total, "")
    return {"groups": groups, "total_files": total, "scanned": total}


# ----------------------------------------------------------------------------
# 删除（送回收站 / 永久删除）
# ----------------------------------------------------------------------------
def recycle_one(path):
    """送 Windows 回收站，成功返回 True。"""
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040
        FOF_NOERRORUI = 0x0400

        op = SHFILEOPSTRUCTW(
            None, FO_DELETE, os.path.abspath(path) + "\0\0", None,
            FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT,
            False, None, None,
        )
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return res == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


def delete_files(paths, to_trash=True, progress=None):
    """
    批量删除。to_trash=True 送回收站（可恢复），False 为永久删除。
    返回 {"deleted": [...], "failed": {path: reason}}
    """
    deleted, failed = [], {}
    total = len(paths)
    for i, p in enumerate(paths):
        if progress:
            progress(i + 1, total, p)
        if not os.path.exists(p):
            failed[p] = "文件已不存在"
            continue
        if to_trash:
            if recycle_one(p):
                deleted.append(p)
            else:
                failed[p] = "无法送入回收站（权限不足或文件被占用）"
        else:
            try:
                os.remove(p)
                deleted.append(p)
            except Exception as e:
                failed[p] = str(e)
    return {"deleted": deleted, "failed": failed}
