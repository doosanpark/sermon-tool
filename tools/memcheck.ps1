# 무엇이 윈도우 커밋 메모리를 붙잡고 있는지 본다. 보기만 하고 아무것도 고치지 않는다.
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$out  = Join-Path $root "data\memcheck.txt"
New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null

$L = @()
$L += "===== 메모리 점검  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====="
$L += ""
$os = Get-CimInstance Win32_OperatingSystem
$L += ("실제 메모리 : {0:N1} GB 중 {1:N1} GB 남음" -f ($os.TotalVisibleMemorySize/1MB), ($os.FreePhysicalMemory/1MB))
$L += ("커밋 한도   : {0:N1} GB" -f ($os.TotalVirtualMemorySize/1MB))
$L += ("남은 커밋   : {0:N1} GB" -f ($os.FreeVirtualMemory/1MB))
$used = ($os.TotalVirtualMemorySize - $os.FreeVirtualMemory)/1MB
$L += ("커밋 사용   : {0:N1} GB" -f $used)
$L += ""

$c = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
if ($c) { $L += ("C: 드라이브  : {0:N1} GB 중 {1:N1} GB 남음" -f ($c.Size/1GB), ($c.FreeSpace/1GB)) }
$pf = Get-CimInstance Win32_PageFileUsage
if ($pf) { foreach ($p in $pf) { $L += ("페이징 파일  : {0}  잡아 둔 {1} MB  쓰는 중 {2} MB" -f $p.Name, $p.AllocatedBaseSize, $p.CurrentUsage) } }
else { $L += "페이징 파일  : 없음" }
$L += ""

$L += "--- 가상 머신·컨테이너가 돌고 있나 ---"
$vm = Get-Process | Where-Object { $_.ProcessName -match "vmmem|vmwp|docker|vmware|virtualbox|vbox|qemu|wsl" }
if ($vm) { foreach ($v in $vm) { $L += ("  {0}  번호 {1}  잡은 {2} MB  실제 {3} MB" -f $v.ProcessName, $v.Id, [int]($v.PrivateMemorySize64/1MB), [int]($v.WorkingSet64/1MB)) } }
else { $L += "  (이름으로는 안 보임)" }
$svc = Get-Service | Where-Object { $_.Name -match "^(vmms|LxssManager|WslService|com.docker.service)$" -and $_.Status -eq "Running" }
if ($svc) { foreach ($s in $svc) { $L += ("  서비스 켜짐: {0}  ({1})" -f $s.Name, $s.DisplayName) } }
$L += ""

$L += "--- 윈도우 기능 (가상 머신 관련) ---"
foreach ($f in @("Microsoft-Windows-Subsystem-Linux","VirtualMachinePlatform","Microsoft-Hyper-V-All","HypervisorPlatform")) {
  $st = (Get-WindowsOptionalFeature -Online -FeatureName $f).State
  if ($st) { $L += ("  {0,-36} {1}" -f $f, $st) }
}
$L += ""

# 커밋을 WMI 쪽 숫자로도 세어 본다 (Get-Process 가 못 세는 것이 있다)
$wp = Get-CimInstance Win32_Process | Sort-Object PageFileUsage -Descending
$sum = ($wp | Measure-Object PageFileUsage -Sum).Sum / 1MB
$L += ("--- 커밋을 많이 잡은 것 25개 (전부 합쳐 {0:N1} GB · 윈도우가 말하는 사용량 {1:N1} GB) ---" -f $sum, $used)
$L += ("{0,-30} {1,8} {2,10}" -f "이름", "번호", "잡은MB")
foreach ($p in ($wp | Select-Object -First 25)) {
  $L += ("{0,-30} {1,8} {2,10}" -f $p.Name, $p.ProcessId, [int]($p.PageFileUsage/1KB))
}
$L += ""
$L += ("설명 안 되는 양 : 약 {0:N1} GB  (드라이버나 가상 머신이 잡은 몫)" -f ($used - $sum))

$L | Out-File -FilePath $out -Encoding utf8
Write-Host ""
Write-Host "결과를 저장했습니다: $out"
Write-Host ""
$L | Select-Object -First 10 | ForEach-Object { Write-Host $_ }
