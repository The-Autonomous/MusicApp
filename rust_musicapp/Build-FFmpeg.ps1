param([ValidateSet('msvc','clang','clang-pgo-train','clang-pgo')][string]$Compiler = 'clang')
$ErrorActionPreference = 'Stop'
$buildRoot = Join-Path $PSScriptRoot 'native/ffmpeg'
$suffix = if ($Compiler.StartsWith('clang')) { "-$Compiler" } else { '' }
$statusPath = Join-Path $buildRoot "build-status$suffix.json"
function Write-BuildStatus([string]$State, [string]$Detail) {
    @{ state = $State; detail = $Detail; updated = [DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
}
Write-BuildStatus 'building' 'Compiling app-local static LAME, zlib, and audio-only FFmpeg; installed tools stay unchanged.'
try {
    & cmd.exe /d /c (Join-Path $buildRoot 'build-slim.cmd') $Compiler *> (Join-Path $buildRoot "logs/build$suffix.log")
    if ($LASTEXITCODE -ne 0) { throw "Native build exited $LASTEXITCODE. See native/ffmpeg/logs/build$suffix.log and build/ffmpeg$suffix/ffbuild/config.log." }
    $candidate = Join-Path $buildRoot "prefix/bin/ffmpeg$suffix.exe"
    if (-not (Test-Path -LiteralPath $candidate)) { throw 'Candidate executable was not produced.' }
    if ($Compiler -eq 'clang-pgo' -or $Compiler -eq 'clang-pgo-train') {
        $configuration = Get-Content -LiteralPath (Join-Path $buildRoot "build/ffmpeg$suffix/ffbuild/config.mak") -Raw
        $requiredFlag = if ($Compiler -eq 'clang-pgo') { '-clang:-fprofile-use=' } else { '-clang:-fprofile-generate' }
        if (-not $configuration.Contains($requiredFlag) -or -not $configuration.Contains('-clang:-fno-math-errno')) {
            throw 'Compiler rejected requested optimization flags; candidate cannot be considered profile-optimized.'
        }
    }
    $muxers = (& $candidate -hide_banner -muxers 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $muxers -notmatch '\bf32le\b' -or $muxers -notmatch '\bmp3\b') {
        throw 'Candidate is missing an application output muxer (f32le or mp3).'
    }
    $decoders = (& $candidate -hide_banner -decoders 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $decoders -match '(?m)^ V[.A-Z]{5}\s+\w+\s') {
        throw 'Candidate unexpectedly contains a video decoder.'
    }
    Write-BuildStatus 'compiled' ('Candidate ready for compatibility validation: ' + $candidate)
} catch {
    Write-BuildStatus 'failed' $_.Exception.Message
    throw
}
