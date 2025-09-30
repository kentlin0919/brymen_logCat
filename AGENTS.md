# Repository Guidelines

## Project Structure & Module Organization
- `logcat_rotate.py` captures Bluetooth-focused Android logs, rotates minute-level CSVs, and prunes expired artifacts.
- Runtime output lands under the directory passed via `--dir`, grouped into five-minute folders with per-minute CSVs plus optional `bugreports/` ZIPs.
- No bundled tests or assets yet; add Python test modules under a future `tests/` directory to keep unit checks isolated from runtime logs.

## Build, Test, and Development Commands
- `python3 logcat_rotate.py --help` — inspect available flags, defaults, and usage patterns.
- `python3 logcat_rotate.py --dir ./logs --prefix bt --retention 36` — run the collector locally; ensure `adb` is on `PATH` and a device is authorized.
- `python3 -m compileall .` — quick syntax verification before pushing changes.

## Coding Style & Naming Conventions
- Stick to PEP 8: four-space indentation, lowercase_with_underscores for functions, and UPPER_SNAKE_CASE for module-wide constants (e.g., `BT_TAGS`).
- Preserve existing type hints and add new ones for public helpers; prefer explicit return annotations.
- Keep log-processing regexes and data class names descriptive; document tricky parsing logic with concise comments when necessary.

## Testing Guidelines
- Introduce unit tests with `pytest` or `unittest` under `tests/`; mirror the module structure (e.g., `tests/test_logcat_rotate.py`).
- Focus on deterministic helpers such as `parse_logcat_line`, rotation boundaries, and cleanup routines using fixture directories.
- Run future suites via `pytest` or `python3 -m unittest discover`; ensure they pass before submitting patches.

## Commit & Pull Request Guidelines
- Use imperative, present-tense commit subjects under 50 characters (e.g., `Add fallback milliseconds for logcat timestamp`).
- Include concise body bullets for context: affected modes, risks, or verification steps.
- Open pull requests with a summary, reproduction or verification notes, and the adb/device setup used; attach log snippets or CSV samples when visual proof helps reviewers.

## Log Capture & Device Tips
- Confirm device time precision upfront: `adb shell 'date "+%Y-%m-%d %H:%M:%S.%3N"'` should yield a full timestamp; if `%N` is unsupported, downgrade to whole milliseconds before running the collector.
- Monitor disk usage of the output directory, especially when long-running; adjust `--retention` or add cron-based pruning if logs accumulate rapidly.


## 回復語言
- 繁體中文


## git
- 使用繁體中文撰寫commit
- 自動git push

