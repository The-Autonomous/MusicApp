param([switch]$Check,[switch]$RuntimeOnly,[string]$TkRuntime)
$ErrorActionPreference = 'Stop'
$portRoot = $PSScriptRoot
Push-Location -LiteralPath $portRoot
try {
    # The application links native C Tcl/Tk directly. It never loads Python.
    $runtimeRoot = Join-Path $portRoot 'runtime'
    if ($TkRuntime) {
        $resolvedRuntime = (Resolve-Path -LiteralPath $TkRuntime).Path
        New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
        foreach ($name in @('tcl86t.dll','tk86t.dll','tcl8.6','tk8.6')) {
            $source = Join-Path $resolvedRuntime $name
            if (-not (Test-Path -LiteralPath $source)) { throw "Native Tcl/Tk component missing: $source" }
            Copy-Item -LiteralPath $source -Destination (Join-Path $runtimeRoot $name) -Recurse -Force
        }
    }
    foreach ($name in @('tcl86t.dll','tk86t.dll','tcl8.6','tk8.6')) {
        if (-not (Test-Path -LiteralPath (Join-Path $runtimeRoot $name))) { throw "Missing native Tcl/Tk runtime. Supply -TkRuntime with its four components. No system installation was changed." }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $portRoot 'tools/ffmpeg.exe'))) {
        throw 'Place a Windows FFmpeg executable at tools/ffmpeg.exe. No package installer is run automatically.'
    }
    if ($RuntimeOnly) { Write-Host 'Native runtime components are present.'; return }
    $cargoCommand = Get-Command cargo.exe -ErrorAction SilentlyContinue
    $cargoPath = if ($cargoCommand) { $cargoCommand.Source } else { Join-Path $env:USERPROFILE '.cargo/bin/cargo.exe' }
    if (-not (Test-Path -LiteralPath $cargoPath)) { throw 'Rust MSVC toolchain is missing. Existing installations are not upgraded by this script.' }
    $env:PATH = (Split-Path -Parent $cargoPath) + ';' + $env:PATH
    $env:CARGO_BUILD_JOBS = '2'
    if ($Check) {
        & $cargoPath test --locked --quiet
        if ($LASTEXITCODE -ne 0) { throw 'Rust tests failed.' }
    }
    & $cargoPath build --release --locked
    if ($LASTEXITCODE -ne 0) { throw 'Rust build failed.' }
    $packageRoot = Join-Path $portRoot 'dist/MusicApp'
    New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $portRoot 'target/release/rust_musicapp.exe') -Destination (Join-Path $packageRoot 'MusicApp.exe') -Force
    foreach ($name in @('runtime','tools','ui','app_pad','licenses','docs','browser_connector')) {
        $sourceRoot = Join-Path $portRoot $name
        $destRoot = Join-Path $packageRoot $name
        New-Item -ItemType Directory -Path $destRoot -Force | Out-Null
        Get-ChildItem -LiteralPath $sourceRoot -Force | Where-Object { $_.Name -ne '__pycache__' } | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $destRoot -Recurse -Force }
    }
    # Application-local MSVC support; no system redistributable installation.
    $redistRoot = Join-Path ${env:ProgramFiles} 'Microsoft Visual Studio/2022/Community/VC/Redist/MSVC'
    if (Test-Path -LiteralPath $redistRoot) {
        $versionRoot = Get-ChildItem -LiteralPath $redistRoot -Directory | Where-Object { $_.Name -match '^\d+\.' } | Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
        $crtRoot = Join-Path $versionRoot.FullName 'x64/Microsoft.VC143.CRT'
        if (Test-Path -LiteralPath $crtRoot) { Get-ChildItem -LiteralPath $crtRoot -Filter '*.dll' -File | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $packageRoot -Force } }
    }
    foreach ($name in @('README.md','LICENSE','THIRD_PARTY.md')) {
        $source = Join-Path $portRoot $name
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $packageRoot -Force }
    }
    $benchmarkDocs = Join-Path $packageRoot 'benchmarks'
    New-Item -ItemType Directory -Path $benchmarkDocs -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $portRoot 'benchmarks/README.md') -Destination $benchmarkDocs -Force
    Set-Content -LiteralPath (Join-Path $packageRoot 'connect-browser.bat') -Encoding ASCII -Value '@echo off', 'cd /d "%~dp0"', 'start "Connect MusicApp" "%~dp0MusicApp.exe" --connect-browser-profile %*'
    $playlistPath = Join-Path $packageRoot 'Playlists.txt'
    if (-not (Test-Path -LiteralPath $playlistPath)) { Set-Content -LiteralPath $playlistPath -Value '# One YouTube playlist URL per line.' -Encoding UTF8 }
    Write-Host "Built: $(Join-Path $packageRoot 'MusicApp.exe')"
    Write-Host 'Keep the adjacent folders with the executable. Your original app was not replaced.'
} finally { Pop-Location }
