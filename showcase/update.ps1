# 从 GitHub 更新 vidcut 镜像：有新提交才重新构建（不需要安装 git）。
#   powershell -ExecutionPolicy Bypass -File update.ps1          检查并更新
#   powershell -ExecutionPolicy Bypass -File update.ps1 -Force   无论如何都重新构建
# 每天凌晨 3 点自动运行（在 update.ps1 所在目录执行一次）：
#   schtasks /Create /SC DAILY /ST 03:00 /TN vidcut-update /TR "powershell -ExecutionPolicy Bypass -File `"$PWD\update.ps1`""
param(
    [switch]$Force,
    [string]$Repo = "bil022/vidcut",
    [string]$Branch = "main",
    [string]$Image = "vidcut"
)
$ErrorActionPreference = "Stop"
$log = Join-Path $PSScriptRoot "update.log"
function Say($msg) { $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"; Write-Host $line; Add-Content -Path $log -Value $line -Encoding UTF8 }

try {
    $latest = (Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/commits/$Branch" -Headers @{ "User-Agent" = "vidcut-update" }).sha
} catch {
    Say "无法从 GitHub 获取最新版本：$_"
    exit 1
}
$current = ""
try { $current = (docker image inspect -f '{{ index .Config.Labels "vidcut.rev" }}' $Image 2>$null) } catch {}

if (($latest -eq $current) -and -not $Force) {
    Say "已是最新（$($latest.Substring(0, 7))）"
    exit 0
}
$from = if ($current) { $current } else { "（未安装）" }
Say "更新 $from → $latest"
# 下载这个提交的源码包，解压到临时目录再构建（旧版 Docker 不支持直接从 git 子目录构建）
$tmp = Join-Path ([IO.Path]::GetTempPath()) "vidcut-$($latest.Substring(0, 7))"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    $zip = Join-Path $tmp "src.zip"
    Invoke-WebRequest -Uri "https://codeload.github.com/$Repo/zip/$latest" -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $tmp
    $src = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
    docker build --build-arg "VIDCUT_REV=$latest" -t $Image (Join-Path $src.FullName "showcase")
    if ($LASTEXITCODE -ne 0) { Say "构建失败"; exit 1 }
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
Say "完成。正在运行的容器要删除后重新创建才会用新版本（工作目录和缓存卷里的数据不受影响）。"
