#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重复文件检查器 (Duplicate File Checker)
======================================
纯标准库实现的本地工具：后端用 Python http.server 扫描文件夹、计算哈希、
判定重复并删除/送回收站；前端用浏览器打开的网页操作。
无需安装任何第三方库。

判定重复的两种方式（满足任一即视为重复）:
  1. 哈希相同   : 文件内容完全一致（SHA-256），几乎肯定是同一文件。
  2. 文件名相似 : 归一化后相同（如 photo.jpg 与 photo(1).jpg），
                 或模糊相似度达到阈值（如 report.pdf 与 report_v2.pdf）。

运行:
    python dupchecker.py            # 启动后自动打开浏览器 http://127.0.0.1:8765
    python dupchecker.py --port 9000 --no-browser
"""

import http.server
import json
import os
import re
import sys
import threading
import time
import webbrowser
import hashlib
import difflib
from urllib.parse import urlparse, parse_qs

HOST = "127.0.0.1"
PORT = 8765

# 扫描任务状态表: scan_id -> dict
scans = {}
_scan_counter = 0
_scan_lock = threading.Lock()

# 当前运行的 http 服务实例（用于“退出程序”）
httpd_instance = None


# ----------------------------------------------------------------------------
# 核心逻辑
# ----------------------------------------------------------------------------
def compute_hash(path, algo="sha256", chunk=1024 * 1024):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def normalize_name(filename):
    """把文件名归一化，去掉副本标记/空格/连字符，用于“文件名相似”判定。"""
    name = os.path.splitext(filename)[0].lower()
    # 去掉 (1) (2) （3）等编号
    name = re.sub(r"[\(\（]\s*\d+\s*[\)\）]", "", name)
    # 去掉常见副本词
    name = re.sub(r"( copy|副本|复制|拷贝| - 副本|_copy|final|最终)", "", name)
    # 去掉空格与连字符/下划线
    name = re.sub(r"[\s\-_]+", "", name)
    return name


def union_find_cluster(records, check_name, threshold):
    """用并查集把“文件名相同/相似”的记录聚成簇。返回簇列表（每簇是记录下标列表）。"""
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

    if check_name:
        # 1) 归一化完全相同的归为一簇
        by_norm = {}
        for i, r in enumerate(records):
            by_norm.setdefault(r["norm"], []).append(i)
        for idxs in by_norm.values():
            if len(idxs) > 1:
                for k in idxs[1:]:
                    union(idxs[0], k)
        # 2) 同目录、同扩展名内做模糊匹配（限制规模，避免全量 O(n^2)）
        by_de = {}
        for i, r in enumerate(records):
            d = os.path.dirname(r["path"])
            ext = os.path.splitext(r["name"])[1].lower()
            by_de.setdefault((d, ext), []).append(i)
        for idxs in by_de.values():
            n = len(idxs)
            for i in range(n):
                for j in range(i + 1, n):
                    a = records[idxs[i]]["norm"]
                    b = records[idxs[j]]["norm"]
                    if a == b:
                        continue
                    if difflib.SequenceMatcher(None, a, b).ratio() >= threshold:
                        union(idxs[i], idxs[j])

    clusters = {}
    for i in range(len(records)):
        clusters.setdefault(find(i), []).append(i)
    return [c for c in clusters.values() if len(c) > 1]


def scan_folder(folder, recursive, check_name, threshold, scan_id):
    try:
        if not os.path.isdir(folder):
            scans[scan_id]["status"] = "error"
            scans[scan_id]["error"] = "文件夹不存在或无法访问: " + folder
            return

        # 收集文件列表
        files = []
        if recursive:
            for root, _dirs, fs in os.walk(folder):
                for fn in fs:
                    files.append(os.path.join(root, fn))
        else:
            for fn in os.listdir(folder):
                p = os.path.join(folder, fn)
                if os.path.isfile(p):
                    files.append(p)

        total = len(files)
        scans[scan_id]["total"] = total
        scans[scan_id]["status"] = "scanning"

        records = []
        for i, p in enumerate(files):
            try:
                size = os.path.getsize(p)
                h = compute_hash(p)
            except Exception:
                size = -1
                h = None
            records.append({
                "path": p,
                "size": size,
                "hash": h,
                "norm": normalize_name(os.path.basename(p)),
                "name": os.path.basename(p),
            })
            scans[scan_id]["scanned"] = i + 1
            scans[scan_id]["current"] = p

        # 哈希重复分组
        hash_groups = []
        by_hash = {}
        for r in records:
            if r["hash"]:
                by_hash.setdefault(r["hash"], []).append(r)
        for h, rs in by_hash.items():
            if len(rs) > 1:
                hash_groups.append({
                    "reason": "hash",
                    "detail": "哈希值相同（几乎肯定是同一文件）",
                    "files": rs,
                })

        # 文件名重复分组（并查集）
        name_groups = []
        if check_name:
            clusters = union_find_cluster(records, check_name, threshold)
            for c in clusters:
                name_groups.append({
                    "reason": "name",
                    "detail": "文件名归一化/相似度 ≥ %.0f%%" % (threshold * 100),
                    "files": [records[i] for i in c],
                })

        scans[scan_id]["hash_groups"] = hash_groups
        scans[scan_id]["name_groups"] = name_groups
        scans[scan_id]["status"] = "done"
    except Exception as e:
        scans[scan_id]["status"] = "error"
        scans[scan_id]["error"] = str(e)


# ----------------------------------------------------------------------------
# 删除（送回收站 / 永久删除）
# ----------------------------------------------------------------------------
def recycle_one(path):
    """送 Windows 回收站，成功返回 True。"""
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCT(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", wintypes.UINT),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x40
        FOF_NOCONFIRMATION = 0x10
        FOF_NOERRORUI = 0x400
        pFrom = path + "\0\0"
        op = SHFILEOPSTRUCT(0, FO_DELETE, pFrom, None,
                            FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI,
                            False, None, None)
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return res == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


def do_delete(paths, to_trash):
    deleted = []
    failed = {}
    for p in paths:
        if to_trash:
            if recycle_one(p):
                deleted.append(p)
            else:
                failed[p] = "无法送入回收站（可能已被移动/删除或权限不足）"
        else:
            try:
                os.remove(p)
                deleted.append(p)
            except Exception as e:
                failed[p] = str(e)
    return {"deleted": deleted, "failed": failed}


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>重复文件检查器</title>
<style>
  :root{
    --bg:#f5f7fa; --panel:#ffffff; --border:#e3e8ef; --text:#1f2937;
    --muted:#6b7280; --primary:#2563eb; --primary-d:#1d4ed8;
    --danger:#dc2626; --ok:#16a34a; --warn:#d97706; --chip:#eef2ff;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:"Microsoft YaHei UI","Segoe UI",system-ui,sans-serif;
       background:var(--bg);color:var(--text);font-size:14px}
  header{background:var(--primary);color:#fff;padding:14px 20px;font-size:18px;font-weight:600}
  .wrap{max-width:1000px;margin:0 auto;padding:18px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px}
  label{display:block;font-weight:600;margin-bottom:6px}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  input[type=text]{flex:1;min-width:260px;padding:9px 11px;border:1px solid var(--border);border-radius:8px;font-size:14px}
  button{cursor:pointer;border:none;border-radius:8px;padding:9px 16px;font-size:14px;font-weight:600;background:var(--primary);color:#fff}
  button:hover{background:var(--primary-d)}
  button.ghost{background:#fff;color:var(--primary);border:1px solid var(--primary)}
  button.danger{background:var(--danger)}
  button.danger:hover{background:#b91c1c}
  button:disabled{opacity:.5;cursor:not-allowed}
  .opts{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px;align-items:center}
  .opts label{font-weight:400;margin:0;display:flex;gap:6px;align-items:center}
  .opts input[type=range]{width:140px}
  .progress{height:10px;background:#e5e7eb;border-radius:6px;overflow:hidden;margin-top:12px;display:none}
  .progress > div{height:100%;width:0;background:var(--primary);transition:width .2s}
  .status{margin-top:8px;color:var(--muted);font-size:13px;min-height:18px}
  .sec-title{font-size:16px;font-weight:700;margin:6px 0 10px;display:flex;align-items:center;gap:8px}
  .badge{background:var(--chip);color:var(--primary);border-radius:999px;padding:2px 10px;font-size:12px}
  .group{border:1px solid var(--border);border-radius:10px;margin-bottom:12px;overflow:hidden}
  .group-head{background:#f8fafc;padding:10px 12px;display:flex;justify-content:space-between;align-items:center;gap:10px;border-bottom:1px solid var(--border)}
  .group-head .reason{font-weight:600}
  .group-head .detail{color:var(--muted);font-size:12px}
  .file{display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid #f1f5f9}
  .file:last-child{border-bottom:none}
  .file input{width:16px;height:16px}
  .file .p{flex:1;word-break:break-all;font-family:Consolas,Menlo,monospace;font-size:13px}
  .file .s{color:var(--muted);font-size:12px;white-space:nowrap}
  .keep{color:var(--ok);font-size:12px;font-weight:600}
  .group-actions{display:flex;gap:8px;padding:8px 12px;background:#fafafa}
  .bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
  .empty{color:var(--muted);padding:20px;text-align:center}
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;padding:20px;z-index:50}
  .modal .box{background:#fff;border-radius:12px;max-width:560px;width:100%;padding:20px;max-height:80vh;overflow:auto}
  .modal h3{margin:0 0 10px}
  .modal ul{margin:8px 0;padding-left:18px;max-height:240px;overflow:auto;color:var(--muted);font-size:13px}
  .modal .acts{display:flex;gap:10px;justify-content:flex-end;margin-top:14px}
  .toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#111827;color:#fff;padding:10px 18px;border-radius:8px;opacity:0;transition:opacity .25s;z-index:60;font-size:13px}
  .toast.show{opacity:.95}
  .hint{color:var(--muted);font-size:12px;margin-top:6px}
</style>
</head>
<body>
<header>重复文件检查器</header>
<div class="wrap">
  <div class="card">
    <label>文件夹路径</label>
    <div class="row">
      <input type="text" id="folder" placeholder="点右侧「浏览」选择文件夹，或直接粘贴路径">
      <button class="ghost" id="browseBtn">浏览…</button>
      <button id="scanBtn">开始扫描</button>
    </div>
    <div class="hint">点「浏览…」会弹出系统文件夹选择窗口。建议先在一个测试文件夹里试用。</div>
    <div class="opts">
      <label><input type="checkbox" id="recursive" checked> 包含子文件夹</label>
      <label><input type="checkbox" id="checkName" checked> 检测文件名相似</label>
      <label>相似度阈值 <span id="thrVal">80%</span>
        <input type="range" id="threshold" min="50" max="95" value="80">
      </label>
    </div>
    <div class="progress" id="prog"><div></div></div>
    <div class="status" id="status"></div>
  </div>

  <div class="bar" id="globalBar" style="display:none">
    <button class="ghost" id="selAll">全选可删</button>
    <button class="ghost" id="selNone">全部取消</button>
    <button class="danger" id="delBtn">删除选中</button>
    <button class="ghost" id="skipBtn">跳过（保留全部）</button>
    <span class="status" id="selCount" style="margin:0"></span>
  </div>

  <div id="results"></div>
  <div style="text-align:center;margin-top:8px">
    <button class="ghost" id="exitBtn">退出程序</button>
    <div class="hint">点此可干净关闭程序；任务栏出现的小黑窗（若有）也可直接关闭。</div>
  </div>
</div>

<div class="modal" id="modal">
  <div class="box">
    <h3 id="modalTitle">确认删除</h3>
    <p id="modalText"></p>
    <ul id="modalList"></ul>
    <div class="acts">
      <button class="ghost" id="mCancel">取消</button>
      <button class="danger" id="mPermanent">永久删除</button>
      <button id="mTrash">移入回收站</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let currentDeletePaths = [];

function fmtSize(b){
  if(b<0) return "?";
  if(b<1024) return b+" B";
  if(b<1024*1024) return (b/1024).toFixed(1)+" KB";
  if(b<1024*1024*1024) return (b/1024/1024).toFixed(1)+" MB";
  return (b/1024/1024/1024).toFixed(2)+" GB";
}
function toast(msg){
  const t=$("toast"); t.textContent=msg; t.classList.add("show");
  setTimeout(()=>t.classList.remove("show"),2600);
}
function setStatus(msg){ $("status").textContent=msg; }

$("threshold").addEventListener("input", e=>$("thrVal").textContent=e.target.value+"%");

// 浏览：调用后端弹出 Windows 原生文件夹选择框
let picking=false;
$("browseBtn").onclick=function(){
  if(picking) return;
  picking=true;
  const btn=$("browseBtn"), old=btn.textContent;
  btn.textContent="选择中…"; btn.disabled=true;
  setStatus("系统选择窗口已打开，请在窗口中选择文件夹（若没看到，请检查是否被其他窗口挡住或查看任务栏）。");
  fetch("/api/pick-folder",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({initial:$("folder").value.trim()})})
    .then(r=>r.json())
    .then(d=>{
      if(d.path){ $("folder").value=d.path; setStatus("已选择："+d.path); toast("已选择文件夹"); }
      else if(d.cancelled){ setStatus("已取消选择。"); }
      else { setStatus(""); toast(d.error||"打开选择窗口失败，请手动粘贴路径"); }
    })
    .catch(()=>{ setStatus(""); toast("打开选择窗口失败，请手动粘贴路径"); })
    .finally(()=>{ picking=false; btn.textContent=old; btn.disabled=false; });
};

// 回车即开始扫描
$("folder").addEventListener("keydown", e=>{ if(e.key==="Enter") startScan(); });

function startScan(){
  const folder=$("folder").value.trim();
  if(!folder){ toast("请先填写文件夹路径"); return; }
  const payload={
    folder,
    recursive:$("recursive").checked,
    check_name:$("checkName").checked,
    threshold:parseInt($("threshold").value,10)/100
  };
  $("scanBtn").disabled=true;
  $("results").innerHTML="";
  $("globalBar").style.display="none";
  $("prog").style.display="block";
  $("prog").firstElementChild.style.width="0%";
  setStatus("正在准备扫描…");
  fetch("/api/scan",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)})
    .then(r=>r.json())
    .then(d=>{
      const es=new EventSource("/api/stream?scan_id="+encodeURIComponent(d.scan_id));
      es.onmessage=ev=>{
        const s=JSON.parse(ev.data);
        if(s.status==="scanning"){
          const pct = s.total? Math.round(s.scanned/s.total*100):0;
          $("prog").firstElementChild.style.width=pct+"%";
          setStatus("已扫描 "+s.scanned+" / "+s.total+" 个文件…");
        } else if(s.status==="done"){
          es.close();
          $("prog").style.display="none";
          $("scanBtn").disabled=false;
          renderResults(s.hash_groups||[], s.name_groups||[]);
          const totalDup=(s.hash_groups||[]).length+(s.name_groups||[]).length;
          setStatus(totalDup? "发现 "+totalDup+" 组重复文件。" : "未发现重复文件。");
        } else if(s.status==="error"){
          es.close();
          $("prog").style.display="none";
          $("scanBtn").disabled=false;
          setStatus("错误："+ (s.error||"未知错误"));
        }
      };
      es.onerror=()=>{ es.close(); $("scanBtn").disabled=false;
        setStatus("连接中断，请重试。"); };
    })
    .catch(e=>{ $("scanBtn").disabled=false; setStatus("请求失败："+e); });
}

function renderResults(hashGroups, nameGroups){
  const root=$("results"); root.innerHTML="";
  if(!hashGroups.length && !nameGroups.length){
    const d=document.createElement("div"); d.className="empty";
    d.textContent="未找到重复文件 🎉"; root.appendChild(d); return;
  }
  if(hashGroups.length){
    root.appendChild(sectionTitle("内容完全相同（哈希重复）", hashGroups.length, "var(--danger)"));
    hashGroups.forEach((g,i)=>root.appendChild(groupCard(g,i,"hash")));
  }
  if(nameGroups.length){
    root.appendChild(sectionTitle("文件名相似（可能为重复）", nameGroups.length, "var(--warn)"));
    nameGroups.forEach((g,i)=>root.appendChild(groupCard(g,i,"name")));
  }
  $("globalBar").style.display="flex";
  updateSelCount();
}

function sectionTitle(text, n, color){
  const h=document.createElement("div"); h.className="sec-title";
  const t=document.createElement("span"); t.textContent=text;
  const b=document.createElement("span"); b.className="badge"; b.style.background=color; b.style.color="#fff";
  b.textContent=n+" 组"; h.appendChild(t); h.appendChild(b); return h;
}

function groupCard(g, idx, kind){
  const card=document.createElement("div"); card.className="group";
  const head=document.createElement("div"); head.className="group-head";
  const left=document.createElement("div");
  const r=document.createElement("div"); r.className="reason"; r.textContent=(kind==="hash"?"● 哈希重复":"● 文件名相似");
  const d=document.createElement("div"); d.className="detail"; d.textContent=g.detail+" · 共 "+g.files.length+" 个";
  left.appendChild(r); left.appendChild(d);
  const acts=document.createElement("div"); acts.className="group-actions";
  const selBtn=document.createElement("button"); selBtn.className="ghost"; selBtn.textContent="选可删";
  selBtn.style.padding="4px 10px"; selBtn.style.fontSize="12px";
  selBtn.onclick=()=>{ card.querySelectorAll("input[type=checkbox]").forEach(c=>{ if(!c.dataset.keep) c.checked=true; }); updateSelCount(); };
  const skipBtn=document.createElement("button"); skipBtn.className="ghost"; skipBtn.textContent="跳过";
  skipBtn.style.padding="4px 10px"; skipBtn.style.fontSize="12px";
  skipBtn.onclick=()=>{ card.querySelectorAll("input[type=checkbox]").forEach(c=>c.checked=false); updateSelCount(); };
  acts.appendChild(selBtn); acts.appendChild(skipBtn);
  head.appendChild(left); head.appendChild(acts);
  card.appendChild(head);

  // 默认保留第一个（按路径排序），其余预选删除
  const files=[...g.files].sort((a,b)=>a.path.localeCompare(b.path));
  files.forEach((f,fi)=>{
    const row=document.createElement("div"); row.className="file";
    const cb=document.createElement("input"); cb.type="checkbox"; cb.value=f.path;
    const keep = fi===0;
    if(keep){ cb.dataset.keep="1"; } else { cb.checked=true; }
    cb.onchange=updateSelCount;
    const p=document.createElement("div"); p.className="p"; p.textContent=f.path;
    const s=document.createElement("div"); s.className="s"; s.textContent=fmtSize(f.size);
    row.appendChild(cb); row.appendChild(p); row.appendChild(s);
    if(keep){ const k=document.createElement("div"); k.className="keep"; k.textContent="保留"; row.appendChild(k); }
    card.appendChild(row);
  });
  return card;
}

function getAllChecked(){
  return [...document.querySelectorAll("#results input[type=checkbox]:checked")].map(c=>c.value);
}
function updateSelCount(){
  const n=getAllChecked().length;
  $("selCount").textContent="已选 "+n+" 个待删除";
}

$("scanBtn").onclick=startScan;
$("selAll").onclick=()=>{ document.querySelectorAll("#results input[type=checkbox]").forEach(c=>c.checked=true); updateSelCount(); };
$("selNone").onclick=()=>{ document.querySelectorAll("#results input[type=checkbox]").forEach(c=>c.checked=false); updateSelCount(); };
$("skipBtn").onclick=()=>{ document.querySelectorAll("#results input[type=checkbox]").forEach(c=>c.checked=false); updateSelCount(); toast("已跳过，全部保留。"); };

$("delBtn").onclick=()=>{
  const paths=[...new Set(getAllChecked())];
  if(!paths.length){ toast("没有选中任何文件"); return; }
  currentDeletePaths=paths;
  $("modalText").textContent="即将删除 "+paths.length+" 个文件。建议先“移入回收站”以便恢复。";
  const ul=$("modalList"); ul.innerHTML="";
  paths.slice(0,200).forEach(p=>{ const li=document.createElement("li"); li.textContent=p; ul.appendChild(li); });
  if(paths.length>200){ const li=document.createElement("li"); li.textContent="… 其余 "+(paths.length-200)+" 个省略"; ul.appendChild(li); }
  $("modal").style.display="flex";
};
$("mCancel").onclick=()=>{ $("modal").style.display="none"; };
$("mTrash").onclick=()=>confirmDelete(true);
$("mPermanent").onclick=()=>confirmDelete(false);

function confirmDelete(toTrash){
  $("modal").style.display="none";
  const paths=currentDeletePaths;
  fetch("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({paths, to_trash:toTrash})})
  .then(r=>r.json())
  .then(res=>{
    const ok=res.deleted.length, fail=Object.keys(res.failed).length;
    toast("已删除 "+ok+" 个"+(fail?("，失败 "+fail+" 个"):"")+ (toTrash?"（回收站）":"（永久）"));
    if(ok){ // 从界面移除已删除项
      ok.forEach(p=>{ document.querySelectorAll('#results input[value="'+cssEscape(p)+'"]').forEach(c=>{
        const row=c.closest(".file"); if(row) row.remove();
      }); });
    }
    if(fail){ const msgs=Object.entries(res.failed).map(([k,v])=>k+": "+v).join("\n"); alert("部分删除失败：\n"+msgs); }
    updateSelCount();
  })
  .catch(e=>toast("删除请求失败："+e));
}
function cssEscape(s){ return s.replace(/["\\]/g,"\\$&"); }

$("exitBtn").onclick=()=>{
  if(!confirm("确定退出程序吗？本地服务将停止。")) return;
  fetch("/api/shutdown",{method:"POST"}).catch(()=>{});
  document.body.innerHTML="<div style='padding:60px;text-align:center;font-family:sans-serif;color:#6b7280'>程序已退出，可以关闭此窗口了。</div>";
};
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            data = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parsed.path == "/api/stream":
            qs = parse_qs(parsed.query)
            sid = qs.get("scan_id", [None])[0]
            self.serve_stream(sid)
        elif parsed.path == "/api/shutdown":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            threading.Thread(target=_delayed_shutdown, daemon=True).start()
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        if parsed.path == "/api/scan":
            global _scan_counter
            with _scan_lock:
                _scan_counter += 1
                sid = "scan_" + str(_scan_counter)
            scans[sid] = {
                "status": "queued", "total": 0, "scanned": 0,
                "current": "", "hash_groups": [], "name_groups": [],
                "error": None,
            }
            t = threading.Thread(
                target=scan_folder,
                args=(payload.get("folder", ""), payload.get("recursive", True),
                      payload.get("check_name", True), float(payload.get("threshold", 0.8)), sid),
                daemon=True,
            )
            t.start()
            self._send_json({"scan_id": sid})
        elif parsed.path == "/api/delete":
            res = do_delete(payload.get("paths", []), bool(payload.get("to_trash", True)))
            self._send_json(res)
        elif parsed.path == "/api/pick-folder":
            self._send_json(pick_folder_dialog(payload.get("initial", "")))
        elif parsed.path == "/api/shutdown":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            threading.Thread(target=_delayed_shutdown, daemon=True).start()
        else:
            self.send_error(404)

    def serve_stream(self, sid):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                s = scans.get(sid)
                if not s:
                    self.wfile.write(b"data: \"__gone__\"\n\n")
                    break
                obj = {
                    "status": s["status"],
                    "total": s.get("total", 0),
                    "scanned": s.get("scanned", 0),
                    "current": s.get("current", ""),
                    "hash_groups": s["hash_groups"] if s["status"] == "done" else [],
                    "name_groups": s["name_groups"] if s["status"] == "done" else [],
                    "error": s.get("error"),
                }
                self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                if s["status"] in ("done", "error"):
                    break
                time.sleep(0.2)
        except Exception:
            pass

    def log_message(self, *args):
        pass


def _msgbox(text, title="重复文件检查器"):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(text), str(title), 0x10)
    except Exception:
        pass


# ---------------------------------------------------------------
# 原生文件夹选择对话框
# 优先使用现代 IFileOpenDialog（Win Vista+，可粘贴路径/带侧边栏），
# 失败时回退到经典 SHBrowseForFolder（树形选择）。
# ---------------------------------------------------------------

_dialog_lock = threading.Lock()


def _com_call(ptr, index, argtypes, *args):
    """按虚表下标调用 COM 接口方法，返回 HRESULT（不自动抛异常）。"""
    import ctypes
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    fn = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(fn)(ptr, *args)


def _com_release(ptr):
    try:
        _com_call(ptr, 2, [])
    except Exception:
        pass


def _pick_folder_modern(initial=""):
    """IFileOpenDialog + FOS_PICKFOLDERS。返回路径 / None(取消) / 抛异常(不可用)。"""
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32),
                    ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16),
                    ("Data4", ctypes.c_ubyte * 8)]

    def guid(s):
        g = GUID()
        if ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g)) != 0:
            raise OSError("bad guid")
        return g

    CLSID_FileOpenDialog = guid("{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}")
    IID_IFileOpenDialog = guid("{D57C7288-D4AD-4768-BE02-9D969532D960}")
    IID_IShellItem = guid("{43826D1E-E718-42EE-BC55-A1E261C37BFE}")

    # 虚表下标：IUnknown(0-2) / IModalWindow.Show(3) / IFileDialog...
    SHOW, SET_OPTIONS, GET_OPTIONS, SET_FOLDER, SET_TITLE, GET_RESULT = 3, 9, 10, 12, 17, 20
    SI_GET_DISPLAY_NAME = 5

    FOS_PICKFOLDERS = 0x00000020
    FOS_FORCEFILESYSTEM = 0x00000040
    FOS_PATHMUSTEXIST = 0x00000800
    SIGDN_FILESYSPATH = 0x80058000
    CLSCTX_INPROC_SERVER = 1
    CANCELLED = -2147023673  # 0x800704C7

    ole32.CoInitialize(None)
    dlg = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(ctypes.byref(CLSID_FileOpenDialog), None,
                                    CLSCTX_INPROC_SERVER,
                                    ctypes.byref(IID_IFileOpenDialog),
                                    ctypes.byref(dlg))
        if hr != 0 or not dlg.value:
            raise OSError("CoCreateInstance failed: 0x%X" % (hr & 0xFFFFFFFF))

        opts = ctypes.c_uint32()
        _com_call(dlg, GET_OPTIONS, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(opts))
        _com_call(dlg, SET_OPTIONS, [ctypes.c_uint32],
                  opts.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST)
        _com_call(dlg, SET_TITLE, [ctypes.c_wchar_p], "选择要检查重复文件的文件夹")

        # 若已填了有效路径，则以它作为初始位置
        if initial and os.path.isdir(initial):
            item = ctypes.c_void_p()
            hr2 = shell32.SHCreateItemFromParsingName(
                ctypes.c_wchar_p(os.path.abspath(initial)), None,
                ctypes.byref(IID_IShellItem), ctypes.byref(item))
            if hr2 == 0 and item.value:
                _com_call(dlg, SET_FOLDER, [ctypes.c_void_p], item)
                _com_release(item)

        # 以当前前台窗口（通常是浏览器）为父窗口，保证对话框显示在最前
        try:
            hwnd = user32.GetForegroundWindow()
        except Exception:
            hwnd = 0

        hr = _com_call(dlg, SHOW, [ctypes.c_void_p], hwnd)
        if hr == CANCELLED or (hr & 0xFFFFFFFF) == 0x800704C7:
            return None
        if hr != 0:
            raise OSError("Show failed: 0x%X" % (hr & 0xFFFFFFFF))

        item = ctypes.c_void_p()
        if _com_call(dlg, GET_RESULT, [ctypes.c_void_p], ctypes.byref(item)) != 0 or not item.value:
            return None
        try:
            buf = ctypes.c_wchar_p()
            if _com_call(item, SI_GET_DISPLAY_NAME,
                         [ctypes.c_uint32, ctypes.POINTER(ctypes.c_wchar_p)],
                         SIGDN_FILESYSPATH, ctypes.byref(buf)) != 0:
                return None
            path = buf.value
            ole32.CoTaskMemFree(buf)
            return path
        finally:
            _com_release(item)
    finally:
        if dlg.value:
            _com_release(dlg)
        try:
            ole32.CoUninitialize()
        except Exception:
            pass


def _pick_folder_classic():
    """经典 SHBrowseForFolder 回退方案。"""
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    user32 = ctypes.windll.user32

    class BROWSEINFO(ctypes.Structure):
        _fields_ = [("hwndOwner", wintypes.HWND),
                    ("pidlRoot", ctypes.c_void_p),
                    ("pszDisplayName", ctypes.c_wchar_p),
                    ("lpszTitle", ctypes.c_wchar_p),
                    ("ulFlags", ctypes.c_uint),
                    ("lpfn", ctypes.c_void_p),
                    ("lParam", ctypes.c_void_p),
                    ("iImage", ctypes.c_int)]

    BIF_RETURNONLYFSDIRS = 0x0001
    BIF_NEWDIALOGSTYLE = 0x0040
    MAX_PATH = 260

    ole32.CoInitialize(None)
    try:
        disp = ctypes.create_unicode_buffer(MAX_PATH)
        bi = BROWSEINFO()
        try:
            bi.hwndOwner = user32.GetForegroundWindow()
        except Exception:
            bi.hwndOwner = 0
        bi.pszDisplayName = ctypes.cast(disp, ctypes.c_wchar_p)
        bi.lpszTitle = "选择要检查重复文件的文件夹"
        bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE

        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return None
        try:
            buf = ctypes.create_unicode_buffer(MAX_PATH)
            shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            if not shell32.SHGetPathFromIDListW(pidl, buf):
                return None
            return buf.value
        finally:
            ole32.CoTaskMemFree(pidl)
    finally:
        try:
            ole32.CoUninitialize()
        except Exception:
            pass


def pick_folder_dialog(initial=""):
    """弹出系统文件夹选择框。返回 {"path": str} / {"cancelled": True} / {"error": str}"""
    if os.name != "nt":
        return {"error": "当前系统不支持原生选择框，请手动输入路径。"}
    # 同一时间只允许一个对话框，避免重复点击弹出多个
    if not _dialog_lock.acquire(blocking=False):
        return {"error": "选择窗口已打开，请先在该窗口中完成选择。"}
    try:
        try:
            path = _pick_folder_modern(initial)
        except Exception:
            try:
                path = _pick_folder_classic()
            except Exception as e:
                return {"error": "无法打开系统选择框：%s" % e}
        if not path:
            return {"cancelled": True}
        return {"path": path}
    finally:
        _dialog_lock.release()


def _delayed_shutdown():
    """稍等片刻后关闭服务（让响应先发完）。"""
    global httpd_instance
    time.sleep(0.3)
    if httpd_instance is not None:
        try:
            httpd_instance.shutdown()
        except Exception:
            pass


def main():
    port = PORT
    no_browser = False
    for a in sys.argv[1:]:
        if a.startswith("--port"):
            try:
                port = int(a.split("=")[1])
            except Exception:
                pass
        elif a in ("--no-browser", "-n"):
            no_browser = True

    httpd = None
    actual_port = None
    # 端口自动避让：若被占用则顺延
    for try_port in range(port, port + 20):
        try:
            httpd = http.server.ThreadingHTTPServer((HOST, try_port), Handler)
            actual_port = try_port
            break
        except OSError:
            continue
    if httpd is None:
        _msgbox("无法启动本地服务：端口 %d 起均被占用。" % port)
        return
    global httpd_instance
    httpd_instance = httpd

    url = "http://%s:%d" % (HOST, actual_port)
    print("重复文件检查器已启动：", url)
    print("在浏览器中打开上面的地址即可使用。关闭此窗口或按 Ctrl+C 退出。")
    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
