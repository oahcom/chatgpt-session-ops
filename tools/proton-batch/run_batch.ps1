# Proton batch registration runner (Windows PowerShell 5.1 compatible).
#
# Usage:
#   .\run_batch.ps1 -Command proton  -Count 3
#   .\run_batch.ps1 -Command chatgpt -Limit 3
#   .\run_batch.ps1 -Command all     -Count 3
#   .\run_batch.ps1 -Command status
#   .\run_batch.ps1 -Command probe

[CmdletBinding()]
param(
    [ValidateSet("proton", "chatgpt", "all", "status", "export", "probe", "mailtest", "retry")]
    [string]$Command = "status",

    [int]$Count = 1,
    [int]$Limit = 0,
    [string]$Out = "",
    [string]$Stage = "chatgpt",
    [string]$Config = "",
    [switch]$Verbose
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

# Force UTF-8 so Chinese log lines are not mangled by the OEM code page.
try {
    chcp 65001 > $null
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = "utf-8"
} catch { }

$Python = "python"
if (Test-Path -LiteralPath (Join-Path $Root ".venv\Scripts\python.exe")) {
    $Python = Join-Path $Root ".venv\Scripts\python.exe"
}

if (-not (Test-Path -LiteralPath (Join-Path $Root "config.json"))) {
    Write-Host "[WARN] config.json not found. Copy config.example.json first." -ForegroundColor Yellow
}

$argv = @("-m", "proton_batch", $Command)
if ($Config) { $argv += @("--config", $Config) }
if ($Verbose) { $argv += "--verbose" }

switch ($Command) {
    "proton"  { $argv += @("--count", $Count) }
    "all"     { $argv += @("--count", $Count) }
    "chatgpt" { if ($Limit -gt 0) { $argv += @("--limit", $Limit) } }
    "retry"   { $argv += @("--stage", $Stage); if ($Limit -gt 0) { $argv += @("--limit", $Limit) } }
    "export"  {
        if (-not $Out) { $Out = "data\accounts.csv" }
        $argv += @("--out", $Out)
    }
}

Write-Host ("[INFO] " + $Python + " " + ($argv -join " ")) -ForegroundColor Cyan
& $Python @argv
exit $LASTEXITCODE
