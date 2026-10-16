param([string]$Python = 'C:\Users\Caleb\AppData\Local\Programs\Python\Python313\python.exe', [ValidateSet('msvc','clang')][string]$Compiler = 'msvc', [ValidatePattern('^[a-zA-Z0-9_-]+$')][string]$ReportPrefix)
$ErrorActionPreference = 'Stop'
$portRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $portRoot
$suffix = if ($Compiler -eq 'clang') { '-clang' } else { '' }
if (-not $ReportPrefix) { $ReportPrefix = "ffmpeg-slim$suffix" }
$candidate = "native/ffmpeg/prefix/bin/ffmpeg$suffix.exe"
$statusPath = Join-Path $portRoot "native/ffmpeg/validation-status$suffix.json"
function Write-ValidationStatus([string]$State, [string]$Detail) {
    @{ state = $State; detail = $Detail; updated = [DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
}
try {
    Write-ValidationStatus 'waiting' 'Waiting for native build to finish. No executable has been replaced.'
    $deadline = [DateTime]::UtcNow.AddHours(1)
    do {
        $build = Get-Content -LiteralPath "native/ffmpeg/build-status$suffix.json" -Raw | ConvertFrom-Json
        if ($build.state -eq 'failed') { throw $build.detail }
        if ($build.state -eq 'compiled') { break }
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Native build did not finish within one hour.' }
        Start-Sleep -Seconds 5
    } while ($true)
    Write-ValidationStatus 'compatibility' 'Comparing decoding, seeking, MP3 conversion, metadata, HTTP and local HTTPS.'
    & $Python -B benchmarks/verify_slim_ffmpeg.py --reference native/ffmpeg/reference/ffmpeg-7.1-full.exe --candidate $candidate --output "benchmarks/results/$reportPrefix-compat" --keep-fixtures --tls required *> "native/ffmpeg/logs/compatibility$suffix.log"
    if ($LASTEXITCODE -ne 0) { throw "Compatibility gate failed. Review benchmarks/results/$reportPrefix-compat/compatibility.json." }
    Write-ValidationStatus 'benchmarking' 'Alternating native decoder startup/CPU/memory trials. No code changes or executable replacement during measurement.'
    & $Python -B benchmarks/benchmark_ffmpeg.py --reference native/ffmpeg/reference/ffmpeg-7.1-full.exe --candidate $candidate --fixtures "benchmarks/results/$reportPrefix-compat/fixtures" --output "benchmarks/results/$reportPrefix-performance" *> "native/ffmpeg/logs/performance$suffix.log"
    if ($LASTEXITCODE -ne 0) { throw "Native decoder benchmark failed. Review benchmarks/results/$reportPrefix-performance/benchmark.json." }
    Write-ValidationStatus 'verified' 'Compatibility and benchmarks finished. Candidate awaits final review and deployment into Rust package.'
} catch {
    Write-ValidationStatus 'failed' $_.Exception.Message
    throw
}
