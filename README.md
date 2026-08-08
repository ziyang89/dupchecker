# 重复文件检查器 (DupChecker)

一个**纯本地、零依赖**的 Windows 重复文件查找工具。双击即用，不需要安装 Python，也不需要联网。

<img width="1002" height="725" alt="ScreenShot_2026-08-08_151225_474" src="https://github.com/user-attachments/assets/a39dd1ee-49c4-4261-8f1b-7d48d49ecec3" />

## 功能

- **两种判重方式**（满足任一即视为重复）：
  - **按内容**：SHA-256 哈希完全一致（分块计算，省内存）。
  - **按文件名**：文件名归一化后相同（如 `photo.jpg` 与 `photo(1).jpg`），或同目录同扩展名下模糊相似度达到阈值。
- **性能优化**：只对「大小相同」的文件计算哈希——大小不同的文件内容必然不同，无需读盘，大文件夹也能秒扫。
- **原生桌面界面**（tkinter），不依赖浏览器，不依赖任何第三方库。
- **安全删除**：默认移入回收站（可恢复），也可选择永久删除（二次确认）。
- **智能建议**：每组默认保留最早修改的文件，其余预选删除；可一键「全选建议项」。
- **实时进度**：进度条上方显示当前阶段与进度百分比。

## 下载与使用

### 方式一：直接下载 exe（推荐普通用户）

到 [Releases](https://github.com/ziyang89/dupchecker/releases) 页面下载 `DupChecker.exe`，双击即可运行。
也可直接下载：[DupChecker.exe v1.0](https://github.com/ziyang89/dupchecker/releases/download/v1.0/DupChecker.exe)

> 单文件、无需安装。建议关闭杀软误报白名单后再运行；首次启动因自解压会稍慢 2~4 秒。

### 方式二：源码运行（开发者）

```bash
pip install tk        # 仅 Windows 标准库自带，通常无需安装
python dupchecker_gui.py
```

## 界面说明

1. 点「浏览…」选择要检查的文件夹（可勾选「包含子文件夹」递归扫描）。
2. 选择判重方式：按内容 / 按文件名，并可拖动「名称相似度」滑杆（50%~95%）。
3. 点「开始扫描」，进度条上方会实时显示进度。
4. 结果以分组形式展示，单击文件名前的方框勾选要删除的文件，或点「全选建议项」。
5. 点「删除选中」执行删除（默认进回收站，可勾选「永久删除」）。
6. 快捷键：`F5` 重新扫描、`Esc` 停止扫描、双击文件在资源管理器中定位。

## 打包为独立 exe

```bash
pip install pyinstaller pillow
python build_icon.py                       # 生成多尺寸 appicon.ico
pyinstaller --onefile --noconsole --name 重复文件检查器 重复文件检查器.spec
```

产物位于 `dist_gui/重复文件检查器.exe`。

## 文件结构

| 文件 | 说明 |
| --- | --- |
| `dupchecker_gui.py` | tkinter 原生桌面界面主程序 |
| `dupcore.py` | 核心逻辑（扫描 / 判重 / 删除），与界面解耦 |
| `build_icon.py` | 生成「双文档 + 放大镜」多尺寸图标 |
| `appicon.ico` / `appicon_master.png` | 软件图标资源 |
| `重复文件检查器.spec` | PyInstaller 打包配置 |
| `dist_gui/重复文件检查器.exe` | 打包好的单文件可执行程序 |
| `dupchecker.py` | 旧版 Web 版后端（备用，不推荐） |

## 许可证

MIT
