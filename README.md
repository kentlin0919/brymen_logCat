# logcat_rotate

以藍牙（Bluetooth）相關訊息為重點，從 Android `logcat` 擷取並持續寫入 CSV，按分鐘輪轉檔案，並依保留期自動清除舊資料。輸出以每 5 分鐘為一個目錄群組，內含逐分鐘的 CSV；可選擇輸出 `bugreports/` 壓縮檔以利問題回報。若加上 `--no-bugreport`，將完全停用 bugreport 匯出。

## 功能特色
- 針對藍牙相關 Tag 過濾：聚焦 BT 訊息（例如模組內的 `BT_TAGS`）。
- 分鐘級 CSV 輪轉：每分鐘自動產出一個 CSV，避免單檔過大。
- 五分鐘目錄分組：輸出路徑下以 5 分鐘為單位建立資料夾便於整理與查找。
- 保留期清理：依設定的保留期自動刪除過期的目錄/檔案，控制磁碟用量。
- 可選 `bugreports/`：在需要時輸出裝置 bugreport ZIP 以輔助除錯（若腳本開啟該模式）。

## 需求
- Python 3（建議 3.8+）
- 已安裝並可從 `PATH` 呼叫的 `adb`
- 至少一台已授權的 Android 裝置（`adb devices` 可見且為 authorized）

## 快速開始
1. 檢視可用參數與預設值：
   ```bash
   python3 logcat_rotate.py --help
   ```
2. 本機執行並輸出到 `./logs`，檔名前綴為 `bt`，保留期 36（單位視程式而定，常見為小時）：
   ```bash
   python3 logcat_rotate.py --dir ./logs --prefix bt --retention 36
   # 停用 bugreport 匯出：
   python3 logcat_rotate.py --dir ./logs --prefix bt --retention 36 --no-bugreport
   ```
3. 執行前請確認：
   - `adb` 可連線且裝置時間精度符合需求（見下方「裝置與時間精度」）。
   - 目錄可寫入，並監控磁碟空間（長時間執行時尤需留意）。

## 輸出結構
- 根輸出目錄：由 `--dir` 指定，例如 `./logs`
- 五分鐘群組資料夾：例如 `2024-05-01_13-25/`（13:25–13:29 之間的分鐘 CSV）
- 分鐘級 CSV：每分鐘一檔，檔名通常包含前綴與分鐘時間戳（如 `bt_20240501_1325.csv`）
- 可選 `bugreports/`：當啟用 bugreport 匯出時，會在群組資料夾下建立 ZIP 檔

> CSV 欄位以腳本實作為準；通常會包含時間戳、層級、Tag 與訊息等。每一列對應一行 `logcat` 訊息（經過 `parse_logcat_line` 類的解析）。

## 常用參數
- `--dir`：輸出根目錄
- `--prefix`：輸出檔名前綴（例如 `bt`）
- `--retention`：保留期（單位依腳本實作，常見為「小時」）；超過者自動清除
- `--no-bugreport`：禁止輸出 bugreport（即使預設情境會產生 bugreport，也強制停用）
- 其餘旗標與細節請執行：
  ```bash
  python3 logcat_rotate.py --help
  ```

## 裝置與時間精度
- 先確認裝置時間可提供毫秒精度：
  ```bash
  adb shell 'date "+%Y-%m-%d %H:%M:%S.%3N"'
  ```
  若 `%N` 不支援，請在執行收集器前降級為以整數毫秒處理，避免解析錯誤或時間對不齊。
- 長時間執行時請監控輸出目錄的磁碟用量；必要時提高 `--retention` 清理頻率、或以排程搭配額外清理。

## 開發與測試
- 程式風格：遵循 PEP 8；模組常數使用 UPPER_SNAKE_CASE（如 `BT_TAGS`），函式命名使用 `lowercase_with_underscores`；保留並補齊型別註解。
- 語法快查：
  ```bash
  python3 -m compileall .
  ```
- 單元測試：建議以 `pytest` 或 `unittest` 放在 `tests/` 目錄（例如 `tests/test_logcat_rotate.py`）。
  - 優先針對可決定性的工具函式與流程測：`parse_logcat_line`、輪轉邊界、保留期清理等；
  - 執行方式：
    ```bash
    pytest
    # 或
    python3 -m unittest discover
    ```

## 疑難排解
- `adb` 連線問題：
  - 確認 `adb devices` 可看到裝置且為 `device`（非 `unauthorized`/`offline`）。
  - 重新插拔資料線、開啟 USB 偵錯、確認授權對話框。
- CSV 未產出或無 BT 訊息：
  - 檢查當前過濾的 Tag 是否覆蓋到預期模組。
  - 放寬過濾或暫時全量收集以定位來源。
- 磁碟快速膨脹：
  - 降低 `--retention` 或加上排程清理；縮小日志等級或過濾範圍。

## 提交與貢獻
- Commit 標題（繁體中文、祈使句，50 字以內）：
  - 例如：`加入 logcat 時間戳毫秒降級處理`
- 內文簡述影響面、風險與驗證步驟；必要時附上日誌片段或 CSV 範例。
- 開 PR 時請描述：摘要、重現/驗證方式、使用的 adb/裝置設定；若有視覺證據（螢幕擷圖、CSV 範例）更佳。
- 自動提交
- Commit：簡短、命令式、標明範疇（如：`fix(chart): clamp pan at edges`）
- 語言 ： 請以繁體中文書寫
- 請將相關變更分組，避免不相關的重構
- PR 必須包含：
  - 變更摘要與原因
  - UI 變更請附截圖/GIF
  - 手動測試步驟與影響模組/路徑
  - 若適用請附上相關 issue 或任務 ID
- 分支命名：`feature/<name>`、`fix/<name>`、`chore/<name>`
- 每個 PR 需至少一位審核者
- 請保持 PR 規模小且聚焦（盡量少於 300 行）
- 如果沒有CHANGELOG.md 就建立
- 將變更進行填寫到CHANGELOG.md

---
若你需要我擴充參數說明、補上範例 CSV 欄位、或加入 GitHub Actions/排程腳本，告訴我你的需求與環境即可！


## README 書寫條件
- 要有詳細的版本資訊
- 詳細的檔案結構
- 針對每個功能畫出時序圖
- 針對每個系統的環境安裝的詳細流程以及指令