import argparse
import csv
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
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


def build_logcat_cmd_from_now():
    base = ["adb", "logcat", "-v", "threadtime"]
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
            return base + ["-T", ts]
    except Exception:
        pass
    return base + ["-T", "1"]


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
    enable_bugreport = not getattr(args, "no_bugreport", False)
    bug_dir = out_dir / "bugreports"
    bug_dir.mkdir(parents=True, exist_ok=True)
    last_bugreport_time = 0.0
    bugreport_lock = threading.Lock()
    bugreport_running = {"flag": False}

    # Original generic BT issue detector (additive; not changing other logic)
    def should_trigger_bt_issue(rec: dict, raw_line: str) -> bool:
        if not enable_bugreport:
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

    def trigger_bugreport_async():
        nonlocal last_bugreport_time
        with bugreport_lock:
            if bugreport_running["flag"]:
                return
            bugreport_running["flag"] = True
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_path = bug_dir / f"bugreport_{ts}.zip"
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

    try:
        proc = subprocess.Popen(
            build_logcat_cmd_from_now(),
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

            # Check previous-minute tail line for onNotify trigger
            try:
                cur_key = _minute_key(rec["timestamp"])
                if prev_min_key is None:
                    prev_min_key = cur_key
                elif cur_key != prev_min_key and prev_rec is not None:
                    if _is_bt_notify(prev_rec, prev_line):
                        now_t = time.time()
                        if enable_bugreport and (now_t - last_bugreport_time >= args.bugreport_cooldown):
                            trigger_bugreport_async()
                    prev_min_key = cur_key
            except Exception:
                pass

            # write to CSV
            rotator.write_row(rec)

            # trigger only on GATT Service Changed indication timeout
            if enable_bugreport:
                try:
                    if _is_gatt_service_changed_timeout(rec, line):
                        now_t = time.time()
                        if now_t - last_bugreport_time >= args.bugreport_cooldown:
                            trigger_bugreport_async()
                except Exception:
                    pass
            
            # Also keep original generic BT error trigger
            if enable_bugreport:
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
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
