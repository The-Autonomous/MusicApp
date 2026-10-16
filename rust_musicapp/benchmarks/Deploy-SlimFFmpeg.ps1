param([string]$ReportPrefix = 'ffmpeg-audio-final')
$ErrorActionPreference = 'Stop'
$portRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $portRoot
$candidate = Join-Path $portRoot 'native/ffmpeg/prefix/bin/ffmpeg-clang.exe'
$compat = Get-Content -LiteralPath "benchmarks/results/$ReportPrefix-compat/compatibility.json" -Raw | ConvertFrom-Json
$perf = Get-Content -LiteralPath "benchmarks/results/$ReportPrefix-performance/benchmark.json" -Raw | ConvertFrom-Json
if (-not $compat.passed -or $compat.passed_tests -ne 153 -or $compat.failed_tests -ne 0) { throw 'Compatibility gate is incomplete.' }
if ($perf.runs.Count -ne 134 -or $perf.errors.Count -ne 0) { throw 'Benchmark gate is incomplete.' }
$candidateHash = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash
if ($candidateHash -ne $compat.candidate.sha256 -or $candidateHash -ne $perf.executables.Candidate.sha256) { throw 'Candidate differs from the tested binary.' }
$before = Get-Content -LiteralPath native/ffmpeg/pre-deployment.json -Raw | ConvertFrom-Json
if ((Get-FileHash dist/MusicApp/MusicApp.exe -Algorithm SHA256).Hash -ne $before.rust_exe) { throw 'Rust executable changed during native tests.' }
foreach ($target in @('tools/ffmpeg.exe','dist/MusicApp/tools/ffmpeg.exe')) {
    $current = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    if ($current -ne $before.full_ffmpeg -and $current -ne $candidateHash) { throw "Unexpected FFmpeg at $target; leaving it alone." }
}
Copy-Item -LiteralPath $candidate -Destination tools/ffmpeg.exe -Force
Copy-Item -LiteralPath $candidate -Destination dist/MusicApp/tools/ffmpeg.exe -Force
@('Exact native FFmpeg license:', (& $candidate -L 2>&1 | Out-String), 'Exact version/configuration:', (& $candidate -version 2>&1 | Out-String)) |
    Set-Content -LiteralPath licenses/FFmpeg-build.txt -Encoding UTF8
