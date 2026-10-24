$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Find-Python {
    if ($env:WEBAGENT_PYTHON) { return $env:WEBAGENT_PYTHON }
    foreach ($cand in @("python", "py")) {
        try {
            & $cand -c "import PIL" 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $cand }
        } catch { }
    }
    $found = Get-ChildItem "$env:USERPROFILE\.dsh-beta\dsh-runtimes" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "dependencies\python\python.exe" } |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($found) { return $found }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host ""
    Write-Host "  找不到可用的 Python（需要已安装 Pillow）。" -ForegroundColor Red
    Write-Host "  1) 安装 Python: https://www.python.org/downloads/  安装时勾选 Add to PATH"
    Write-Host "  2) 再执行:    pip install pillow"
    Write-Host ""
    Read-Host "按回车退出"
    exit 1
}

function Wait-Menu { Read-Host "`n按回车返回菜单" | Out-Null }

while ($true) {
    Write-Host ""
    Write-Host "================================================" -ForegroundColor Cyan
    Write-Host "              webagent 一键菜单" -ForegroundColor Cyan
    Write-Host "================================================" -ForegroundColor Cyan
    Write-Host "  1. 环境自检（第一次使用先跑这个）"
    Write-Host "  2. 填写 / 更换 API key"
    Write-Host "  3. 打开示例网页（练习用）"
    Write-Host "  4. 查看窗口标题"
    Write-Host "  5. 试运行：只规划，不点击"
    Write-Host "  6. 正式运行：会真的点击"
    Write-Host "  7. 持续运行（F8 暂停，Esc 退出）"
    Write-Host "  8. 截图 + 识别预览"
    Write-Host "  0. 退出"
    Write-Host "================================================" -ForegroundColor Cyan

    $choice = Read-Host "请输入序号后回车"

    switch ($choice) {
        "1" { & $py -m webagent doctor; Wait-Menu }
        "2" { & $py -m webagent key; Wait-Menu }
        "3" { & $py -m webagent demo; Wait-Menu }
        "4" { & $py -m webagent windows; Wait-Menu }
        "5" { & $py -m webagent run --dry-run --force; Wait-Menu }
        "6" { & $py -m webagent run --live --force; Wait-Menu }
        "7" { & $py -m webagent watch --live; Wait-Menu }
        "8" { & $py -m webagent snapshot --lines; Wait-Menu }
        "0" { exit 0 }
        default { Write-Host "没有这个序号，请重新输入。" -ForegroundColor Yellow }
    }
}