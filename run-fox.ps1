# 启动雾栖 @linGotFoxbot
#
#   .\run-fox.ps1              # 正常跑，会真的在 Telegram 里说话
#   .\run-fox.ps1 -DryRun      # 只收不发，用来观察她想说什么
#   .\run-fox.ps1 -Check       # 只检查配置，不连 Telegram
#
# Token 不在这个文件里。第一次跑先设一次环境变量（见下方提示）。

param(
    [switch]$DryRun,
    [switch]$Check
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

# ---- token ----------------------------------------------------------------
# 优先用当前会话里已经设好的；没有就向用户要，且只在这次进程里生效。
if (-not $env:TELEGRAM_BOT_TOKEN) {
    $secure = Read-Host "粘贴 Telegram bot token（不会写进任何文件）" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $env:TELEGRAM_BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}
if (-not $env:TELEGRAM_BOT_TOKEN) {
    Write-Error "没有 token，退出。"
    exit 1
}

# ---- model ----------------------------------------------------------------
# 本地 Magpie 网关；如果已经在环境里配过就不覆盖。
if (-not $env:NERVIPULSA_MODEL) {
    $env:NERVIPULSA_MODEL = "opencode-go/muse-spark-1.3-contributor"
}
if (-not $env:NERVIPULSA_BASE_URL) {
    $env:NERVIPULSA_BASE_URL = "http://127.0.0.1:3425/v1"
}
if (-not $env:NERVIPULSA_PROVIDER) {
    $env:NERVIPULSA_PROVIDER = "magpie"
}

# ---- run ------------------------------------------------------------------
# NB: $args is a PowerShell automatic variable - never assign to it.
$cliArgs = @("-u", "-m", "app.main", "--config", "config.fox.toml")

if ($Check) {
    $cliArgs += "--check"
}
elseif ($DryRun) {
    $cliArgs += "--dry-run"
}

Write-Host ""
Write-Host "启动雾栖：" -ForegroundColor Cyan
Write-Host "  model      $env:NERVIPULSA_MODEL"
Write-Host "  endpoint   $env:NERVIPULSA_BASE_URL"
Write-Host "  模式       $(if ($Check) {'只检查配置'} elseif ($DryRun) {'dry-run（只收不发）'} else {'正常（会真的发消息）'})"
Write-Host "  Ctrl+C 退出" -ForegroundColor DarkGray
Write-Host ""

python @cliArgs
