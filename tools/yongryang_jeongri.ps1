# GitHub 가 받지 않는 큰 파일을 깃 추적에서 뺀다. 파일 자체는 지우지 않는다.
#   용량_정리.bat 로 실행
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$limit = 50MB

Write-Host ""
Write-Host "=== 1. 깃이 추적 중인 파일 가운데 50MB 넘는 것 ===" -ForegroundColor Cyan
$big = @()
foreach ($f in (git ls-files)) {
    if (Test-Path -LiteralPath $f) {
        $it = Get-Item -LiteralPath $f
        if ($it.Length -gt $limit) { $big += $it }
    }
}
if ($big.Count -eq 0) {
    Write-Host "  없음" -ForegroundColor Green
} else {
    foreach ($it in $big) {
        $mb = [math]::Round($it.Length / 1MB)
        Write-Host ("  {0,7}MB  {1}" -f $mb, $it.FullName.Substring($PWD.Path.Length + 1))
    }
    Write-Host ""
    Write-Host "  추적에서 뺍니다 (파일은 그대로 둡니다)…"
    foreach ($it in $big) {
        git rm --cached --quiet -- $it.FullName.Substring($PWD.Path.Length + 1)
    }
    Write-Host "  뺐습니다. 다음 커밋부터 올라가지 않습니다." -ForegroundColor Green
}

Write-Host ""
Write-Host "=== 2. 아직 올리지 않은 커밋 안에 큰 파일이 있나 ===" -ForegroundColor Cyan
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
$upstream = (git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null)
if (-not $upstream) {
    Write-Host "  원격 가지가 없어 건너뜁니다 (아직 한 번도 push 하지 않았거나 upstream 이 없음)" -ForegroundColor Yellow
} else {
    $bad = @()
    $objs = git rev-list --objects "$upstream..HEAD" 2>$null
    foreach ($line in $objs) {
        $parts = $line -split ' ', 2
        if ($parts.Count -lt 2) { continue }
        $size = [int64](git cat-file -s $parts[0])
        if ($size -gt $limit) { $bad += [pscustomobject]@{ MB = [math]::Round($size / 1MB); Path = $parts[1] } }
    }
    if ($bad.Count -eq 0) {
        Write-Host "  없음 — push 해도 됩니다" -ForegroundColor Green
    } else {
        foreach ($b in $bad) { Write-Host ("  {0,7}MB  {1}" -f $b.MB, $b.Path) -ForegroundColor Red }
        Write-Host ""
        Write-Host "  이 파일들은 이미 커밋 안에 들어 있어서 .gitignore 로는 못 막습니다." -ForegroundColor Yellow
        Write-Host "  올리지 않은 커밋을 풀어서 다시 커밋해야 합니다. 작업 내용은 그대로 남습니다:"
        Write-Host ""
        Write-Host "    git reset --soft $upstream"
        Write-Host "    git reset"
        Write-Host "    용량_정리.bat   (한 번 더 — 그러면 1번에서 빠집니다)"
        Write-Host "    git add -A && git commit -m `"...`""
        Write-Host ""
    }
}

Write-Host ""
Write-Host "=== 3. 지금 상태 ===" -ForegroundColor Cyan
git status --short | Select-Object -First 40
$n = (git status --short | Measure-Object).Count
if ($n -gt 40) { Write-Host "  … 외 $($n - 40)개" }
Write-Host ""
