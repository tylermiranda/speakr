<#
.SYNOPSIS
  Install project-memory hooks into the user's Copilot CLI configuration (Windows).
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $py) { throw "Python was not found on PATH." }

$script = Join-Path (Get-Location).Path "Setup-ProjectMemoryHooks.py"
if (-not (Test-Path $script)) {
    throw "Setup-ProjectMemoryHooks.py not found in the current directory."
}

& $py.Source $script
exit $LASTEXITCODE
