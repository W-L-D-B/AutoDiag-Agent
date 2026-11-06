param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Command)

$ErrorActionPreference = 'Stop'
$arguments = @('-d', 'AutoDiagUbuntu', '-u', 'autodiag', '--cd', '/home/autodiag/projects/AutoDiag_Agent')
if ($Command.Count -gt 0) {
    $arguments += '--exec'
    $arguments += $Command
}
& wsl.exe @arguments
exit $LASTEXITCODE
