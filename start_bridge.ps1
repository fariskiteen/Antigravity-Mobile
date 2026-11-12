$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$Host.UI.RawUI.WindowTitle = "Antigravity 手机联动 Bridge (18765)"

$bridgePy = Join-Path $PSScriptRoot "antigravity_bridge.py"
if (-not (Test-Path -LiteralPath $bridgePy)) {
    Write-Host "错误: 未找到桥接脚本: $bridgePy" -ForegroundColor Red
    pause
    exit 1
}

# 自动优先查找 PATH 中的 python，若无则探测常见安装目录
$pyExe = $null
$cmd = Get-Command python.exe -ErrorAction SilentlyContinue
if ($cmd) {
    $pyExe = $cmd.Source
} else {
    $probePaths = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
        "C:\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($p in $probePaths) {
        if (Test-Path -LiteralPath $p) {
            $pyExe = $p
            break
        }
    }
}

if (-not $pyExe) {
    Write-Host "错误: 电脑上未找到 Python 3 环境！" -ForegroundColor Red
    Write-Host "请先安装 Python 3（安装时请勾选 'Add python.exe to PATH'）" -ForegroundColor Yellow
    pause
    exit 1
}

# 幂等清理端口 18765 上的旧实例，避免重复启动报端口占用冲突
$existing = Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    $pids = $existing | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -gt 0 }
    foreach ($procId in $pids) {
        Write-Host "检测到端口 18765 已有运行中的旧实例 (PID $procId)，正在关闭并重启..." -ForegroundColor Yellow
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 400
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Antigravity 手机联动 Bridge 启动中..." -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "• 提示: 保持此窗口打开即可保持手机联动；" -ForegroundColor DarkGray
Write-Host "• 提示: 若要开机静默常驻，请双击运行 install_autostart.bat。" -ForegroundColor DarkGray
Write-Host ""

& $pyExe -X utf8 $bridgePy
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Host "Bridge 异常退出，退出码: $exitCode" -ForegroundColor Red
    pause
}
exit $exitCode
