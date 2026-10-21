param(
    [Parameter(Mandatory = $true)][string]$Python312,
    [switch]$NoBundle,
    [switch]$SkipDependencyInstall
)
$ErrorActionPreference = 'Stop'
$desktopRoot = Split-Path $PSScriptRoot -Parent
$repositoryRoot = Split-Path $desktopRoot -Parent
Set-Location -LiteralPath $desktopRoot

function Assert-Exit([string]$operation) {
    if ($LASTEXITCODE -ne 0) { throw "$operation failed with exit code $LASTEXITCODE" }
}

& $Python312 -c 'import sys; assert sys.version_info[:2] == (3,12), "Python 3.12 is required"'
Assert-Exit 'Python validation'
& $Python312 scripts/prepare_runtime.py
Assert-Exit 'Runtime preparation'
if (-not $SkipDependencyInstall) {
    $uvCommand = (Get-Command uv -ErrorAction Stop).Source
    & $uvCommand pip install --python $Python312 --target src-tauri/resources/python/Lib/site-packages --no-deps -r requirements-lock.txt
    Assert-Exit 'Locked runtime dependencies'
    # pip/uv can retain non-inheriting wheel ACLs; invalidate the scoped grant.
    $runtimeAclMarker = Join-Path $desktopRoot 'src-tauri/resources/python/.appcontainer-RX'
    if (Test-Path -LiteralPath $runtimeAclMarker) { Remove-Item -LiteralPath $runtimeAclMarker }
}
& './src-tauri/resources/python/python.exe' -I -c 'import pypdf, pypdfium2, PIL, rapidocr_onnxruntime'
Assert-Exit 'Bundled adapter imports'

$localCargo = Join-Path $repositoryRoot '.build/cargo'
if (Test-Path -LiteralPath (Join-Path $localCargo 'bin/cargo.exe')) {
    $env:CARGO_HOME = $localCargo
    $env:RUSTUP_HOME = Join-Path $repositoryRoot '.build/rustup'
    $env:PATH = (Join-Path $localCargo 'bin') + ';' + $env:PATH
}
Get-Command cargo -ErrorAction Stop | Out-Null
Get-Command npm.cmd -ErrorAction Stop | Out-Null
& npm.cmd ci
Assert-Exit 'Locked frontend dependencies'
& './src-tauri/resources/python/python.exe' -m unittest discover -s tests -p 'test_*.py'
Assert-Exit 'Runtime regression'
& './src-tauri/resources/python/python.exe' tests/sandbox_probe.py
Assert-Exit 'AppContainer gate'
foreach ($directory in @('src-tauri/resources/python', 'src-tauri/resources/runtime')) {
    $marker = Join-Path $desktopRoot ($directory + '/.appcontainer-RX')
    if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker }
}

if ($NoBundle) {
    & npm.cmd exec tauri build -- --no-bundle
} else {
    & $Python312 scripts/prepare_nsis.py
    Assert-Exit 'NSIS preparation'
    & npm.cmd exec tauri build -- --bundles nsis
}
Assert-Exit 'Desktop build'
Write-Output 'Build complete. See src-tauri/target/release and README.md for validation limits.'
