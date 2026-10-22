# 커밋 메모리가 시간에 따라 어떻게 변하는지 10분간 찍는다. 보기만 한다.
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$out  = Join-Path $root "data\memtrace.txt"
New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null

$L = @()
$L += "===== 커밋 메모리 10분 추적  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====="
$L += ""
$L += "{0,-10} {1,12} {2,12} {3,12} {4,10}  {5}" -f "시각", "커밋사용GB", "커밋한도GB", "프로그램합GB", "빈메모리GB", "제일큰것"
Write-Host "10분간 30초마다 잽니다. 이 창을 닫지 마세요."
Write-Host ""

$first = $null; $last = $null
for ($i = 0; $i -lt 21; $i++) {
  $os = Get-CimInstance Win32_OperatingSystem
  $used  = ($os.TotalVirtualMemorySize - $os.FreeVirtualMemory)/1MB
  $limit = $os.TotalVirtualMemorySize/1MB
  $freeRam = $os.FreePhysicalMemory/1MB
  $ps = Get-Process | Sort-Object PrivateMemorySize64 -Descending
  $sum = (($ps | Measure-Object PrivateMemorySize64 -Sum).Sum)/1GB
  $top = $ps | Select-Object -First 1
  $topTxt = "{0} {1}MB" -f $top.ProcessName, [int]($top.PrivateMemorySize64/1MB)
  $row = "{0,-10} {1,12:N1} {2,12:N1} {3,12:N1} {4,10:N1}  {5}" -f (Get-Date -Format "HH:mm:ss"), $used, $limit, $sum, $freeRam, $topTxt
  $L += $row
  Write-Host $row
  if ($null -eq $first) { $first = $used }
  $last = $used
  if ($i -lt 20) { Start-Sleep -Seconds 30 }
}

$L += ""
$grow = $last - $first
$L += ("10분 동안 커밋 사용이 {0:N1} GB 에서 {1:N1} GB 로 {2:N1} GB 바뀌었습니다 (분당 {3:N2} GB)" -f $first, $last, $grow, ($grow/10))
if ($grow -gt 1) {
  $L += "→ 설교 편집기를 닫아 둔 채로도 계속 오릅니다. 편집기와 무관하게 새는 것입니다."
} elseif ($grow -lt -1) {
  $L += "→ 오히려 줄었습니다. 무언가가 메모리를 놓아주고 있습니다."
} else {
  $L += "→ 거의 그대로입니다. 가만히 두면 새지 않는다는 뜻입니다."
}

$L | Out-File -FilePath $out -Encoding utf8
Write-Host ""
Write-Host "끝났습니다. 결과: $out"
Write-Host $L[-1]
