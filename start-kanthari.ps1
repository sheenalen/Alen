$scriptPath = Join-Path $PSScriptRoot "serve-kanthari.ps1"

Start-Process powershell.exe -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    "`"$scriptPath`""
) -WorkingDirectory $PSScriptRoot

Write-Host "Kanthari server window opened."
Write-Host "Open http://127.0.0.1:5000 after the server says it is running."
