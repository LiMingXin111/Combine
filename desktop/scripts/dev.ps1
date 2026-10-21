param(
    [switch]$Browser,
    [switch]$Check,
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path $PSScriptRoot -Parent)
$nodeCommand = (Get-Command node -ErrorAction Stop).Source
$arguments = @('scripts/dev.mjs')
if ($Browser) { $arguments += '--browser' }
if ($Check) { $arguments += '--check' }
if ($Stop) { $arguments += '--stop' }
& $nodeCommand @arguments
exit $LASTEXITCODE
