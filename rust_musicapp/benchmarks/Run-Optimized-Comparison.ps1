param([int]$BuildProcessId)
$ErrorActionPreference = 'Stop'
$portRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $portRoot
$statusPath = Join-Path $PSScriptRoot 'results/optimized-pipeline-status.json'
function Write-Status([string]$Phase,[string]$Detail) {
    @{phase=$Phase;detail=$Detail;updated_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
}
try {
    Write-Status 'waiting_for_build' 'No measurements run while the release compiler is active.'
    if ($BuildProcessId) {
        $build = Get-Process -Id $BuildProcessId -ErrorAction SilentlyContinue
        if ($build -and -not $build.WaitForExit(1200000)) { throw 'Release build did not finish within twenty minutes.' }
        if ($build -and $null -ne $build.ExitCode -and $build.ExitCode -ne 0) { throw 'Release build failed.' }
    }
    $exe = Join-Path $portRoot 'dist/MusicApp/MusicApp.exe'
    $previous = Join-Path $PSScriptRoot 'baselines/2026-10-14-first-release/MusicApp.exe'
    if ((Get-FileHash -LiteralPath $exe).Hash -eq (Get-FileHash -LiteralPath $previous).Hash) { throw 'The packaged executable has not been updated.' }
    $uiSource = (Get-FileHash -LiteralPath (Join-Path $portRoot 'ui/app.tcl')).Hash
    if ($uiSource -ne (Get-FileHash -LiteralPath (Join-Path $portRoot 'dist/MusicApp/ui/app.tcl')).Hash) { throw 'Packaged UI does not match the optimized source.' }
    $python = Join-Path $env:LOCALAPPDATA 'Programs/Python/Python313/python.exe'
    $profile = Join-Path $portRoot 'dist/MusicApp/state'
    Write-Status 'smoke_check' 'Checking playback/effects validation with the new packaged executable.'
    & $python -B benchmarks/run_benchmarks.py --rust-exe $exe --profile-dir $profile --scenarios playback_profile --repeats 1 --seconds 1 --warmup 0.5 --skip-charts --output benchmarks/results/optimized-release-smoke *> benchmarks/results/optimized-release-smoke.log
    if ($LASTEXITCODE -ne 0) { throw 'Optimized release smoke check failed; inspect its log.' }
    Write-Status 'benchmarking' '99 trials: unchanged Python, previous Rust, optimized Rust; 11 states, three repeats each.'
    & $python -B benchmarks/run_benchmarks.py --rust-exe $exe --previous-rust-exe $previous --profile-dir $profile --output benchmarks/results/2026-10-14-optimized-threeway *> benchmarks/results/optimized-threeway.log
    if ($LASTEXITCODE -ne 0) { throw 'Benchmark reported failed trials; inspect optimized-threeway.log and results.json.' }
    Write-Status 'complete' '99 trials and detailed charts finished; ready for analysis.'
} catch {
    Write-Status 'failed' $_.Exception.Message
    throw
}
