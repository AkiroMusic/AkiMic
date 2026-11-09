# AkiMic — Wireless Microphone for Windows

[![Website](https://img.shields.io/badge/site-akiromusic.github.io-6D82FF)](https://akiromusic.github.io/AkiMic/)
[![Release](https://img.shields.io/github/v/release/AkiroMusic/AkiMic?style=flat-square&label=Latest%20Release&color=6C8CFF)](https://github.com/AkiroMusic/AkiMic/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-2CC5E0?style=flat-square)](https://github.com/AkiroMusic/AkiMic)
[![License](https://img.shields.io/badge/license-GPL--3.0-6C8CFF?style=flat-square)](LICENSE)

**EN** | [中文](#中文)

**AkiMic** turns your phone into a wireless microphone for your Windows PC. Audio streams over LAN WebRTC straight into a virtual audio device — one click to set up, nothing to install on the phone: scan a QR code, tap once, talk.

> **v1.0.0 — One-click flow**
> Launch and everything is automatic: virtual driver check, firewall rules (no security popups), trusted local certificate, QR pairing and device matching. Auto-reconnect survives lock screens and Wi-Fi hiccups.

## Download

| Platform | File | Notes |
|----------|------|-------|
| Windows 10/11 x64 | [AkiMic.exe](https://github.com/AkiroMusic/AkiMic/releases/latest/download/AkiMic.exe) | Portable single exe — run as administrator (first run sets up the virtual device) |

Requires the free [VB-CABLE](https://vb-audio.com/Cable/) virtual audio device — AkiMic detects and installs it automatically on first launch.

---

Turn your phone into your PC's wireless microphone — one click, over LAN WebRTC, with an Aurora Glass UI.

AkiMic is a personal rewrite inspired by [ZeroMic](https://github.com/hypixice/ZeroMic) (GPL-3.0). No app needed on the phone: scan a QR code, tap once, talk.

## Features

- **One-click flow** — launch, and everything else is automatic: driver check (silent install on first run), firewall rules (TCP signaling + UDP ICE, LAN-only, no Windows security popups), persistent certificate trusted by the PC, QR pairing, device matching, default-speaker protection.
- **Aurora Dusk UI** — built on the Aki Design System, flat rendering: Aurora Dusk palette, clean panels, 4 switchable color packs, real RMS level meters, round-trip latency readout. Freely resizable window.
- **Auto-reconnect** — the phone re-negotiates WebRTC automatically after lock screen, backgrounding, or Wi-Fi hiccups; the desktop resets cleanly when the phone leaves.
- **Persistent settings** — language, theme, gains and output device are saved next to the exe (`settings.json`), portable-style.
- **System tray** — minimize to tray; mute from the tray; single-instance guard.

## How it works

```
Phone browser (mic) ──WebRTC P2P over LAN──▶ Desktop WebView2 ──Web Audio──▶ "CABLE Input"
                                                                              │
                                              VB-CABLE virtual device ◀──────┘
                                                                              │
Apps (Discord / games) pick "CABLE Output" as microphone ◀──────────────────┘
```

- Python only handles signaling (Socket.IO), the bundled UI, driver management and the window shell — **audio never passes through Python**, which keeps latency at LAN-WebRTC level.
- HTTPS uses a throwaway self-signed certificate (browsers require secure context for microphone access). Accept the warning on the phone once.

## Requirements

- Windows 10/11 with WebView2 Runtime (preinstalled on current builds)
- Python **3.12 / 3.13** for development (`pywebview` does not support 3.14 yet)
- Phone and PC on the same Wi-Fi

## Run from source

```bat
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-windows.txt
.venv\Scripts\python main.py
```

Note: without admin rights the firewall rules, certificate trust install and first-run driver install are skipped (your machine likely already has VB-CABLE installed, in which case everything else works).

## Build single exe

```bat
build.bat
```

Output: `release\AkiMic.exe` (kept current). PyInstaller work dirs are cleaned up automatically. The exe requests admin via its manifest and bundles the web UI + VB-CABLE setup.

## Usage

1. Run AkiMic (as admin).
2. Scan the QR with your phone, accept the certificate warning once.
3. Tap the mic button on the phone.
4. In Discord / your game, set the microphone to **CABLE Output (VB-Audio Virtual Cable)**.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Phone can't open the URL | Same Wi-Fi? Corporate APs isolate clients — use a hotspot if needed. |
| No microphone in apps | Select "CABLE Output (VB-Audio Virtual Cable)" as the input device; restart the app if it was running before. |
| PC lost default speaker after first run | AkiMic restores it automatically; if it still changed, set it back manually once. |
| Cert warning on phone | Expected (self-signed LAN HTTPS) → Advanced → Proceed. |

## Project layout

```
akimic/           Python package (server, driver, tray, settings, net, main)
webui/            desktop + mobile pages, shared tokens.css, lang packs
assets/           vbcable.zip, icon-src.png + generated icon.ico/icon.png
release/          latest packaged AkiMic.exe (mirrored on every build)
scripts/          make_icon.py, smoke_test.py
```

## License

GPL-3.0 (see `LICENSE`). Inspired by ZeroMic by Hypixice Studio; VB-CABLE is a separate donationware product by VB-Audio.

---

# 中文

**AkiMic** 把手机变成 Windows 电脑的无线麦克风：音频经局域网 WebRTC 直连进虚拟声卡——一键配置，手机无需安装任何 App：扫码、点一下、开说。

> **v1.0.0 —— 一键流程**
> 启动后全自动：虚拟驱动检测、防火墙放行（不弹安全警报）、本机受信任证书、扫码配对与设备匹配。自动重连可穿越锁屏与 Wi-Fi 抖动。

## 下载

| 平台 | 文件 | 说明 |
|------|------|------|
| Windows 10/11 x64 | [AkiMic.exe](https://github.com/AkiroMusic/AkiMic/releases/latest/download/AkiMic.exe) | 绿色单文件——以管理员运行（首跑自动配置虚拟设备） |

需要免费的 [VB-CABLE](https://vb-audio.com/Cable/) 虚拟声卡——AkiMic 首次启动会自动检测并安装。

---

把手机变成电脑的无线麦克风——一键启动、局域网 WebRTC、Aurora Glass 界面。

AkiMic 参考开源项目 [ZeroMic](https://github.com/hypixice/ZeroMic)（GPL-3.0）为个人使用重写。手机无需装任何 App：扫码、点一下、开说。

## 特性

- **一键流程**——启动后全自动：驱动检测（首跑静默安装）、防火墙放行（TCP 信令 + UDP ICE，仅限局域网，不再弹安全警报）、持久化证书受电脑信任、扫码配对、设备自动匹配、默认扬声器保护恢复。
- **Aurora Dusk 界面**——基于 Aki 设计系统、平面化渲染：Aurora Dusk 配色、干净面板、4 套可切换配色、真实 RMS 电平表、往返延迟显示。窗口自由缩放。
- **自动重连**——手机锁屏 / 切后台 / Wi-Fi 抖动后自动重建 WebRTC；手机离开时桌面端干净复位。
- **设置持久化**——语言、主题、增益、输出设备保存在 exe 旁（`settings.json`），绿色便携。
- **系统托盘**——最小化到托盘，托盘可静音，单实例保护。

## 工作原理

```
手机浏览器（麦克风）──局域网 WebRTC 直连──▶ 电脑 WebView2 ──Web Audio──▶ "CABLE Input"
                                                                        │
                                        VB-CABLE 虚拟声卡环回 ◀──────────┘
                                                                        │
游戏 / Discord 选 "CABLE Output" 作为麦克风 ◀───────────────────────────┘
```

- Python 只负责信令（Socket.IO）、内置网页、驱动管理与窗口壳，**音频数据完全不经过 Python**，延迟就是局域网 WebRTC 的延迟。
- HTTPS 使用临时自签名证书（浏览器要求安全上下文才给麦克风权限），手机上首次需手动信任一次。

## 环境要求

- Windows 10/11 + WebView2 Runtime（新版系统自带）
- 开发用 Python **3.12 / 3.13**（`pywebview` 暂不支持 3.14）
- 手机与电脑同一 Wi-Fi

## 源码运行

```bat
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-windows.txt
.venv\Scripts\python main.py
```

说明：非管理员运行时会跳过防火墙规则、证书信任安装与首跑驱动安装（本机若已装 VB-CABLE，其余功能不受影响）。

## 打包单文件

```bat
build.bat
```

输出：`release\AkiMic.exe`（保持最新），PyInstaller 中间目录构建后自动清理。exe 的 manifest 自动请求管理员，内嵌网页与 VB-CABLE 安装包。

## 使用

1. 以管理员运行 AkiMic。
2. 手机扫二维码，证书警告「高级 → 继续访问」一次。
3. 手机上点按麦克风按钮。
4. 在 Discord / 游戏里把麦克风输入设为 **CABLE Output (VB-Audio Virtual Cable)**。

## 常见问题

| 现象 | 处理 |
|---|---|
| 手机打不开地址 | 确认同一 Wi-Fi；部分公共 AP 开了客户端隔离，可改用手机热点。 |
| 软件里找不到麦克风 | 输入设备选 "CABLE Output (VB-Audio Virtual Cable)"；软件是开着的就重启它。 |
| 首跑后默认扬声器变了 | AkiMic 会自动恢复；若仍变化，手动切回一次即可。 |
| 手机提示证书不安全 | 正常现象（局域网自签名 HTTPS）→ 高级 → 继续访问。 |

## 项目布局

```
akimic/           Python 包（server / driver / tray / settings / net / main）
webui/            桌面端 + 手机端页面、共享 tokens.css、语言包
assets/           vbcable.zip、icon-src.png 及生成的 icon.ico / icon.png
release/          最新打包的 AkiMic.exe（每次构建自动同步）
scripts/          make_icon.py、smoke_test.py
```

## 许可

GPL-3.0（见 `LICENSE`）。参考 Hypixice Studio 的 ZeroMic 重写；VB-CABLE 为 VB-Audio 的捐赠软件，版权归其所有。
