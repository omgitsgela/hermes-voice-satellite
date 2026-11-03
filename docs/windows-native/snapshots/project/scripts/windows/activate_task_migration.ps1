$ErrorActionPreference='Stop'
$root='C:\Users\<operator>\AI Projects\hermes-voice-satellite'
$all=@(Get-CimInstance Win32_Process)
$matches=@($all | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -like '*scripts*windows*supervise.pyw*' })
$ids=@($matches | ForEach-Object { [int]$_.ProcessId })
$roots=@($matches | Where-Object { [int]$_.ParentProcessId -notin $ids })
if ($roots.Count -gt 1) { throw 'More than one old supervisor root; refusing automatic migration' }
foreach ($item in $roots) {
    if ([int]$item.ProcessId -in @(13184,17412,31328)) { throw 'Desktop process guard' }
    Write-Output ('Stopping identified old supervisor tree PID '+$item.ProcessId)
    & taskkill.exe /PID $item.ProcessId /T /F
    if ($LASTEXITCODE -ne 0) { throw 'Old supervisor tree did not terminate cleanly' }
}
Start-ScheduledTask -TaskName 'HermesVoiceSatellite'
Write-Output 'Scheduled task start requested. Verify separate health and Desktop process checks.'
