param([string]$Source = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$package = Join-Path $PSScriptRoot 'dist/MusicApp'
$exe = Join-Path $package 'MusicApp.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Run build.bat first.' }
$report = Join-Path $package 'state/profile_import_report.json'
$process = Start-Process -FilePath $exe -ArgumentList @('--import-python-profile', ('"' + (Resolve-Path -LiteralPath $Source).Path + '"')) -WindowStyle Hidden -Wait -PassThru
if ($process.ExitCode -ne 0) { throw 'Profile import failed; the original application was not modified.' }
if (-not (Test-Path -LiteralPath $report)) { throw 'Profile import did not produce a report.' }
$result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
Write-Host ('Settings match: ' + $result.settings_match)
Write-Host ('YouTube session verified: ' + $result.youtube_session.verified)
Write-Host ('Detailed report: ' + $report)
