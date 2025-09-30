param(
  [string]$Subject = "新增 tkinter UI：裝置選擇與收集控制"
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
  $Body = @(
    "新增 `log_ui.py` 介面（選擇 adb 裝置、輸出資料夾、前綴與保留小時）",
    "以 ANDROID_SERIAL 指派裝置，無需改動既有 `logcat_rotate.py`",
    "子程序串流 stdout/stderr 至 UI，提供開始/停止控制",
    "驗證：python log_ui.py 啟動 UI、選裝置後開始收集"
  ) -join [Environment]::NewLine

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

