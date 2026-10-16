$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    & (Join-Path $PSScriptRoot 'Build.ps1') -RuntimeOnly
    if ($LASTEXITCODE -ne 0) { throw 'Project-local runtime setup failed.' }
} finally {
    Pop-Location
}
