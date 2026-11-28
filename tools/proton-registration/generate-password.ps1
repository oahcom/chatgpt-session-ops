[CmdletBinding()]
param(
    [ValidateRange(24, 64)]
    [int]$Length = 32
)

$ErrorActionPreference = "Stop"

# Character sets.
$Upper = [char[]]"ABCDEFGHJKLMNPQRSTUVWXYZ"
$Lower = [char[]]"abcdefghijkmnopqrstuvwxyz"
$Digits = [char[]]"23456789"
$Special = [char[]]"!@#$%^&*-_=+"

$All = [char[]]($Upper + $Lower + $Digits + $Special)

# .NET Framework 4.8 compatible cryptographic RNG.
$Rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()

function Get-SecureIndex {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Max,

        [Parameter(Mandatory = $true)]
        [System.Security.Cryptography.RandomNumberGenerator]$Generator
    )

    if ($Max -lt 1 -or $Max -gt 256) {
        throw "Max must be between 1 and 256."
    }

    # Rejection sampling prevents modulo bias.
    $Limit = 256 - (256 % $Max)
    $Buffer = New-Object byte[] 1

    do {
        $Generator.GetBytes($Buffer)
        $Value = [int]$Buffer[0]
    } while ($Value -ge $Limit)

    return [int]($Value % $Max)
}

$PasswordChars = New-Object 'System.Collections.Generic.List[char]'

try {
    # Guarantee at least one character from each category.
    foreach ($Set in @($Upper, $Lower, $Digits, $Special)) {
        $Index = Get-SecureIndex `
            -Max $Set.Length `
            -Generator $Rng

        $PasswordChars.Add($Set[$Index])
    }

    # Fill remaining positions from the complete alphabet.
    while ($PasswordChars.Count -lt $Length) {
        $Index = Get-SecureIndex `
            -Max $All.Length `
            -Generator $Rng

        $PasswordChars.Add($All[$Index])
    }

    # Cryptographically randomized Fisher-Yates shuffle.
    for ($i = $PasswordChars.Count - 1; $i -gt 0; $i--) {
        $j = Get-SecureIndex `
            -Max ($i + 1) `
            -Generator $Rng

        $Temp = $PasswordChars[$i]
        $PasswordChars[$i] = $PasswordChars[$j]
        $PasswordChars[$j] = $Temp
    }

    $Password = -join $PasswordChars.ToArray()

    Write-Host ""
    Write-Host "Generated password:" -ForegroundColor Green
    Write-Host $Password -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Copy it into your password manager immediately."
    Write-Host "This script does not save the password to disk."
}
finally {
    if ($null -ne $Rng) {
        $Rng.Dispose()
    }
}
