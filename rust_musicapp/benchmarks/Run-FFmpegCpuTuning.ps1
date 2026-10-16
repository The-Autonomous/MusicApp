$ErrorActionPreference = 'Stop'
$portRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $portRoot
$python = 'C:\Users\Caleb\AppData\Local\Programs\Python\Python313\python.exe'
$statusFile = Join-Path $portRoot 'native/ffmpeg/cpu-tuning-status.json'
$full = 'native/ffmpeg/reference/ffmpeg-7.1-full.exe'
$baseline = 'native/ffmpeg/reference/ffmpeg-slim-before-cpu-tuning.exe'
$trained = 'native/ffmpeg/prefix/bin/ffmpeg-clang-pgo-train.exe'
$candidate = 'native/ffmpeg/prefix/bin/ffmpeg-clang-pgo.exe'
function Status([string]$State,[string]$Detail) {
    @{state=$State;detail=$Detail;updated=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $statusFile -Encoding UTF8
}
function Require-Success([string]$Description) { if ($LASTEXITCODE -ne 0) { throw "$Description failed with exit $LASTEXITCODE" } }
try {
    Status 'waiting-instrumented-build' 'No app binary has been changed.'
    $deadline=[DateTime]::UtcNow.AddHours(1)
    do {
        $build=Get-Content native/ffmpeg/build-status-clang-pgo-train.json -Raw | ConvertFrom-Json
        if ($build.state -eq 'failed') { throw $build.detail }
        if ($build.state -eq 'compiled') { break }
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Instrumented build timed out' }
        Start-Sleep -Seconds 5
    } while ($true)
    Status 'training' 'Compatibility suite plus separate 45-second varied training fixtures. No timed measurements yet.'
    $env:LLVM_PROFILE_FILE=(Join-Path $portRoot 'native/ffmpeg/profiles/training/%m-%p.profraw')
    & $python -B benchmarks/verify_slim_ffmpeg.py --reference $full --candidate $trained --output benchmarks/results/ffmpeg-pgo-training-compat --keep-fixtures --tls required *> native/ffmpeg/logs/pgo-training-compat.log
    Require-Success 'Instrumented compatibility'
    & $python -B benchmarks/train_ffmpeg_pgo.py --generator $full --instrumented $trained --output native/ffmpeg/profiles/training-media *> native/ffmpeg/logs/pgo-training-media.log
    Require-Success 'Training workload'
    $profileList=Join-Path $portRoot 'native/ffmpeg/profiles/training-files.txt'
    $profiles=@(Get-ChildItem native/ffmpeg/profiles/training -Filter '*.profraw' -File)
    if ($profiles.Count -lt 20) { throw 'Too few native profiles were collected' }
    [IO.File]::WriteAllLines($profileList,@($profiles.FullName),[Text.UTF8Encoding]::new($false))
    & 'C:/Program Files/LLVM/bin/llvm-profdata.exe' merge -sparse -f $profileList -o native/ffmpeg/profiles/musicapp.profdata *> native/ffmpeg/logs/pgo-profile-merge.log
    Require-Success 'Profile merge'
    & 'C:/Program Files/LLVM/bin/llvm-profdata.exe' show native/ffmpeg/profiles/musicapp.profdata *> native/ffmpeg/logs/pgo-profile-summary.log
    Require-Success 'Profile inspection'
    $env:LLVM_PROFILE_FILE=$null
    Status 'building-optimized' 'Compiling release candidate using native profiles and explicit no-math-errno optimization.'
    & ./Build-FFmpeg.ps1 -Compiler clang-pgo
    $build=Get-Content native/ffmpeg/build-status-clang-pgo.json -Raw | ConvertFrom-Json
    if ($build.state -ne 'compiled') { throw 'Profile-optimized candidate did not compile' }
    Status 'release-compatibility' 'Validating the optimized release before any performance comparison.'
    & $python -B benchmarks/verify_slim_ffmpeg.py --reference $full --candidate $candidate --output benchmarks/results/ffmpeg-pgo-release-compat --keep-fixtures --tls required *> native/ffmpeg/logs/pgo-release-compat.log
    Require-Success 'Release compatibility'
    Status 'screening' 'Current slim versus PGO release; five alternating trials per format, 30-minute fixtures. No code changes during measurement.'
    & $python -B benchmarks/benchmark_ffmpeg.py --reference $baseline --candidate $candidate --fixture-generator $full --fixtures benchmarks/results/ffmpeg-pgo-release-compat/fixtures --output benchmarks/results/ffmpeg-pgo-screen --startup-repeats 1 --sustained-repeats 5 --sustained-seconds 1800 *> native/ffmpeg/logs/pgo-screen.log
    Require-Success 'CPU screening'
    & $python -B benchmarks/assess_ffmpeg_cpu.py benchmarks/results/ffmpeg-pgo-screen/benchmark.json *> native/ffmpeg/logs/pgo-screen-assessment.log
    Require-Success 'Screen assessment'
    $screen=Get-Content benchmarks/results/ffmpeg-pgo-screen/cpu-assessment.json -Raw | ConvertFrom-Json
    if (-not $screen.accepted_cpu_improvement) { Status 'no-proven-gain' 'Candidate passed compatibility but did not meet predefined CPU improvement gate. Active slim app is unchanged.'; exit 0 }
    Status 'confirmation' 'Independent confirmation: nine alternating sustained trials per format plus seven startup trials per format.'
    & $python -B benchmarks/benchmark_ffmpeg.py --reference $baseline --candidate $candidate --fixture-generator $full --fixtures benchmarks/results/ffmpeg-pgo-release-compat/fixtures --output benchmarks/results/ffmpeg-pgo-confirm --startup-repeats 7 --sustained-repeats 9 --sustained-seconds 1800 *> native/ffmpeg/logs/pgo-confirm.log
    Require-Success 'CPU confirmation'
    & $python -B benchmarks/assess_ffmpeg_cpu.py benchmarks/results/ffmpeg-pgo-confirm/benchmark.json *> native/ffmpeg/logs/pgo-confirm-assessment.log
    Require-Success 'Confirmation assessment'
    $confirm=Get-Content benchmarks/results/ffmpeg-pgo-confirm/cpu-assessment.json -Raw | ConvertFrom-Json
    if (-not $confirm.accepted_cpu_improvement) { Status 'no-proven-gain' 'Screening gain did not repeat in independent confirmation. Active slim app is unchanged.'; exit 0 }
    Status 'original-full-comparison' 'Checking the proven candidate against the historical full FFmpeg as well.'
    & $python -B benchmarks/benchmark_ffmpeg.py --reference $full --candidate $candidate --fixtures benchmarks/results/ffmpeg-pgo-release-compat/fixtures --output benchmarks/results/ffmpeg-pgo-versus-full --startup-repeats 1 --sustained-repeats 5 --sustained-seconds 1800 *> native/ffmpeg/logs/pgo-versus-full.log
    Require-Success 'Historical comparison'
    Status 'proven-ready' 'Compatibility passed and CPU gain repeated. Candidate awaits parent review and actual Rust playback tests before deployment.'
} catch {
    Status 'failed' $_.Exception.Message
    throw
} finally { $env:LLVM_PROFILE_FILE=$null }
