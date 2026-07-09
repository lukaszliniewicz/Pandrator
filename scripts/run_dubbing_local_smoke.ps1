$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$pixiCommand = Get-Command pixi -ErrorAction SilentlyContinue
if (-not $pixiCommand) {
    $localPixi = Join-Path $env:USERPROFILE ".local\bin\pixi.exe"
    if (Test-Path $localPixi) {
        $pixiExecutable = $localPixi
    } else {
        throw "Pixi was not found on PATH or at $localPixi."
    }
} else {
    $pixiExecutable = $pixiCommand.Source
}

$env:PIXI_HOME = Join-Path $repoRoot ".pixi-home"
$env:PIXI_CACHE_DIR = Join-Path $repoRoot ".pixi-cache"
$env:RATTLER_CACHE_DIR = Join-Path $repoRoot ".pixi-cache\rattler"
$env:PIXI_NO_PATH_UPDATE = "1"

& $pixiExecutable run --manifest-path (Join-Path $repoRoot "pixi.toml") -e smoke smoke-dubbing-local
exit $LASTEXITCODE
