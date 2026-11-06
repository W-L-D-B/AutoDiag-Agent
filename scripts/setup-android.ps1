param(
    [string]$SdkRoot = 'D:\Android\Sdk',
    [string]$StudioRoot = 'D:\soft\AndroidStudio'
)

$ErrorActionPreference = 'Stop'
$env:JAVA_HOME = Join-Path $StudioRoot 'jbr'
$env:ANDROID_HOME = $SdkRoot
$env:ANDROID_AVD_HOME = 'D:\Android\avd'
New-Item -ItemType Directory -Path $env:ANDROID_AVD_HOME -Force | Out-Null
$androidCli = Join-Path $SdkRoot 'cmdline-tools\latest\bin\android.exe'
$avdManager = Join-Path $SdkRoot 'cmdline-tools\latest\bin\avdmanager.bat'
$artifactDirectory = Join-Path $PSScriptRoot '..\artifacts'
New-Item -ItemType Directory -Path $artifactDirectory -Force | Out-Null

& $androidCli --no-metrics "--sdk=$SdkRoot" sdk install 'platform-tools' 'platforms/android-35' 'build-tools/35.0.0' 'emulator' 'system-images/android-35/google_apis/x86_64'
if ($LASTEXITCODE -ne 0) { throw 'Android SDK package installation failed.' }

if (-not (Test-Path -LiteralPath (Join-Path $env:ANDROID_AVD_HOME 'AutoDiag_API35.ini'))) {
    'no' | & $avdManager create avd --name AutoDiag_API35 --package 'system-images;android-35;google_apis;x86_64' --device pixel_6
    if ($LASTEXITCODE -ne 0) { throw 'Android virtual device creation failed.' }
}

[Environment]::SetEnvironmentVariable('ANDROID_HOME', $SdkRoot, 'User')
[Environment]::SetEnvironmentVariable('ANDROID_AVD_HOME', $env:ANDROID_AVD_HOME, 'User')
Write-Output 'Android SDK and AutoDiag_API35 AVD are ready.'
