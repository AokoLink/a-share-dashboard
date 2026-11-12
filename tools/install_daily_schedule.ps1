param(
    [Parameter(Mandatory=$true)][string]$Python,
    [switch]$Install
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$pythonWindowless = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw 'pythonw.exe is required for a hidden scheduled task.' }
$dataRoot = Join-Path $projectRoot '_analysis\daily_pipeline'
$databasePath = Join-Path $projectRoot 'data\market.db'
$taskName = 'Stock-Daily-Candidates'
$arguments = '-X utf8 -m pipeline.daily_scheduler --once --root "' + $dataRoot + '" --db "' + $databasePath + '"'
$plan = [ordered]@{ task=$taskName; executable=$pythonWindowless; arguments=$arguments; working_directory=$projectRoot;
    schedule='Daily 15:05 to 23:00, every 5 minutes; also at user logon'; user=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name;
    constraint='Computer must be on and this user logged on; runner checks the exchange calendar.' }
if (-not $Install) { $plan | ConvertTo-Json; return }
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Actions.Count -ne 1 -or $existing.Actions[0].WorkingDirectory -ne $projectRoot -or
        $existing.Actions[0].Arguments -ne $arguments -or $existing.Actions[0].Execute -ne $pythonWindowless) {
        throw 'A different scheduled task already uses this name. It has not been replaced.'
    }
}
$daily = New-ScheduledTaskTrigger -Daily -At '15:05'
$repetition = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(15).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 7 -Minutes 55)
$daily.Repetition = $repetition.Repetition
$logon = New-ScheduledTaskTrigger -AtLogOn -User $plan.user
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument $arguments -WorkingDirectory $projectRoot
$principal = New-ScheduledTaskPrincipal -UserId $plan.user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($daily,$logon) -Settings $settings -Principal $principal -Description 'Stock daily candidates; persistent shared lock, calendar gate and bounded retries.' -Force | Out-Null
$installed = Get-ScheduledTask -TaskName $taskName
$plan['state'] = [string]$installed.State
$plan['installed'] = $true
$plan | ConvertTo-Json
