[CmdletBinding()]
param(
    [string]$RustExe = (Join-Path $PSScriptRoot '..\target\release\rust_musicapp.exe'),
    [ValidateRange(1, 100)][int]$Repeats = 3,
    [ValidateRange(0.1, 3600)][double]$Seconds = 8,
    [ValidateRange(0, 600)][double]$Warmup = 2,
    [ValidateRange(0.05, 10)][double]$Interval = 0.25,
    [ValidateRange(1, 300)][double]$StartupTimeout = 30,
    [ValidateSet('idle_visible','idle_hidden','paused_visible','paused_hidden','playback_bypass','playback_eq','playback_echo','controls','search','progress')]
    [string[]]$Scenarios = @('idle_visible','idle_hidden','paused_visible','paused_hidden','playback_bypass','playback_eq','playback_echo','controls','search','progress'),
    [string]$OutputDirectory,
    # Optional separately supplied executable subject; it must support the same
    # benchmark flags. No baseline is launched or inferred unless supplied.
    [string]$BaselineExecutable,
    [string[]]$BaselinePrefixArguments = @()
)
$ErrorActionPreference = 'Stop'
$Invariant = [Globalization.CultureInfo]::InvariantCulture
$Base = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$RustExe = [IO.Path]::GetFullPath($RustExe)
if (-not (Test-Path -LiteralPath $RustExe -PathType Leaf)) { throw "Build the release executable first: $RustExe" }
if ($BaselineExecutable) {
    $BaselineExecutable = [IO.Path]::GetFullPath($BaselineExecutable)
    if (-not (Test-Path -LiteralPath $BaselineExecutable -PathType Leaf)) { throw "Baseline executable is missing: $BaselineExecutable" }
}
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $PSScriptRoot ('results\' + (Get-Date -Format 'yyyyMMdd-HHmmss')) }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if (-not $OutputDirectory.StartsWith($Base + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Benchmark output must remain inside rust_musicapp.'
}
if ((Test-Path -LiteralPath $OutputDirectory) -and @(Get-ChildItem -LiteralPath $OutputDirectory -Force).Count -gt 0) {
    throw 'Choose a new or empty output directory; existing benchmark files are preserved.'
}
$null = New-Item -ItemType Directory -Path $OutputDirectory -Force

# Windows/.NET only. Compile the small native process-enumeration helper in
# memory; any compiler temporary files also stay inside the owned project.
if (-not ('MusicAppBenchNative' -as [type])) {
    $taskTemp = Join-Path $PSScriptRoot '.tmp'
    $null = New-Item -ItemType Directory -Path $taskTemp -Force
    $previousTemp, $previousTmp = $env:TEMP, $env:TMP
    try {
        $env:TEMP = $taskTemp; $env:TMP = $taskTemp
        Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class MusicAppBenchNative {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct Entry {
        public uint size, usage, pid; public IntPtr heap;
        public uint module, threads, parent; public int priority; public uint flags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=260)] public string name;
    }
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint pid);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern bool Process32FirstW(IntPtr h, ref Entry e);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern bool Process32NextW(IntPtr h, ref Entry e);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll")] static extern bool GetSystemTimes(out long idle, out long kernel, out long user);
    public static Dictionary<int,int> Parents() {
        var result = new Dictionary<int,int>(); var h = CreateToolhelp32Snapshot(2,0);
        if (h == new IntPtr(-1)) return result;
        try { var e = new Entry(); e.size = (uint)Marshal.SizeOf(typeof(Entry));
            if (Process32FirstW(h,ref e)) do { result[(int)e.pid] = (int)e.parent; } while(Process32NextW(h,ref e));
        } finally { CloseHandle(h); } return result;
    }
    public static long[] SystemTimes() { long i,k,u; GetSystemTimes(out i,out k,out u); return new long[]{i,k,u}; }
    public static void WriteWave(string path, double seconds) {
        int count=(int)Math.Ceiling(seconds*44100), size=count*4;
        using(var b=new BinaryWriter(File.Create(path))) {
            b.Write(System.Text.Encoding.ASCII.GetBytes("RIFF")); b.Write(36+size);
            b.Write(System.Text.Encoding.ASCII.GetBytes("WAVEfmt ")); b.Write(16); b.Write((short)1); b.Write((short)2);
            b.Write(44100); b.Write(44100*4); b.Write((short)4); b.Write((short)16);
            b.Write(System.Text.Encoding.ASCII.GetBytes("data")); b.Write(size);
            for(int n=0;n<count;n++) { double t=n/44100.0;
                double l=Math.Sin(2*Math.PI*440*t)*.18+Math.Sin(2*Math.PI*110*t)*.06;
                double r=Math.Sin(2*Math.PI*660*t)*.18+Math.Sin(2*Math.PI*220*t)*.06;
                b.Write((short)Math.Round(l*32767)); b.Write((short)Math.Round(r*32767));
            }
        }
    }
}
'@
    } finally { $env:TEMP = $previousTemp; $env:TMP = $previousTmp }
}

