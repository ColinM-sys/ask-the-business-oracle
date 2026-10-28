# Launch a long-running job outside the Claude session (hidden, via WMI) so it is not killed with session tasks.
# Usage: powershell -File launch_detached.ps1 <workdir> <args...>
param([string]$Dir, [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)
$py = "C:\Users\cmcdo\AppData\Local\Programs\Python\Python312\python.exe"
$cmd = "`"$py`" " + ($Rest -join " ")
$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow = [uint16]0}
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $cmd; CurrentDirectory = $Dir; ProcessStartupInformation = $si }
"pid=$($r.ProcessId) rc=$($r.ReturnValue) $cmd"
