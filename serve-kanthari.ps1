$python = "C:\Python314\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

Set-Location $PSScriptRoot
Write-Host "Starting Kanthari on http://127.0.0.1:5000"
& $python ".\app.py"
