import argparse
import csv
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path


# logcat -v threadtime pattern
# Format: MM-DD HH:MM:SS.mmm PID TID LEVEL TAG: message
LINE_RE = re.compile(
    r"^(?P<md>\d{2}-\d{2})\s+"
    r"(?P<hms>\d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<pid>\d+)\s+"
    r"(?P<tid>\d+)\s+"
    r"(?P<level>[VDIWEF])\s+"
    r"(?P<tag>[^:]+):\s+"
    r"(?P<msg>.*)$"
)

# Bluetooth tags and keywords (kept minimal and additive use only)
BT_TAGS = {
    # Narrowed to GATT-specific tags to reduce noise
    "BtGatt",
    "BtGatt.GattService",
}

BT_KEYWORDS = [
    # Narrowed to GATT timeout keywords
    "gatt_indication_confirmation_timeout",
    "service changed notification timed out",
    "service changed",
    "gatt timeout",
]

GATT_CONGESTION_STATUS_RE = re.compile(r"status\s*[:=]\s*(142|0x8e)", re.IGNORECASE)


LOGCAT_SINCE_SUPPORT: bool | None = None


def _ensure_logcat_since_support() -> bool:
    global LOGCAT_SINCE_SUPPORT
    if LOGCAT_SINCE_SUPPORT is not None:
        return LOGCAT_SINCE_SUPPORT
    try:
        result = subprocess.run(
            ["adb", "logcat", "-v", "threadtime", "-T", "1", "-d"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        lowered = combined.lower()
        if "invalid option" in lowered or "unrecognized option" in lowered:
            LOGCAT_SINCE_SUPPORT = False
            return LOGCAT_SINCE_SUPPORT
        LOGCAT_SINCE_SUPPORT = True
        return LOGCAT_SINCE_SUPPORT
    except subprocess.TimeoutExpired:
        LOGCAT_SINCE_SUPPORT = True
    except Exception:
        LOGCAT_SINCE_SUPPORT = True
    return LOGCAT_SINCE_SUPPORT


class BugreportController:
    def __init__(self, initial: bool):
        self._enabled = initial
        self._lock = threading.Lock()

    def set_enabled(self, value: bool) -> None:
        with self._lock:
            self._enabled = value

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled


class BugreportCLIControl(threading.Thread):
    def __init__(self, controller: BugreportController):
        super().__init__(daemon=True)
        self.controller = controller

    def run(self) -> None:
        sys.stderr.write("[bugreport-ui] Commands: on/off/toggle/status/quit\n")
        sys.stderr.flush()
        while True:
            try:
                sys.stderr.write("[bugreport-ui] > ")
                sys.stderr.flush()
                cmd = sys.stdin.readline()
            except Exception:
                break
            if not cmd:
                break
            cmd = cmd.strip().lower()
            if not cmd:
                continue
            if cmd in {"on", "enable"}:
                self.controller.set_enabled(True)
                sys.stderr.write("[bugreport-ui] bugreport enabled\n")
            elif cmd in {"off", "disable"}:
                self.controller.set_enabled(False)
                sys.stderr.write("[bugreport-ui] bugreport disabled\n")
            elif cmd in {"toggle", "switch"}:
                current = self.controller.is_enabled()
                self.controller.set_enabled(not current)
                state = "enabled" if not current else "disabled"
                sys.stderr.write(f"[bugreport-ui] bugreport {state}\n")
            elif cmd in {"status", "state"}:
                state = "enabled" if self.controller.is_enabled() else "disabled"
                sys.stderr.write(f"[bugreport-ui] bugreport currently {state}\n")
            elif cmd in {"quit", "exit"}:
                sys.stderr.write("[bugreport-ui] closing control interface\n")
                sys.stderr.flush()
                break
            else:
                sys.stderr.write("[bugreport-ui] unknown command\n")
            sys.stderr.flush()


def parse_logcat_line(line: str):
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    now = datetime.now()
    year = now.year
    md = m.group("md")
    hms = m.group("hms")
    ts_str = f"{year}-{md} {hms}"
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None
    return {
        "timestamp": ts,
        "pid": m.group("pid"),
        "tid": m.group("tid"),
        "level": m.group("level"),
        "tag": m.group("tag"),
        "message": m.group("msg"),
    }


class CSVRotator:
    def __init__(self, out_dir: Path, prefix: str):
        self.out_dir = out_dir
        self.prefix = prefix
        self.current_minute_key = None
        self.current_bucket_key = None
        self.file = None
        self.writer = None
        self.header = ["timestamp", "pid", "tid", "level", "tag", "message"]
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _minute_key(self, dt: datetime):
        return dt.strftime("%Y-%m-%d_%H-%M")

    def _bucket_key(self, dt: datetime):
        # group into 5-minute folders
        bucket_min = (dt.minute // 5) * 5
        return dt.replace(minute=bucket_min, second=0, microsecond=0).strftime("%Y-%m-%d_%H-%M")

    def _file_path_for(self, dt: datetime) -> Path:
        bucket = self._bucket_key(dt)
        minute = self._minute_key(dt)
        folder = self.out_dir / bucket
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{self.prefix}_{minute}.csv"

    def _open_for_dt(self, dt: datetime):
        min_key = self._minute_key(dt)
        bucket_key = self._bucket_key(dt)
        if (
            min_key == self.current_minute_key
            and bucket_key == self.current_bucket_key
            and self.file
            and not self.file.closed
        ):
            return
        self.close()
        path = self._file_path_for(dt)
        is_new = not path.exists()
        self.file = path.open("a", encoding="utf-8", newline="")
        self.writer = csv.writer(self.file)
        if is_new:
            self.writer.writerow(self.header)
        self.current_minute_key = min_key
        self.current_bucket_key = bucket_key

    def write_row(self, row: dict):
        ts = row["timestamp"]
        self._open_for_dt(ts)
        self.writer.writerow([
            ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            row["pid"],
            row["tid"],
            row["level"],
            row["tag"],
            row["message"],
        ])

    def close(self):
        if self.file and not self.file.closed:
            try:
                self.file.flush()
            except Exception:
                pass
            try:
                self.file.close()
            except Exception:
                pass
        self.file = None
        self.writer = None
        self.current_minute_key = None
        self.current_bucket_key = None


def try_delete_if_old(path: Path, cutoff_epoch: float):
    try:
        if path.stat().st_mtime < cutoff_epoch:
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def cleanup_old_logs(directory: Path, retention_hours: int, prefix: str):
    now = time.time()
    cutoff = now - retention_hours * 3600
    for root, dirs, files in os.walk(directory, topdown=False):
        root_path = Path(root)
        for name in files:
            p = root_path / name
            if p.suffix.lower() in {".csv", ".txt"}:
                try_delete_if_old(p, cutoff)
            # Also clean up bugreport zips older than retention (e.g., 36h)
            if p.suffix.lower() == ".zip" and "bugreport" in p.name.lower():
                try_delete_if_old(p, cutoff)
        try:
            if not any(Path(root).iterdir()):
                Path(root).rmdir()
        except Exception:
            pass


def cleanup_old_logs_loop(stop_event: threading.Event, directory: Path, retention_hours: int, prefix: str, interval_sec: int = 300):
    while not stop_event.wait(timeout=interval_sec):
        try:
            cleanup_old_logs(directory, retention_hours, prefix)
        except Exception:
            pass


def build_logcat_cmd_from_now() -> tuple[list[str], datetime | None]:
    base = ["adb", "logcat", "-v", "threadtime"]
    if not _ensure_logcat_since_support():
        print("[warn] 目標裝置 logcat 不支援 -T 參數，將從目前時間開始過濾舊紀錄", file=sys.stderr)
        return base, datetime.now() - timedelta(seconds=1)
    try:
        ts = subprocess.check_output(
            ["adb", "shell", 'date "+%Y-%m-%d %H:%M:%S.%3N"'],
            encoding="utf-8",
            errors="replace",
        ).strip()
        if ts:
            ts = ts.replace("\r", "")
            if "%3N" in ts or "%N" in ts:
                ts = ts.replace("%3N", "000").replace("%N", "000000000")
            return base + ["-T", ts], None
    except Exception:
        pass
    return base + ["-T", "1"], None


def run(args):
    out_dir = Path(args.dir)
    rotator = CSVRotator(out_dir, args.prefix)

    stop_event = threading.Event()
    cleaner = threading.Thread(
        target=cleanup_old_logs_loop,
        args=(stop_event, out_dir, args.retention, args.prefix, 300),
        daemon=True,
    )
    cleaner.start()

    # bugreport trigger control
    bugreport_controller = BugreportController(not getattr(args, "no_bugreport", False))
    bugreport_ui_thread = None
    if getattr(args, "bugreport_ui", False):
        bugreport_ui_thread = BugreportCLIControl(bugreport_controller)
        bugreport_ui_thread.start()
    # bugreport output directory: absolute path as-is; relative to out_dir otherwise
    try:
        br_dir_arg = getattr(args, "bugreport_dir", "bugreports") or "bugreports"
    except Exception:
        br_dir_arg = "bugreports"
    bug_dir = Path(br_dir_arg)
    if not bug_dir.is_absolute():
        bug_dir = out_dir / bug_dir
    bug_dir.mkdir(parents=True, exist_ok=True)
    last_bugreport_time = 0.0
    bugreport_lock = threading.Lock()
    bugreport_running = {"flag": False}

    # Configure custom bugreport keywords
    custom_keywords = []
    try:
        raw_list = getattr(args, "bugreport_keyword", []) or []
        for item in raw_list:
            if not item:
                continue
            # Support comma/semicolon separated input
            parts = re.split(r"[;,\n]+", str(item))
            for p in parts:
                kw = p.strip().lower()
                if kw:
                    custom_keywords.append(kw)
    except Exception:
        custom_keywords = []

    # Original generic BT issue detector (additive; not changing other logic)
    def should_trigger_bt_issue(rec: dict, raw_line: str) -> bool:
        if not bugreport_controller.is_enabled():
            return False
        lvl = (rec.get("level") or "").upper()
        tag = (rec.get("tag") or "").strip()
        msg = (rec.get("message") or "").lower()
        tag_l = tag.lower()
        if tag in BT_TAGS or "bluetooth" in tag_l or tag_l.startswith("bt"):
            if lvl in {"E", "F"}:
                return True
        if any(k in msg for k in BT_KEYWORDS):
            if lvl in {"E", "F"} or "anr" in msg or "crash" in msg:
                return True
        raw = (raw_line or "").lower()
        if ("bluetooth" in raw or raw.startswith("bt")) and ("crash" in raw or "fatal" in raw or "assert" in raw):
            return True
        return False

    # Original minute-rollover onNotify detector
    def _minute_key(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d_%H-%M")

    def _is_bt_notify(rec: dict, raw_line: str) -> bool:
        try:
            tag = (rec.get("tag") or "").lower()
            msg = (rec.get("message") or "").lower()
            if ("gattservice" in tag or "btgatt" in tag or tag == "btgatt.gattservice") and "onnotify" in msg:
                return True
            raw = (raw_line or "").lower()
            if "gattservice" in raw and "onnotify" in raw:
                return True
        except Exception:
            pass
        return False

    prev_min_key = None
    prev_line = None
    prev_rec = None

    def _is_gatt_service_changed_timeout(rec: dict, raw_line: str) -> bool:
        try:
            msg = (rec.get("message") or "").lower()
            raw = (raw_line or "").lower()
            # Robust match for the target error
            if "gatt_indication_confirmation_timeout" in msg or "gatt_indication_confirmation_timeout" in raw:
                return True
            if "service changed notification timed out" in msg or "service changed notification timed out" in raw:
                return True
            if "gatt_utils.cc" in raw and "timed out" in raw and "service changed" in raw:
                return True
        except Exception:
            pass
        return False

    def _is_gatt_congestion_event(rec: dict, raw_line: str) -> bool:
        try:
            msg = (rec.get("message") or "").lower()
            raw = (raw_line or "").lower()
            if "bta_gattc_cmpl_cback" in raw or "bta_gattc_cmpl_cback" in msg:
                if GATT_CONGESTION_STATUS_RE.search(raw) or GATT_CONGESTION_STATUS_RE.search(msg):
                    return True
                if "gatt_congested" in raw or "gatt_congested" in msg:
                    return True
                if "gatt_busy" in raw or "gatt_busy" in msg:
                    return True
        except Exception:
            pass
        return False

    def _sanitize_reason(reason: str) -> str:
        # keep simple, filename-safe tokens
        safe = []
        for ch in reason.lower():
            if ch.isalnum():
                safe.append(ch)
            elif ch in {'.', '-', '_'}:
                safe.append(ch)
            else:
                safe.append('-')
        # collapse repeats
        out = re.sub(r"-+", "-", ''.join(safe)).strip('-')
        return out[:64] if out else "reason"

    def trigger_bugreport_async(reason: str | None = None):
        nonlocal last_bugreport_time
        with bugreport_lock:
            if bugreport_running["flag"]:
                return
            bugreport_running["flag"] = True
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        suffix = f"_{_sanitize_reason(reason)}" if reason else ""
        out_path = bug_dir / f"bugreport_{ts}{suffix}.zip"
        print(f"[info] trigger bugreport -> {out_path}", file=sys.stderr)

        def _run():
            try:
                creation = 0
                if os.name == "nt":
                    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                with open(out_path, "wb") as f:
                    p = subprocess.Popen(
                        ["adb", "bugreport"],
                        stdout=f,
                        stderr=subprocess.STDOUT,
                        creationflags=creation,
                    )
                    p.wait()
                print(f"[info] bugreport done: {out_path}", file=sys.stderr)
                last_bugreport_time = time.time()
            except Exception as e:
                print(f"[warn] bugreport failed: {e}", file=sys.stderr)
            finally:
                bugreport_running["flag"] = False

    # --- Crash detection helpers ---
    FATAL_RE = re.compile(r"FATAL EXCEPTION:\s*([^\s]+)")
    PROCESS_RE = re.compile(r"Process:\s*([^,\s]+)")
    FATAL_SIGNAL_RE = re.compile(r"Fatal signal\s+\d+\s+\(([^)]+)\)")
    NATIVE_PKG_TAIL_RE = re.compile(r"\(([^)]+)\)\s*$")

    def _extract_crash_reason(rec: dict, raw_line: str, prev_rec: dict | None, prev_line: str | None) -> str | None:
        try:
            tag = (rec.get("tag") or "").strip()
            msg = rec.get("message") or ""
            # Java crash header
            if tag == "AndroidRuntime" and "FATAL EXCEPTION" in msg:
                m = FATAL_RE.search(msg)
                thread = m.group(1) if m else "unknown"
                return f"crash_fatal_exception_{thread}"
            # Java crash "Process:" line following header
            if tag == "AndroidRuntime" and msg.startswith("Process:") and prev_line:
                if prev_rec and prev_rec.get("tag") == "AndroidRuntime" and "FATAL EXCEPTION" in (prev_rec.get("message") or ""):
                    mthread = FATAL_RE.search(prev_rec.get("message") or "")
                    thread = mthread.group(1) if mthread else "unknown"
                    mpkg = PROCESS_RE.search(msg)
                    pkg = mpkg.group(1) if mpkg else "app"
                    return f"crash_fatal_exception_{thread}_{pkg}"
            # Native crash
            if tag == "libc" and "Fatal signal" in msg:
                msig = FATAL_SIGNAL_RE.search(msg)
                sig = msig.group(1) if msig else "signal"
                # try to capture package at end: ... pid 1234 (com.app)
                mpkg = NATIVE_PKG_TAIL_RE.search(msg)
                pkg = mpkg.group(1) if mpkg else "app"
                return f"crash_{sig}_{pkg}"
        except Exception:
            pass
        return None

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def handle_exit(signum, frame):
        stop_event.set()
        rotator.close()
        try:
            if proc and proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        time.sleep(0.1)
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, handle_exit)
        signal.signal(signal.SIGTERM, handle_exit)
    except Exception:
        pass

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    logcat_cmd, skip_before_ts = build_logcat_cmd_from_now()

    try:
        proc = subprocess.Popen(
            logcat_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
    except FileNotFoundError:
        print("找不到 adb，請安裝並確認 PATH", file=sys.stderr)
        handle_exit(None, None)
        return

    try:
        for line in proc.stdout:
            # always mirror to stdout
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except Exception:
                pass

            rec = parse_logcat_line(line)
            if rec is None:
                rec = {
                    "timestamp": datetime.now(),
                    "pid": "",
                    "tid": "",
                    "level": "",
                    "tag": "",
                    "message": line.strip(),
                }

            if skip_before_ts and rec["timestamp"] < skip_before_ts:
                continue

            bug_enabled = bugreport_controller.is_enabled()

            # Check previous-minute tail line for onNotify trigger
            try:
                cur_key = _minute_key(rec["timestamp"])
                if prev_min_key is None:
                    prev_min_key = cur_key
                elif cur_key != prev_min_key and prev_rec is not None:
                    if _is_bt_notify(prev_rec, prev_line):
                        now_t = time.time()
                        if bug_enabled and (now_t - last_bugreport_time >= args.bugreport_cooldown):
                            trigger_bugreport_async()
                    prev_min_key = cur_key
            except Exception:
                pass

            # write to CSV
            rotator.write_row(rec)

            # trigger only on GATT Service Changed indication timeout
            if bug_enabled:
                try:
                    if _is_gatt_service_changed_timeout(rec, line):
                        now_t = time.time()
                        if now_t - last_bugreport_time >= args.bugreport_cooldown:
                            trigger_bugreport_async()
                except Exception:
                    pass

            # Trigger when bta_gattc callback reports congestion/busy status
            if bug_enabled:
                try:
                    if _is_gatt_congestion_event(rec, line):
                        now_t = time.time()
                        if now_t - last_bugreport_time >= args.bugreport_cooldown:
                            trigger_bugreport_async()
                except Exception:
                    pass
            
            # App crash trigger (Java or native). Include reason in filename
            if bug_enabled:
                try:
                    reason = _extract_crash_reason(rec, line, prev_rec, prev_line)
                    if reason:
                        now_t = time.time()
                        if now_t - last_bugreport_time >= args.bugreport_cooldown:
                            trigger_bugreport_async(reason)
                except Exception:
                    pass

            # Custom keyword trigger
            if bug_enabled and custom_keywords:
                try:
                    low = (line or "").lower()
                    hit = next((k for k in custom_keywords if k in low), None)
                    if hit is not None:
                        now_t = time.time()
                        if now_t - last_bugreport_time >= args.bugreport_cooldown:
                            trigger_bugreport_async(f"kw_{hit}")
                except Exception:
                    pass

            # Also keep original generic BT error trigger
            if bug_enabled:
                try:
                    if should_trigger_bt_issue(rec, line):
                        now_t = time.time()
                        if now_t - last_bugreport_time >= args.bugreport_cooldown:
                            trigger_bugreport_async()
                except Exception:
                    pass

            # Update previous line tracking
            prev_line = line
            prev_rec = rec
    except KeyboardInterrupt:
        pass
    finally:
        handle_exit(None, None)


def main():
    parser = argparse.ArgumentParser(description="Capture logcat to CSV and rotate; optional bugreport trigger")
    parser.add_argument("--dir", default="logs", help="Output directory (default: logs)")
    parser.add_argument("--prefix", default="logcat", help="File prefix (default: logcat)")
    parser.add_argument("--retention", type=int, default=36, help="Retention hours for logs (default: 36)")
    parser.add_argument("--no-bugreport", action="store_true", help="Disable automatic bugreport trigger")
    parser.add_argument("--bugreport-cooldown", type=int, default=900, help="Cooldown seconds between bugreports (default: 900)")
    parser.add_argument(
        "--bugreport-dir",
        default="bugreports",
        help="Bugreport output directory (absolute or relative to --dir; default: bugreports)",
    )
    parser.add_argument(
        "--bugreport-keyword",
        action="append",
        help="Keyword(s) that trigger a bugreport when seen (repeatable, supports comma-separated)",
        default=[],
    )
    parser.add_argument(
        "--bugreport-ui",
        action="store_true",
        help="Launch simple CLI UI to control bugreport toggling at runtime",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