function Number([double]$Value, [string]$Format = '0.###') { return $Value.ToString($Format, $Invariant) }
function Escape([string]$Value) { return [Security.SecurityElement]::Escape($Value) }
function Mean($Values) { $a = @($Values); if ($a.Count -eq 0) { return 0.0 }; return [double](($a | Measure-Object -Average).Average) }
function StdDev($Values) {
    $a = @($Values); if ($a.Count -lt 2) { return 0.0 }; $mean = Mean $a
    $sum = 0.0; foreach ($value in $a) { $sum += [Math]::Pow(($value - $mean), 2) }
    return [Math]::Sqrt($sum / ($a.Count - 1))
}
function Percentile($Values, [double]$Fraction) {
    $a = @($Values | Sort-Object); if ($a.Count -eq 0) { return 0.0 }
    $index = ($a.Count - 1) * $Fraction; $low = [int][Math]::Floor($index); $high = [int][Math]::Ceiling($index)
    return $a[$low] + ($a[$high] - $a[$low]) * ($index - $low)
}
function Quote-Argument([string]$Value) {
    if ($Value -and $Value -notmatch '[\s"]') { return $Value }
    $quoted = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $quoted = [regex]::Replace($quoted, '(\\+)$', '$1$1')
    return '"' + $quoted + '"'
}
function New-Profiler([int]$RootId) {
    return @{ RootId=$RootId; Known=@{}; Cpu=@{}; PeakRss=0.0; PeakPrivate=0.0; PreviousSystem=([MusicAppBenchNative]::SystemTimes()) }
}
function Get-TreeSnapshot($Profiler) {
    $parents = [MusicAppBenchNative]::Parents()
    $ids = New-Object 'System.Collections.Generic.HashSet[int]'
    $null = $ids.Add($Profiler.RootId)
    do {
        $changed = $false
        foreach ($entry in $parents.GetEnumerator()) {
            if ($ids.Contains($entry.Value) -and $ids.Add($entry.Key)) { $changed = $true }
        }
    } while ($changed)
    foreach ($known in $Profiler.Known.Values) {
        # An observed child may outlive its parent; keep tracking it only while
        # its creation time still matches. Never adopt an unrelated reused PID.
        try {
            $existing = [Diagnostics.Process]::GetProcessById($known.Id)
            try { if (-not $existing.HasExited -and $existing.StartTime.ToUniversalTime().Ticks -eq $known.Start) { $null = $ids.Add([int]$known.Id) } } finally { $existing.Dispose() }
        } catch { }
    }
    $rss = 0.0; $private = 0.0; $threads = 0; $count = 0
    foreach ($processId in $ids) {
        try {
            $process = [Diagnostics.Process]::GetProcessById($processId)
            try {
                if ($process.HasExited) { continue }
                $identity = "$processId/$($process.StartTime.ToUniversalTime().Ticks)"
                $Profiler.Known[$identity] = @{Id=$processId; Start=$process.StartTime.ToUniversalTime().Ticks}
                $Profiler.Cpu[$identity] = $process.TotalProcessorTime.TotalSeconds
                $rss += $process.WorkingSet64 / 1MB; $private += $process.PrivateMemorySize64 / 1MB
                $threads += $process.Threads.Count; $count++
            } finally { $process.Dispose() }
        } catch [System.ArgumentException] { } catch [System.InvalidOperationException] { } catch [System.ComponentModel.Win32Exception] { }
    }
    $cpu = 0.0; foreach ($value in $Profiler.Cpu.Values) { $cpu += $value }
    $Profiler.PeakRss = [Math]::Max($Profiler.PeakRss, $rss); $Profiler.PeakPrivate = [Math]::Max($Profiler.PeakPrivate, $private)
    $system = [MusicAppBenchNative]::SystemTimes(); $previous = $Profiler.PreviousSystem
    $totalDelta = ($system[1] + $system[2]) - ($previous[1] + $previous[2])
    $systemCpu = if ($totalDelta -gt 0) { 100.0 * (1.0 - ($system[0] - $previous[0]) / [double]$totalDelta) } else { 0.0 }
    $Profiler.PreviousSystem = $system
    return [pscustomobject]@{cpu_seconds=$cpu; rss_mib=$rss; private_mib=$private; threads=$threads; processes=$count; system_cpu_percent=$systemCpu}
}
function Stop-OwnedTree($Process, $Profiler) {
    foreach ($known in $Profiler.Known.Values) {
        try {
            $child = [Diagnostics.Process]::GetProcessById($known.Id)
            try { if (-not $child.HasExited -and $child.StartTime.ToUniversalTime().Ticks -eq $known.Start) { $child.Kill() } } finally { $child.Dispose() }
        } catch { }
    }
    try { if (-not $Process.HasExited) { $Process.Kill(); $null = $Process.WaitForExit(3000) } } catch { }
}
function Run-Trial([string]$Implementation, [string]$Scenario, [int]$Repeat, [string]$Media) {
    $state = Join-Path $OutputDirectory ("work\$Scenario-$Repeat-$Implementation")
    if (Test-Path -LiteralPath $state) { throw "Trial directory already exists: $state" }
    $null = New-Item -ItemType Directory -Path $state
    $runtime = $Seconds + $Warmup + 2
    $executable = $RustExe; $prefix = @()
    if ($Implementation -eq 'Baseline') { $executable = $BaselineExecutable; $prefix = $BaselinePrefixArguments }
    $arguments = @($prefix) + @('--benchmark',$Scenario,'--seconds',(Number $runtime),'--media',$Media,'--state-dir',$state,'--no-hooks','--no-network','--mute')
    $argumentString = ($arguments | ForEach-Object { Quote-Argument $_ }) -join ' '
    $log = Join-Path $state 'stdout.log'; $errorLog = Join-Path $state 'stderr.log'
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $process = Start-Process -FilePath $executable -ArgumentList $argumentString -WorkingDirectory $Base -WindowStyle Hidden -PassThru -RedirectStandardOutput $log -RedirectStandardError $errorLog
    $profiler = New-Profiler $process.Id
    $samples = New-Object 'System.Collections.Generic.List[object]'
    try {
        while (-not (Test-Path -LiteralPath (Join-Path $state 'ready'))) {
            $null = Get-TreeSnapshot $profiler
            if ($process.HasExited) { throw "Exited before readiness; see $errorLog" }
            if ($clock.Elapsed.TotalSeconds -gt $StartupTimeout) { throw "Readiness timed out; see $errorLog" }
            Start-Sleep -Milliseconds 20
        }
        $startup = $clock.Elapsed.TotalSeconds
        $null = Get-TreeSnapshot $profiler
        $startupPeakRss=$profiler.PeakRss; $startupPeakPrivate=$profiler.PeakPrivate
        $until = $clock.Elapsed.TotalSeconds + $Warmup
        while ($clock.Elapsed.TotalSeconds -lt $until) {
            if ($process.HasExited) { throw "Exited during warmup; see $errorLog" }
            $null = Get-TreeSnapshot $profiler
            Start-Sleep -Milliseconds ([int][Math]::Max(1,[Math]::Min($Interval*1000,($until-$clock.Elapsed.TotalSeconds)*1000)))
        }
        $previous = Get-TreeSnapshot $profiler; $initialCpu = $previous.cpu_seconds
        $sampleStarted = $clock.Elapsed.TotalSeconds; $previousAt = $sampleStarted
        while ($clock.Elapsed.TotalSeconds - $sampleStarted -lt $Seconds) {
            $remaining = $Seconds - ($clock.Elapsed.TotalSeconds - $sampleStarted)
            Start-Sleep -Milliseconds ([int][Math]::Max(1,[Math]::Min($Interval*1000,$remaining*1000)))
            if ($process.HasExited) { throw "Exited during measurement; see $errorLog" }
            $current = Get-TreeSnapshot $profiler; $now = $clock.Elapsed.TotalSeconds
            $corePercent = ($current.cpu_seconds - $previous.cpu_seconds) / ($now-$previousAt) * 100.0
            $samples.Add([pscustomobject]@{elapsed_seconds=$now-$sampleStarted; cpu_one_core_percent=$corePercent; cpu_machine_percent=$corePercent/[Environment]::ProcessorCount; rss_mib=$current.rss_mib; private_mib=$current.private_mib; threads=$current.threads; processes=$current.processes; system_cpu_percent=$current.system_cpu_percent; cpu_seconds=$current.cpu_seconds})
            $previous = $current; $previousAt = $now
        }
        $elapsed = $previousAt-$sampleStarted
        if (-not $process.WaitForExit(5000)) { throw "Application did not finish; see $errorLog" }
        if ($process.ExitCode -ne 0) { throw "Application exit code $($process.ExitCode); see $errorLog" }
        $audioDiagnostic=$null
        if($Scenario.StartsWith('playback_') -or $Scenario.StartsWith('paused_')) {
            $diagnosticPath=Join-Path $state 'benchmark_audio.json'
            if(-not(Test-Path -LiteralPath $diagnosticPath)){throw "Missing audio validation: $diagnosticPath"}
            $audioDiagnostic=Get-Content -LiteralPath $diagnosticPath -Raw | ConvertFrom-Json
            if($audioDiagnostic.last_error -or $audioDiagnostic.duration_seconds -le 0){throw "Audio validation failed; see $diagnosticPath"}
            if($Scenario.StartsWith('playback_') -and $audioDiagnostic.position_seconds -lt [Math]::Max(0.05,$Seconds*0.5)){throw "Audio clock did not advance; see $diagnosticPath"}
            if($Scenario.StartsWith('paused_') -and -not$audioDiagnostic.is_paused){throw "Paused workload was not paused; see $diagnosticPath"}
        }
        return [pscustomobject]@{
            implementation=$Implementation; scenario=$Scenario; repeat=$Repeat; startup_seconds=$startup; sample_seconds=$elapsed
            cpu_one_core_percent=($previous.cpu_seconds-$initialCpu)/$elapsed*100.0
            cpu_machine_percent=($previous.cpu_seconds-$initialCpu)/$elapsed*100.0/[Environment]::ProcessorCount
            cpu_sample_p95=(Percentile @($samples | ForEach-Object {$_.cpu_one_core_percent}) 0.95)
            rss_mean_mib=(Mean @($samples | ForEach-Object {$_.rss_mib})); rss_peak_mib=($samples | Measure-Object rss_mib -Maximum).Maximum
            private_mean_mib=(Mean @($samples | ForEach-Object {$_.private_mib})); private_peak_mib=($samples | Measure-Object private_mib -Maximum).Maximum
            startup_peak_rss_mib=$startupPeakRss; startup_peak_private_mib=$startupPeakPrivate
            trial_peak_rss_mib=$profiler.PeakRss; trial_peak_private_mib=$profiler.PeakPrivate
            threads_mean=(Mean @($samples | ForEach-Object {$_.threads})); processes_max=($samples | Measure-Object processes -Maximum).Maximum
            audio_validation=$audioDiagnostic; samples=@($samples.ToArray()); executable=$executable; arguments=$arguments; stdout=$log; stderr=$errorLog
        }
    } finally { Stop-OwnedTree $process $profiler; $process.Dispose() }
}
function Summarize-Runs($Runs) {
    $summary = New-Object 'System.Collections.Generic.List[object]'
    foreach ($group in @($Runs | Group-Object scenario,implementation)) {
        $first = $group.Group[0]
        $item = [ordered]@{scenario=$first.scenario; implementation=$first.implementation; repeats=$group.Count}
        foreach ($metric in @('startup_seconds','cpu_one_core_percent','cpu_machine_percent','cpu_sample_p95','rss_mean_mib','rss_peak_mib','private_mean_mib','private_peak_mib','startup_peak_rss_mib','startup_peak_private_mib','trial_peak_rss_mib','trial_peak_private_mib','threads_mean','processes_max')) {
            $values = @($group.Group | ForEach-Object { $_.$metric })
            $item[$metric] = Mean $values; $item["${metric}_stddev"] = StdDev $values
        }
        $summary.Add([pscustomobject]$item)
    }
    return @($summary.ToArray())
}
function Write-Charts($Data) {
    $svg = New-Object Text.StringBuilder
    $null = $svg.AppendLine('<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1120" viewBox="0 0 1200 1120"><rect width="1200" height="1120" fill="#141820"/><g font-family="Segoe UI,Arial,sans-serif" fill="#dce5f2">')
    $null = $svg.AppendLine('<text x="45" y="45" font-size="27" font-weight="bold">MusicApp · measured process resources</text>')
    $subjectText = if ($BaselineExecutable) { 'Rust and supplied external baseline · all observed child processes included' } else { 'Rust only · no baseline supplied and no comparison values inferred' }
    $null = $svg.AppendLine('<text x="45" y="76" font-size="15">' + (Escape $subjectText) + '</text>')
    $implementations = if ($BaselineExecutable) { @('Baseline','Rust') } else { @('Rust') }
    $colors = @{Rust='#00d8b3'; Baseline='#e9b44c'}
    $metrics = @(@('cpu_one_core_percent','CPU: percentage of one logical processor'),@('private_mean_mib','Private committed memory: MiB'),@('startup_seconds','Process launch to ready: seconds'))
    $panel = 0
    foreach ($metricPair in $metrics) {
        $metric = $metricPair[0]; $top = 125 + $panel * 310; $bottom = $top + 200
        $null = $svg.AppendLine('<text x="45" y="'+($top-16)+'" font-size="18">'+(Escape $metricPair[1])+' · lower is better</text>')
        $maximum = 0.01
        foreach ($row in $Data.summary) { $maximum = [Math]::Max($maximum, $row.$metric + $row."${metric}_stddev") }
        $maximum *= 1.2
        foreach ($fraction in @(0,0.25,0.5,0.75,1)) {
            $y = $bottom - 180*$fraction
            $null = $svg.AppendLine('<line x1="85" x2="1165" y1="'+(Number $y)+'" y2="'+(Number $y)+'" stroke="#536176" opacity="0.35"/><text x="75" y="'+(Number ($y+4))+'" font-size="11" text-anchor="end">'+(Number ($maximum*$fraction) '0.##')+'</text>')
        }
        for ($index=0; $index -lt $Scenarios.Count; $index++) {
            $center = 105 + ($index+0.5)*(1040.0/$Scenarios.Count)
            for ($series=0; $series -lt $implementations.Count; $series++) {
                $implementation = $implementations[$series]
                $row = @($Data.summary | Where-Object { $_.scenario -eq $Scenarios[$index] -and $_.implementation -eq $implementation })
                if ($row.Count -eq 0) { continue }; $row=$row[0]
                $barWidth = [Math]::Min(34, (1040.0/$Scenarios.Count)/($implementations.Count+1))
                $x = $center + ($series-($implementations.Count-1)/2.0)*($barWidth+4)-$barWidth/2
                $height = $row.$metric/$maximum*180; $y=$bottom-$height
                $error=$row."${metric}_stddev"/$maximum*180
                $null = $svg.AppendLine('<rect x="'+(Number $x)+'" y="'+(Number $y)+'" width="'+(Number $barWidth)+'" height="'+(Number $height)+'" fill="'+$colors[$implementation]+'"/>')
                $cx=$x+$barWidth/2
                $null = $svg.AppendLine('<line x1="'+(Number $cx)+'" x2="'+(Number $cx)+'" y1="'+(Number ($y-$error))+'" y2="'+(Number ($y+$error))+'" stroke="#dce5f2"/><text x="'+(Number $cx)+'" y="'+(Number ($y-$error-6))+'" font-size="10" text-anchor="middle">'+(Number $row.$metric '0.##')+'</text>')
            }
            $wordIndex=0
            foreach ($word in $Scenarios[$index].Split('_')) { $null=$svg.AppendLine('<text x="'+(Number $center)+'" y="'+($bottom+19+$wordIndex*14)+'" font-size="11" text-anchor="middle">'+(Escape $word)+'</text>'); $wordIndex++ }
        }
        $panel++
    }
    $legendIndex=0
    foreach ($implementation in $implementations) { $x=45+$legendIndex*180; $null=$svg.AppendLine('<rect x="'+$x+'" y="1045" width="16" height="16" fill="'+$colors[$implementation]+'"/><text x="'+($x+25)+'" y="1058" font-size="14">'+$implementation+'</text>'); $legendIndex++ }
    $null=$svg.AppendLine('<text x="45" y="1090" font-size="12">'+(Escape "$Repeats repeats x ${Seconds}s after ${Warmup}s warmup · mean bars, repeat standard deviation · near-idle CPU can be noise")+'</text></g></svg>')
    [IO.File]::WriteAllText((Join-Path $OutputDirectory 'comparison.svg'),$svg.ToString())
    $rows=New-Object Text.StringBuilder
    foreach ($row in $Data.summary) { $null=$rows.AppendLine('<tr><td>'+(Escape $row.scenario)+'</td><td>'+$row.implementation+'</td><td>'+(Number $row.cpu_one_core_percent)+'</td><td>'+(Number $row.private_mean_mib '0.0')+'</td><td>'+(Number $row.rss_mean_mib '0.0')+'</td><td>'+(Number $row.startup_seconds)+'</td><td>'+(Number $row.cpu_sample_p95)+'</td></tr>') }
    $limitations=($Data.limitations | ForEach-Object {'<li>'+(Escape $_)+'</li>'}) -join ''
    $html='<!doctype html><html><head><meta charset="utf-8"><title>MusicApp measured benchmark</title><style>body{font:15px Segoe UI,Arial,sans-serif;background:#141820;color:#dce5f2;max-width:1200px;margin:30px auto;padding:20px}img{width:100%;height:auto}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:9px;border-bottom:1px solid #445167;text-align:left}p,li{line-height:1.6;color:#b8c5d8}a{color:#00d8b3}</style></head><body><h1>MusicApp benchmark measurements</h1><p>'+(Escape $subjectText)+'</p><img src="comparison.svg" alt="Measured CPU, private memory and startup charts"><table><thead><tr><th>State</th><th>Subject</th><th>CPU % / core</th><th>Private MiB</th><th>Working set MiB</th><th>Startup s</th><th>CPU sample p95</th></tr></thead><tbody>'+$rows+'</tbody></table><h2>Scope and limits</h2><ul>'+$limitations+'</ul><p><a href="results.json">Raw JSON</a> · <a href="summary.csv">Summary CSV</a> · <a href="samples.csv">Every sample</a></p></body></html>'
    [IO.File]::WriteAllText((Join-Path $OutputDirectory 'report.html'),$html)
}
function Save-Results($Data) {
    $Data.summary = @(Summarize-Runs $Data.runs)
    $Data | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'results.json') -Encoding UTF8
    if ($Data.summary.Count -gt 0) { $Data.summary | Export-Csv -LiteralPath (Join-Path $OutputDirectory 'summary.csv') -NoTypeInformation -Encoding UTF8 }
    $sampleRows=New-Object 'System.Collections.Generic.List[object]'
    foreach ($run in $Data.runs) { foreach ($sample in $run.samples) { $sampleRows.Add([pscustomobject]@{implementation=$run.implementation;scenario=$run.scenario;repeat=$run.repeat;elapsed_seconds=$sample.elapsed_seconds;cpu_one_core_percent=$sample.cpu_one_core_percent;cpu_machine_percent=$sample.cpu_machine_percent;system_cpu_percent=$sample.system_cpu_percent;private_mib=$sample.private_mib;rss_mib=$sample.rss_mib;threads=$sample.threads;processes=$sample.processes}) } }
    if ($sampleRows.Count -gt 0) { $sampleRows.ToArray() | Export-Csv -LiteralPath (Join-Path $OutputDirectory 'samples.csv') -NoTypeInformation -Encoding UTF8 }
}

