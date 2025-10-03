"""
Tkinter UI for selecting an Android device and running log collection.

This UI wraps the existing `logcat_rotate.py` script. It discovers attached
devices via `adb devices -l`, lets the user choose a target device, select an
output directory, and configure common options like prefix and retention hours.

Implementation notes:
- Device selection is passed via environment variable `ANDROID_SERIAL`, which
  `adb` respects. This avoids modifying `logcat_rotate.py` if it doesn't
  support a `--device` flag.
- The collector runs in a background subprocess; stdout/stderr are streamed
  into the UI log window.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk


ADB_EXE = "adb"
DEFAULT_PREFIX = "bt"
DEFAULT_RETENTION = 36
DEFAULT_BUGREPORT_COOLDOWN = 900
DEFAULT_BUGREPORT_DIR = "bugreports"


@dataclass
class Device:
    serial: str
    label: str  # Human friendly label, e.g., "emulator-5554 • Pixel 7 (device)"
    state: str  # device | offline | unauthorized | unknown


def which(program: str) -> Optional[str]:
    """Return absolute path if program exists in PATH, else None."""
    paths = os.environ.get("PATH", "").split(os.pathsep)
    exts = [""]
    if os.name == "nt":
        # Typical executable extensions on Windows
        pathext = os.environ.get("PATHEXT", ".EXE;.BAT;.CMD")
        exts = pathext.lower().split(";")
    for p in paths:
        candidate = Path(p) / program
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        if os.name == "nt":
            for ext in exts:
                candidate_ext = Path(p) / f"{program}{ext}"
                if candidate_ext.is_file() and os.access(candidate_ext, os.X_OK):
                    return str(candidate_ext)
    return None


def list_adb_devices() -> List[Device]:
    """Return a list of connected ADB devices with state and friendly labels."""
    try:
        proc = subprocess.run(
            [ADB_EXE, "devices", "-l"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        return []
    except subprocess.CalledProcessError as e:
        # Return empty list on failure; caller can show error
        return []

    lines = proc.stdout.splitlines()
    devices: List[Device] = []
    for line in lines[1:]:  # Skip header "List of devices attached"
        line = line.strip()
        if not line:
            continue
        # Expected formats:
        #   emulator-5554	device product:sdk_gphone_x86 model:Android_SDK_built_for_x86 ...
        #   R3CN30...	unauthorized
        parts = line.split()
        if not parts:
            continue
        serial = parts[0]
        state = parts[1] if len(parts) > 1 else "unknown"
        extras = " ".join(parts[2:]) if len(parts) > 2 else ""
        label = serial
        if extras:
            # Try to surface model or device from extras if available
            model = _extract_kv(extras, "model:")
            device_raw = _extract_kv(extras, "device:")
            product = _extract_kv(extras, "product:")
            pretty_bits = [b for b in [model, device_raw, product] if b]
            if pretty_bits:
                label = f"{serial} • {' / '.join(pretty_bits)} ({state})"
            else:
                label = f"{serial} ({state})"
        else:
            label = f"{serial} ({state})"
        devices.append(Device(serial=serial, label=label, state=state))
    return devices


def _extract_kv(blob: str, key: str) -> Optional[str]:
    for token in blob.split():
        if token.startswith(key):
            return token[len(key) :]
    return None


class LogCollectorUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Android Log Collector UI")
        self.geometry("760x520")

        # State
        self.devices: List[Device] = []
        self.proc: Optional[subprocess.Popen] = None
        self.reader_thread: Optional[threading.Thread] = None
        self.output_queue: "queue.Queue[str]" = queue.Queue()
        self._stop_reader = threading.Event()

        # UI variables
        self.selected_device = tk.StringVar()
        self.output_dir = tk.StringVar(value=str(Path.cwd() / "logs"))
        self.prefix = tk.StringVar(value=DEFAULT_PREFIX)
        self.retention = tk.StringVar(value=str(DEFAULT_RETENTION))
        self.bugreport_enabled = tk.BooleanVar(value=True)
        self.bugreport_cooldown = tk.StringVar(value=str(DEFAULT_BUGREPORT_COOLDOWN))
        self.bugreport_keywords = tk.StringVar(value="")
        self.bugreport_dir = tk.StringVar(value=DEFAULT_BUGREPORT_DIR)

        # Build widgets
        self._build_widgets()

        # Initial load
        self._refresh_adb_path_status()
        self.refresh_devices()
        self._append_log("UI 啟動完成。請選擇裝置與輸出資料夾後開始。\n")

        # Periodic polling for output
        self.after(120, self._drain_output_queue)

    # UI construction
    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 6}

        frame_top = ttk.Frame(self)
        frame_top.pack(fill=tk.X, **pad)

        # ADB path status
        ttk.Label(frame_top, text="ADB:").grid(row=0, column=0, sticky=tk.W)
        self.adb_path_lbl = ttk.Label(frame_top, text="檢查中…")
        self.adb_path_lbl.grid(row=0, column=1, sticky=tk.W, columnspan=3)

        # Device selection
        ttk.Label(frame_top, text="裝置:").grid(row=1, column=0, sticky=tk.W)
        self.device_combo = ttk.Combobox(
            frame_top, textvariable=self.selected_device, state="readonly", width=60
        )
        self.device_combo.grid(row=1, column=1, sticky=tk.W)
        ttk.Button(frame_top, text="重新整理", command=self.refresh_devices).grid(
            row=1, column=2, sticky=tk.W
        )

        # Output directory
        ttk.Label(frame_top, text="輸出資料夾:").grid(row=2, column=0, sticky=tk.W)
        self.dir_entry = ttk.Entry(frame_top, textvariable=self.output_dir, width=50)
        self.dir_entry.grid(row=2, column=1, sticky=tk.W)
        ttk.Button(frame_top, text="瀏覽…", command=self.browse_dir).grid(
            row=2, column=2, sticky=tk.W
        )

        # Prefix & retention
        ttk.Label(frame_top, text="前綴:").grid(row=3, column=0, sticky=tk.W)
        ttk.Entry(frame_top, textvariable=self.prefix, width=12).grid(
            row=3, column=1, sticky=tk.W
        )
        ttk.Label(frame_top, text="保留(小時):").grid(row=3, column=2, sticky=tk.W)
        ttk.Entry(frame_top, textvariable=self.retention, width=6).grid(
            row=3, column=3, sticky=tk.W
        )

        # Bugreport settings
        ttk.Checkbutton(
            frame_top, text="啟用 bugreport", variable=self.bugreport_enabled
        ).grid(row=4, column=0, sticky=tk.W)
        ttk.Label(frame_top, text="冷卻(秒):").grid(row=4, column=2, sticky=tk.W)
        ttk.Entry(frame_top, textvariable=self.bugreport_cooldown, width=8).grid(
            row=4, column=3, sticky=tk.W
        )
        ttk.Label(frame_top, text="關鍵字(逗號分隔):").grid(row=5, column=0, sticky=tk.W)
        ttk.Entry(frame_top, textvariable=self.bugreport_keywords, width=50).grid(
            row=5, column=1, sticky=tk.W, columnspan=2
        )
        ttk.Label(frame_top, text="Bugreport 目錄:").grid(row=6, column=0, sticky=tk.W)
        ttk.Entry(frame_top, textvariable=self.bugreport_dir, width=50).grid(
            row=6, column=1, sticky=tk.W
        )
        ttk.Button(frame_top, text="瀏覽…", command=self.browse_bug_dir).grid(
            row=6, column=2, sticky=tk.W
        )

        # Start/Stop buttons
        frame_btn = ttk.Frame(self)
        frame_btn.pack(fill=tk.X, **pad)
        self.start_btn = ttk.Button(frame_btn, text="開始", command=self.start_collection)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(
            frame_btn, text="停止", command=self.stop_collection, state=tk.DISABLED
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(8, 0))

        # Log output
        frame_log = ttk.LabelFrame(self, text="輸出")
        frame_log.pack(fill=tk.BOTH, expand=True, **pad)
        self.text = tk.Text(frame_log, height=20, wrap=tk.NONE)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll = ttk.Scrollbar(frame_log, orient=tk.VERTICAL, command=self.text.yview)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.configure(yscrollcommand=yscroll.set)

    def _refresh_adb_path_status(self) -> None:
        adb_path = which(ADB_EXE)
        if adb_path:
            self.adb_path_lbl.configure(text=f"已找到: {adb_path}")
        else:
            self.adb_path_lbl.configure(text="找不到 adb，請先安裝並加入 PATH")

    # Event handlers
    def browse_dir(self) -> None:
        sel = filedialog.askdirectory(title="選擇輸出資料夾", initialdir=self.output_dir.get())
        if sel:
            self.output_dir.set(sel)

    def browse_bug_dir(self) -> None:
        base = self.output_dir.get() or str(Path.cwd() / "logs")
        sel = filedialog.askdirectory(title="選擇 Bugreport 目錄", initialdir=base)
        if sel:
            # allow absolute path; if under output_dir, store relative for neatness
            try:
                out = Path(self.output_dir.get()).resolve()
                p = Path(sel).resolve()
                if str(p).startswith(str(out)):
                    try:
                        rel = p.relative_to(out)
                        self.bugreport_dir.set(str(rel))
                        return
                    except Exception:
                        pass
            except Exception:
                pass
            self.bugreport_dir.set(sel)

    def refresh_devices(self) -> None:
        devices = list_adb_devices()
        self.devices = devices
        labels = [d.label for d in devices]
        self.device_combo.configure(values=labels)
        # Auto-select the first 'device' state if any
        selected_idx = next((i for i, d in enumerate(devices) if d.state == "device"), -1)
        if selected_idx >= 0:
            self.device_combo.current(selected_idx)
            self.selected_device.set(labels[selected_idx])
        elif labels:
            self.device_combo.current(0)
            self.selected_device.set(labels[0])
        else:
            self.selected_device.set("")
        if not labels:
            self._append_log("未偵測到裝置。請連接裝置並按『重新整理』。\n")

    # Collection control
    def start_collection(self) -> None:
        if self.proc is not None:
            return

        # Validate ADB
        if not which(ADB_EXE):
            messagebox.showerror("錯誤", "找不到 adb，請先安裝並加入 PATH。")
            return

        # Validate device
        sel_label = self.selected_device.get().strip()
        if not sel_label:
            messagebox.showwarning("提示", "請先選擇裝置。")
            return
        # Resolve serial by matching label
        serial = None
        for d in self.devices:
            if d.label == sel_label:
                serial = d.serial
                state = d.state
                break
        if not serial:
            messagebox.showwarning("提示", "所選裝置不存在，請重新整理後再試。")
            return
        if state != "device":
            messagebox.showwarning("提示", f"裝置狀態為 {state}，請確認已授權且連線正常。")
            return

        # Validate directory
        out_dir = Path(self.output_dir.get()).expanduser().resolve()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("錯誤", f"無法建立輸出資料夾: {e}")
            return

        # Validate retention integer
        try:
            retention_int = int(self.retention.get())
            if retention_int <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("提示", "保留小時必須為正整數。")
            return

        # Validate bugreport cooldown if enabled
        bug_enabled = bool(self.bugreport_enabled.get())
        cooldown_int = DEFAULT_BUGREPORT_COOLDOWN
        if bug_enabled:
            try:
                cooldown_int = int(self.bugreport_cooldown.get())
                if cooldown_int < 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("提示", "冷卻(秒)必須為 0 或正整數。")
                return

        prefix = self.prefix.get().strip() or DEFAULT_PREFIX

        # Prepare command
        script = str(Path(__file__).parent / "logcat_rotate.py")
        if not Path(script).exists():
            messagebox.showerror("錯誤", "找不到 logcat_rotate.py，請確認檔案存在於同目錄。")
            return

        cmd = [
            sys.executable,
            script,
            "--dir",
            str(out_dir),
            "--prefix",
            prefix,
            "--retention",
            str(retention_int),
        ]

        # Bugreport flags
        if not bug_enabled:
            cmd.append("--no-bugreport")
        else:
            cmd += ["--bugreport-cooldown", str(cooldown_int)]
            # split keywords by comma/semicolon/newline and pass individually
            raw_kw = self.bugreport_keywords.get()
            if raw_kw:
                import re as _re
                for token in _re.split(r"[;,\n]+", raw_kw):
                    kw = token.strip()
                    if kw:
                        cmd += ["--bugreport-keyword", kw]
            # bugreport directory (absolute or relative to --dir)
            brd = self.bugreport_dir.get().strip() or DEFAULT_BUGREPORT_DIR
            cmd += ["--bugreport-dir", brd]

        env = os.environ.copy()
        env["ANDROID_SERIAL"] = serial

        self._append_log(
            f"啟動收集: 裝置={serial}, 目錄={out_dir}, 前綴={prefix}, 保留={retention_int}h, bugreport={'開' if bug_enabled else '關'}, 冷卻={cooldown_int}s, bug目錄={self.bugreport_dir.get().strip() or DEFAULT_BUGREPORT_DIR}\n"
        )

        try:
            # On Windows, avoid opening console window when packaged
            creationflags = 0
            startupinfo = None
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]

            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except Exception as e:
            self._append_log(f"啟動失敗: {e}\n")
            self.proc = None
            return

        # Reader thread
        self._stop_reader.clear()
        self.reader_thread = threading.Thread(target=self._reader_worker, daemon=True)
        self.reader_thread.start()

        # Toggle buttons
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)

    def stop_collection(self) -> None:
        if self.proc is None:
            return
        self._append_log("正在停止收集…\n")
        try:
            if os.name == "nt":
                self.proc.terminate()  # type: ignore[union-attr]
            else:
                self.proc.send_signal(signal.SIGINT)  # type: ignore[union-attr]
        except Exception:
            try:
                self.proc.kill()  # type: ignore[union-attr]
            except Exception:
                pass

        self._stop_reader.set()
        self.proc = None
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

    # Background workers
    def _reader_worker(self) -> None:
        assert self.proc is not None
        proc = self.proc
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if self._stop_reader.is_set():
                    break
                self.output_queue.put(line)
        except Exception:
            pass
        finally:
            self.output_queue.put("\n[子程序結束]\n")

    def _drain_output_queue(self) -> None:
        try:
            while True:
                line = self.output_queue.get_nowait()
                self._append_log(line)
        except queue.Empty:
            pass
        # Re-schedule
        self.after(120, self._drain_output_queue)

    # Log helper
    def _append_log(self, text: str) -> None:
        self.text.insert(tk.END, text)
        self.text.see(tk.END)


def main() -> int:
    if not which(ADB_EXE):
        # Show a minimal message before UI in case of headless
        print("找不到 adb，請先安裝 Android Platform Tools 並將 adb 加入 PATH。", file=sys.stderr)
    app = LogCollectorUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
