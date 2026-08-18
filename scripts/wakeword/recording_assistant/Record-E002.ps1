<#
  One-click launcher for the E002 "Hey Youtab" recording assistant.

  Double-click, or from PowerShell:  .\Record-E002.ps1
  Pass extra flags through, e.g.:     .\Record-E002.ps1 --input-device 3
                                      .\Record-E002.ps1 --list-devices
                                      .\Record-E002.ps1 --mic-check 4

  It runs the assistant out of the isolated operator venv (.venv-recorder),
  from the repo root so the package imports, recording at 48 kHz mono 16-bit.
#>
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
Set-Location $repo
$py = Join-Path $repo '.venv-recorder\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host "Operator venv not found at $py" -ForegroundColor Red
    Write-Host "Create it once (see scripts/wakeword/recording_assistant/README.md):" -ForegroundColor Yellow
    Write-Host "  py -3.12 -m venv .venv-recorder"
    Write-Host "  .\.venv-recorder\Scripts\python.exe -m pip install -r scripts/wakeword/recording_assistant/requirements-recording-assistant.txt numpy"
    exit 1
}
& $py -m scripts.wakeword.recording_assistant.app --speaker E002 --rate 48000 @args
exit $LASTEXITCODE
