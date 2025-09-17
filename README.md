# Brymen LogCat Rotator

## Overview
`logcat_rotate.py` streams Android `adb logcat` output, mirrors each line to stdout, and persists parsed entries as minute-sliced CSV files. Bluetooth GATT errors are highlighted: when qualifying timeouts or crashes appear, the tool can trigger `adb bugreport` to capture a diagnostic snapshot. A background janitor prunes aged CSVs and bug reports, keeping disk consumption predictable during long test runs.

## Prerequisites
- Python 3.8+ with standard library only; no third-party modules are required.
- Android SDK platform tools (`adb`) available on `PATH`, with a connected and authorized device or emulator.
- Sufficient disk quota for rolling CSVs and optional bugreport ZIPs in the chosen output directory.

## Quick Start
```bash
python3 logcat_rotate.py --dir ./logs --prefix bt --retention 36
```
- `--dir` sets the base folder for generated artifacts (defaults to `./logs`).
- `--prefix` controls the CSV filename prefix, helping segment different capture sessions.
- `--retention` keeps artifacts for the provided number of hours; older CSV/TXT/ZIP files are removed.
- Add `--no-bugreport` to disable automatic bugreport capture, or tweak `--bugreport-cooldown` (seconds) to widen the interval between successive reports.

## Output Layout
- CSV files are organized into five-minute buckets (e.g., `logs/2024-09-17_10-15/bt_2024-09-17_10-16.csv`). Each CSV records timestamp, PID, TID, log level, tag, and message.
- When bugreport capture is enabled, ZIPs land in `logs/bugreports/bugreport_<timestamp>.zip`.
- The tool echoes every line to stdout so existing log pipelines remain compatible.

## Operational Notes
- Device timestamps must include milliseconds. Validate with `adb shell 'date "+%Y-%m-%d %H:%M:%S.%3N"'`; if `%N` is unsupported, the script gracefully falls back to whole milliseconds.
- Graceful shutdown (Ctrl+C) ensures open CSV handles are flushed and `adb logcat` is terminated.
- Long-lived sessions benefit from monitoring free space; adjust `--retention` or rotate logs externally as needed.

## Contributing
See `AGENTS.md` for coding standards, testing expectations, and pull request guidance tailored to this repository.


## git
- 使用繁體中文撰寫commit
- 自動git push