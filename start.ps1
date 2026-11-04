# Taskbar Stock Widget - PowerShell launcher
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source

if ($pythonw) {
    Start-Process -FilePath $pythonw -ArgumentList @((Join-Path $PSScriptRoot "main.py")) -WorkingDirectory $PSScriptRoot
} elseif ($python) {
    Start-Process -FilePath $python -ArgumentList @((Join-Path $PSScriptRoot "main.py")) -WorkingDirectory $PSScriptRoot
} else {
    Write-Error "Python not found. Please install Python 3.10+ and add it to PATH."
}