Get-ChildItem -LiteralPath licenses -File | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination dist/MusicApp/licenses -Force }
foreach ($document in @('README.md','THIRD_PARTY.md')) { Copy-Item -LiteralPath $document -Destination dist/MusicApp -Force }
$smokeRoot = Join-Path $portRoot "benchmarks/results/$ReportPrefix-smoke"
New-Item -ItemType Directory -Path $smokeRoot -Force | Out-Null
$reference = Join-Path $portRoot 'native/ffmpeg/reference/ffmpeg-7.1-full.exe'
$inputMedia = Join-Path $portRoot "benchmarks/results/$ReportPrefix-compat/fixtures/wav_pcm16_stereo48.wav"
$media = @{}
foreach ($format in @('mp3','m4a','webm')) {
    $media[$format] = Join-Path $smokeRoot "audio20.$format"
    $codec = switch ($format) { 'mp3' { @('-c:a','libmp3lame','-b:a','192k') } 'm4a' { @('-c:a','aac','-b:a','160k') } 'webm' { @('-c:a','libopus','-b:a','96k') } }
    & $reference -nostdin -hide_banner -loglevel error -y -threads 1 -filter_threads 1 -stream_loop -1 -i $inputMedia -t 20 -vn @codec $media[$format] 2> (Join-Path $smokeRoot "generate-$format.log")
    if ($LASTEXITCODE -ne 0) { throw "Could not generate $format smoke input." }
}
$checks = @()
foreach ($case in @(@('mp3','playback_bypass'),@('mp3','playback_eq'),@('mp3','playback_echo'),@('m4a','playback_bypass'),@('webm','playback_bypass'))) {
    $name = "$($case[0])-$($case[1])"
    $state = Join-Path $smokeRoot $name
    New-Item -ItemType Directory -Path $state -Force | Out-Null
    $arguments = @('--benchmark',$case[1],'--media',$media[$case[0]],'--state-dir',$state,'--seconds','4','--no-network','--no-hooks','--mute')
    $process = Start-Process -FilePath (Join-Path $portRoot 'dist/MusicApp/MusicApp.exe') -ArgumentList $arguments -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $state 'stdout.log') -RedirectStandardError (Join-Path $state 'stderr.log')
    if (-not $process.WaitForExit(20000)) { $process.Kill(); throw "Owned smoke process timed out: $name" }
    if ($process.ExitCode -ne 0) { throw "Rust package smoke failed: $name" }
    $audio = Get-Content -LiteralPath (Join-Path $state 'benchmark_audio.json') -Raw | ConvertFrom-Json
    if ($audio.last_error -or $audio.underflows -ne 0 -or $audio.position_seconds -lt 2 -or -not $audio.is_playing) { throw "Rust audio validation failed: $name" }
    $checks += @{ case=$name; exit_code=$process.ExitCode; audio=$audio }
}
$sourceManifest = Get-Content legacy_reference/reference/source_hashes.json -Raw | ConvertFrom-Json -AsHashtable
$profileManifest = Get-Content state/original-profile-hashes.json -Raw | ConvertFrom-Json -AsHashtable
$originalRoot = Split-Path -Parent $portRoot
foreach ($manifest in @($sourceManifest,$profileManifest)) {
    foreach ($entry in $manifest.GetEnumerator()) {
        if ((Get-FileHash -LiteralPath (Join-Path $originalRoot $entry.Key) -Algorithm SHA256).Hash -ne $entry.Value) { throw "Original file hash changed: $($entry.Key)" }
    }
}
foreach ($property in $before.ui.PSObject.Properties) {
    foreach ($uiRoot in @('ui','dist/MusicApp/ui')) {
        if ((Get-FileHash -LiteralPath (Join-Path $uiRoot $property.Name) -Algorithm SHA256).Hash -ne $property.Value) { throw "UI hash changed: $uiRoot/$($property.Name)" }
    }
}
foreach ($target in @('tools/ffmpeg.exe','dist/MusicApp/tools/ffmpeg.exe')) {
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $candidateHash) { throw "Deployment hash differs: $target" }
}
$report = @{ checked_utc=[DateTime]::UtcNow.ToString('o'); compatibility_passed=$compat.passed_tests; compatibility_failed=$compat.failed_tests; benchmark_runs=$perf.runs.Count; benchmark_errors=$perf.errors.Count; native_ffmpeg_sha256=$candidateHash; native_ffmpeg_bytes=(Get-Item $candidate).Length; original_ffmpeg_bytes=$perf.executables.Reference.size_bytes; rust_exe_unchanged=((Get-FileHash dist/MusicApp/MusicApp.exe -Algorithm SHA256).Hash -eq $before.rust_exe); ui_changed=0; original_source_changes=0; original_sources_checked=$sourceManifest.Count; original_profile_changes=0; original_profiles_checked=$profileManifest.Count; smoke=$checks; package_bytes_before=$before.package_bytes_before; package_bytes_after=(Get-ChildItem dist/MusicApp -Recurse -File | Measure-Object Length -Sum).Sum; python_runtime_files=@(Get-ChildItem dist/MusicApp -Recurse -File | Where-Object { $_.Extension -in @('.py','.pyc','.pyd') -or $_.Name -match '^python.*\.dll$' }).Count }
$report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $smokeRoot 'final-verification.json') -Encoding UTF8
Write-Output 'Audio-only FFmpeg deployed to the Rust source/package. Five real Rust audio/effects smoke runs passed; original sources, profiles, Rust executable and UI hashes match.'
