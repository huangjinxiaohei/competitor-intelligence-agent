[CmdletBinding()]
param(
    [string]$TaskName = 'CompetitorIntelDaily',
    [string]$Time = '09:00',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$command = '"{0}" -m competitor_agent.cli run' -f $python

if (-not $Apply) {
    Write-Host "Plan only: task '$TaskName' will run daily at $Time Asia/Shanghai. Re-run with -Apply to register it."
    exit 0
}

$action = New-ScheduledTaskAction -Execute $python -Argument '-m competitor_agent.cli run' -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Description 'Competitor intelligence daily run (Asia/Shanghai 09:00 by default).' -Force
Write-Host "Registered '$TaskName' for $Time. Windows uses the computer time zone; set it to Asia/Shanghai for Beijing-time execution."
