$ErrorActionPreference='Stop'
$root='C:\Users\<operator>\AI Projects\hermes-voice-satellite'
try {
    $xml=[IO.File]::ReadAllText((Join-Path $root 'state\windows-task.xml'))
    Register-ScheduledTask -TaskName 'HermesVoiceSatellite' -Xml $xml -Force | Out-Null
    $task=Get-ScheduledTask -TaskName 'HermesVoiceSatellite'
    [pscustomobject]@{Name=$task.TaskName;State=[string]$task.State;User=$task.Principal.UserId;LogonType=[string]$task.Principal.LogonType;RunLevel=[string]$task.Principal.RunLevel;RestartCount=$task.Settings.RestartCount;RestartInterval=[string]$task.Settings.RestartInterval;ExecutionTimeLimit=[string]$task.Settings.ExecutionTimeLimit} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'state\task-install-result.json')
} catch {
    [pscustomobject]@{Error=$_.Exception.Message;Category=[string]$_.CategoryInfo.Category} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'state\task-install-error.json')
    Write-Error $_
    exit 1
}