$media=Join-Path $OutputDirectory 'benchmark.wav'
[MusicAppBenchNative]::WriteWave($media,[Math]::Max(20,$Seconds+$Warmup+5))
$data=[ordered]@{
    schema_version=2; created_utc=[DateTime]::UtcNow.ToString('o'); orchestration='PowerShell and Windows/.NET; no additional runtime dependency'
    configuration=@{repeats=$Repeats;seconds=$Seconds;warmup=$Warmup;interval=$Interval;scenarios=$Scenarios;baseline_supplied=[bool]$BaselineExecutable;media='generated 44100Hz stereo 16bit PCM WAV'}
    machine=@{os=[Environment]::OSVersion.VersionString;logical_processors=[Environment]::ProcessorCount;framework=[Environment]::Version.ToString();powershell=$PSVersionTable.PSVersion.ToString()}
    artifacts=@{rust_executable=$RustExe;rust_executable_sha256=(Get-FileHash -LiteralPath $RustExe -Algorithm SHA256).Hash;orchestrator_sha256=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash}
    limitations=@('Only measured subjects are reported; an absent baseline has no invented values.','All observed descendants are included, including the separate FFmpeg decoder.','CPU 100% means one fully busy logical processor. Near-idle differences can be scheduling noise.','Private committed memory and working set are both recorded; summing working sets can double count shared pages.','Global hooks, network, accounts, downloads, game fullscreen behavior and GPU use are excluded.','This generated PCM workload does not measure compressed-codec, library-scan or remote-stream variability.','Short-lived child CPU before observation can be missed. Startup uses a warm filesystem cache.','Each trial uses fresh project-local settings. Optional external baselines must implement the same workload protocol.')
    runs=@(); errors=@(); summary=@()
}
if ($BaselineExecutable) { $data.artifacts.baseline_executable=$BaselineExecutable; $data.artifacts.baseline_executable_sha256=(Get-FileHash -LiteralPath $BaselineExecutable -Algorithm SHA256).Hash }
$subjects=if($BaselineExecutable){2}else{1};$total=$Scenarios.Count*$Repeats*$subjects;$completed=0
try {
    for($repeat=1;$repeat-le$Repeats;$repeat++) {
        foreach($scenario in $Scenarios) {
            $order=if(-not$BaselineExecutable){@('Rust')}elseif($repeat%2){@('Baseline','Rust')}else{@('Rust','Baseline')}
            foreach($implementation in $order) {
                $completed++;Write-Host "[$completed/$total] $scenario / $implementation / repeat $repeat"
                try { $result=Run-Trial $implementation $scenario $repeat $media;$data.runs+=@($result);Write-Host ('  {0}% core, {1} MiB private, startup {2}s' -f (Number $result.cpu_one_core_percent),(Number $result.private_mean_mib '0.0'),(Number $result.startup_seconds)) }
                catch { $data.errors+=@([pscustomobject]@{scenario=$scenario;implementation=$implementation;repeat=$repeat;error=$_.Exception.Message});Write-Warning $_.Exception.Message }
                Save-Results $data
            }
        }
    }
    if($data.runs.Count-gt0){Write-Charts $data}
} finally { if(Test-Path -LiteralPath $media){Remove-Item -LiteralPath $media} }
Write-Host "Results: $OutputDirectory"
if($data.errors.Count-gt0){throw "$($data.errors.Count) trial(s) failed. See results.json and per-trial stderr logs."}
