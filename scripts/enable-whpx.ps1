$ErrorActionPreference = 'Stop'
$reportDirectory = Join-Path $PSScriptRoot '..\artifacts'
New-Item -ItemType Directory -Path $reportDirectory -Force | Out-Null
$reportPath = Join-Path $reportDirectory 'whpx-feature.json'
try {
    $result = Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All -NoRestart -ErrorAction Stop
    [pscustomobject]@{
        Status = 'Enabled'
        RestartNeeded = [bool]$result.RestartNeeded
        CheckedAt = (Get-Date).ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $reportPath -Encoding UTF8
} catch {
    [pscustomobject]@{ Status = 'Error'; Message = $_.Exception.Message } |
        ConvertTo-Json | Set-Content -LiteralPath $reportPath -Encoding UTF8
    exit 1
}
