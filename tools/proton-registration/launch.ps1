[CmdletBinding()]
param(
    [ValidateSet("chrome", "edge")]
    [string]$Browser = "chrome",

    [string]$BrowsePath = "$env:USERPROFILE\.local\bin\browse.exe"
)

$ErrorActionPreference = "Stop"

$RegistrationUrl = "https://account.proton.me/mail/signup"

$BrowserId = switch ($Browser) {
    "chrome" { "chrome-this-computer" }
    "edge"   { "edge-this-computer" }
}

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Write-Failure {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

if (-not (Test-Path -LiteralPath $BrowsePath -PathType Leaf)) {
    Write-Failure "browse executable not found: $BrowsePath"
    exit 2
}

Write-Info "Proton Mail registration assistant"
Write-Info "Browser: $Browser"
Write-Info "Browser ID: $BrowserId"
Write-Info "Opening registration page..."

try {
    # browse CLI 0.1.5 + Windows PowerShell 5.1:
    # use --browser=value instead of --browser value.
    #
    # Construct one native argument containing the complete flag.
    $BrowserArgument = "--browser=$BrowserId"

    $Output = @(
        & $BrowsePath navigate $RegistrationUrl $BrowserArgument 2>&1
    )

    # Capture immediately after the native executable returns.
    $ExitCode = $LASTEXITCODE

    if ($ExitCode -ne 0) {
        $Diagnostic = ($Output | ForEach-Object {
            $_.ToString()
        }) -join [Environment]::NewLine

        throw "browse navigate failed (exit code $ExitCode): $Diagnostic"
    }

    Write-Info "Navigation command completed."

    if ($Output.Count -gt 0) {
        Write-Host ""
        Write-Host "CLI result:"

        $Output | ForEach-Object {
            Write-Host $_
        }
    }

    Write-Host ""
    Write-Host "Registration page:"
    Write-Host $RegistrationUrl
    Write-Host ""
    Write-Host "Next actions (manual):"
    Write-Host "  1. Inspect the page in your paired browser."
    Write-Host "  2. Select the desired Proton Mail plan."
    Write-Host "  3. Choose a username beginning with oahcom."
    Write-Host "  4. Set your password."
    Write-Host "  5. If requested, enter your own recovery email."
    Write-Host "  6. Complete CAPTCHA/human verification yourself."
    Write-Host "  7. Review and submit the registration yourself."
    Write-Host ""
    Write-Host "Do not assume registration succeeded merely because"
    Write-Host "the registration page was opened."

    Write-Info "Browser handoff ready."
}
catch {
    Write-Failure $_.Exception.Message
    exit 3
}
