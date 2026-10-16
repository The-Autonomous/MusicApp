$ErrorActionPreference='Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$python='C:/Users/Caleb/AppData/Local/Programs/Python/Python313/python.exe'
function Status([string]$State,[string]$Detail) {
    @{state=$State;detail=$Detail;updated=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content native/ffmpeg/cpu-tuning-status.json -Encoding UTF8
}
try {
    $compat=Get-Content benchmarks/results/ffmpeg-pgo-release-compat/compatibility.json -Raw | ConvertFrom-Json
    if (-not $compat.passed -or $compat.passed_tests -ne 153) { throw 'Candidate compatibility is incomplete' }
    $candidate='native/ffmpeg/prefix/bin/ffmpeg-clang-pgo.exe'
    if ((Get-FileHash $candidate).Hash -ne $compat.candidate.sha256) { throw 'Candidate differs from tested release' }
    Status 'extended-confirmation' 'Exploratory screen showed 2.36% aggregate CPU reduction, but its t interval crossed zero. Independent confirmation increased to 15 sustained repeats per format, before viewing its results. Same CPU acceptance thresholds apply.'
    & $python -B benchmarks/benchmark_ffmpeg.py --reference native/ffmpeg/reference/ffmpeg-slim-before-cpu-tuning.exe --candidate $candidate --fixture-generator native/ffmpeg/reference/ffmpeg-7.1-full.exe --fixtures benchmarks/results/ffmpeg-pgo-release-compat/fixtures --output benchmarks/results/ffmpeg-pgo-confirm --startup-repeats 7 --sustained-repeats 15 --sustained-seconds 1800 *> native/ffmpeg/logs/pgo-confirm.log
    if ($LASTEXITCODE -ne 0) { throw 'Confirmation benchmark failed' }
    & $python -B benchmarks/assess_ffmpeg_cpu.py benchmarks/results/ffmpeg-pgo-confirm/benchmark.json *> native/ffmpeg/logs/pgo-confirm-assessment.log
    if ($LASTEXITCODE -ne 0) { throw 'Confirmation assessment failed' }
    $confirm=Get-Content benchmarks/results/ffmpeg-pgo-confirm/cpu-assessment.json -Raw | ConvertFrom-Json
    if (-not $confirm.accepted_cpu_improvement) { Status 'no-proven-gain' 'Larger independent confirmation did not meet the CPU gate. Original slim helper remains active.'; exit 0 }
    Status 'original-full-comparison' 'Independent confirmation passed. Comparing with historical full FFmpeg to quantify the remaining earlier CPU gap.'
    & $python -B benchmarks/benchmark_ffmpeg.py --reference native/ffmpeg/reference/ffmpeg-7.1-full.exe --candidate $candidate --fixtures benchmarks/results/ffmpeg-pgo-release-compat/fixtures --output benchmarks/results/ffmpeg-pgo-versus-full --startup-repeats 1 --sustained-repeats 5 --sustained-seconds 1800 *> native/ffmpeg/logs/pgo-versus-full.log
    if ($LASTEXITCODE -ne 0) { throw 'Historical comparison failed' }
    Status 'proven-ready' 'Independent 15-repeat confirmation passed CPU gate; compatibility passed. Awaiting actual Rust playback review and deployment.'
} catch { Status 'failed' $_.Exception.Message; throw }
