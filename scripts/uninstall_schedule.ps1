[CmdletBinding()]
param(
    [string]$TaskName = 'CompetitorIntelDaily',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
if (-not $Apply) {
    Write-Host "Plan only: task '$TaskName' will be removed. Re-run with -Apply to remove it."
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
Write-Host "Removed scheduled task '$TaskName'."
