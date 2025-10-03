param(
  [string]$Subject = "新增：bugreport 開關、關鍵字觸發與輸出路徑；UI 支援設定",
  [string]$Body = ""
)

function ExitOnError($msg) {
  Write-Host $msg -ForegroundColor Red
  exit 1
}

# 檢查是否為 git 倉庫
git rev-parse --is-inside-work-tree 1>$null 2>$null
if ($LASTEXITCODE -ne 0) { ExitOnError "此資料夾不是 git 倉庫。" }

# 檢查 git 是否可用
git --version 1>$null 2>$null
if ($LASTEXITCODE -ne 0) { ExitOnError "找不到 git，請先安裝 Git。" }

# Stage 所有變更
git add -A

# 檢查暫存是否有變更
git diff --cached --quiet
$hasStaged = ($LASTEXITCODE -ne 0)

if (-not $hasStaged) {
  Write-Host "沒有變更需要提交。" -ForegroundColor Yellow
} else {
  if ([string]::IsNullOrWhiteSpace($Body)) {
    $Body = @(
      "logcat_rotate.py：",
      "- 新增 app crash 偵測觸發 bugreport（Java 與 native）",
      "- 支援 --bugreport-keyword（可重複、逗號/分號/換行分隔）",
      "- 新增 --bugreport-dir（支援相對/絕對路徑）",
      "- ZIP 檔名加入原因，例如 kw_timeout、crash_sigsegv_com.app",
      "- 保留既有 BT/GATT 條件與冷卻機制",
      "",
      "log_ui.py：",
      "- 新增 bugreport 開關、冷卻(秒)、自訂關鍵字欄位",
      "- 新增 Bugreport 目錄欄位與瀏覽按鈕",
      "- 以 ANDROID_SERIAL 指派裝置，串流輸出至 UI",
      "",
      "驗證：",
      "- python log_ui.py 啟動 UI，設定關鍵字與目錄後開始收集",
      "- 或 CLI：python logcat_rotate.py --dir ./logs --bugreport-dir bugreports_bt --bugreport-keyword ANR,timeout"
    ) -join [Environment]::NewLine
  }

  git commit -m $Subject -m $Body
  if ($LASTEXITCODE -ne 0) { ExitOnError "git commit 失敗。" }
}

# 取得目前分支
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if (-not $branch) { ExitOnError "無法取得分支名稱。" }

# 檢查是否已設定 upstream
git rev-parse --abbrev-ref --symbolic-full-name @{u} 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
  git push -u origin $branch
} else {
  git push
}
if ($LASTEXITCODE -ne 0) { ExitOnError "git push 失敗。" }

Write-Host "提交與推送完成 (分支: $branch)" -ForegroundColor Green
