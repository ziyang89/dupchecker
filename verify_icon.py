#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
启动重复文件检查器.exe，检查标题栏/任务栏图标句柄是否非零且不同。
"""
import ctypes
import os
import subprocess
import sys
import time

exe = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "dist_gui", "重复文件检查器.exe")
if not os.path.exists(exe):
    print("EXE not found:", exe)
    sys.exit(1)

print("Launching", exe)
proc = subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p
    ]
    user32.SendMessageW.restype = ctypes.c_void_p

    hwnd = None
    deadline = time.time() + 10
    while time.time() < deadline:
        hwnd = user32.FindWindowW(None, "重复文件检查器")
        if hwnd:
            break
        time.sleep(0.1)

    if not hwnd:
        print("Window not found")
        sys.exit(1)

    WM_GETICON = 0x7F
    ICON_SMALL = 0
    ICON_BIG = 1
    h_small = user32.SendMessageW(hwnd, WM_GETICON, ctypes.c_void_p(ICON_SMALL), ctypes.c_void_p(0))
    h_big = user32.SendMessageW(hwnd, WM_GETICON, ctypes.c_void_p(ICON_BIG), ctypes.c_void_p(0))
    h_class_big = user32.GetClassLongPtrW(hwnd, -14)
    h_class_small = user32.GetClassLongPtrW(hwnd, -34)
    print("HWND =", hwnd)
    print("ICON_SMALL handle =", h_small)
    print("ICON_BIG   handle =", h_big)
    print("CLASS_BIG  handle =", h_class_big)
    print("CLASS_SMALL handle =", h_class_small)
    if h_small or h_big:
        print("OK: window icons are set")
    else:
        print("FAIL: no icon handles")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()
