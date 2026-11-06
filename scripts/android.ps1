param([ValidateSet('Doctor', 'Start', 'Studio', 'Devices')][string]$Action = 'Doctor')

$ErrorActionPreference = 'Stop'
$sdkRoot = 'D:\Android\Sdk'
$env:ANDROID_HOME = $sdkRoot
$env:ANDROID_AVD_HOME = 'D:\Android\avd'
$emulator = Join-Path $sdkRoot 'emulator\emulator.exe'
$adb = Join-Path $sdkRoot 'platform-tools\adb.exe'

switch ($Action) {
    'Doctor' {
        & $emulator -accel-check
        $accelerationExitCode = $LASTEXITCODE
        & $emulator -list-avds
        & $adb version
        if ($accelerationExitCode -ne 0) {
            Write-Warning 'Emulator acceleration is not ready. Restart Windows if WHPX was just enabled.'
            exit $accelerationExitCode
        }
    }
    'Start' {
        Start-Process -FilePath $emulator -ArgumentList @('-avd', 'AutoDiag_API35', '-no-snapshot-load')
    }
    'Studio' {
        Start-Process -FilePath 'D:\soft\AndroidStudio\bin\studio64.exe' -ArgumentList @((Resolve-Path (Join-Path $PSScriptRoot '..')).Path)
    }
    'Devices' { & $adb devices -l }
}
