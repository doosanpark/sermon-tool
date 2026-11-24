#!/usr/bin/env python3
"""새 PC 에서 빌드할 수 있게 준비한다 — GitHub 에 없는 것을 채운다.

    python tools/prepare_build.py            없는 것을 내려받고 / 배포자료\\ 에서 가져온다
    python tools/prepare_build.py --check    받지 않고 무엇이 있고 없는지만 본다
    python tools/prepare_build.py --pack     이 PC 에 있는 것을 dist\\배포자료\\ 로 모은다
                                             (다른 PC 로 옮길 때 이 폴더만 복사하면 된다.
                                              build_all.bat 이 끝에 자동으로 만들기도 한다)

왜 이런 스크립트가 필요한가:
    GitHub 에는 소스만 있다. 아래는 올라가지 않는다.
      - bin\\ffmpeg.exe, ffprobe.exe, yt-dlp.exe, deno.exe  (너무 크고, 공식 배포처가 있다)
      - AI 모델 gguf (2.5GB, GitHub 한 파일 100MB 한도)
      - data\\voiceprint.npz  목사님 성문 (사람의 생체정보 — 공개 저장소에 올리지 않는다)
      - data\\고칠말.txt
    앞의 둘은 공식 주소에서 내려받고, 뒤의 셋은 배포자료\\ 폴더에서 가져온다.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
DATA = ROOT / "data"
# 가져올 자료가 있는 곳. 배포자료\\시작하기.bat 이 SERMON_PACK_DIR 로 그 폴더를 알려 준다 —
# 2.6GB 를 프로젝트 안으로 또 복사하지 않아도 된다.
PACK = Path(os.environ.get("SERMON_PACK_DIR") or ROOT / "배포자료")
PACK_OUT = ROOT / "dist" / "배포자료"       # --pack 이 만드는 곳
OLD_PACK = ROOT / "옮길것_AI모델"         # 전에 모아 둔 폴더 — 있으면 같이 찾는다

UA = {"User-Agent": "SermonToolBuild/1.0"}
# 공식 배포처의 '최신' 주소. 고정 주소라 버전이 바뀌어도 그대로 쓸 수 있다.
FFMPEG_ZIP = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-gpl.zip")
YTDLP_EXE = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
DENO_ZIP = ("https://github.com/denoland/deno/releases/latest/download/"
            "deno-x86_64-pc-windows-msvc.zip")

results: list[tuple[str, str, str]] = []     # (상태, 이름, 설명)


def note(state: str, name: str, text: str = "") -> None:
    results.append((state, name, text))
    mark = {"OK": "[OK  ]", "받음": "[받음]", "없음": "[없음]", "주의": "[주의]"}.get(state, f"[{state}]")
    print(f"  {mark} {name}" + (f" — {text}" if text else ""), flush=True)


def _present(path: Path, min_bytes: int = 1000) -> bool:
    return path.is_file() and path.stat().st_size >= min_bytes


def download(url: str, dst: Path, label: str) -> bool:
    """dst 로 내려받는다. 중간에 끊기면 반쪽 파일을 남기지 않는다."""
    tmp = dst.with_suffix(dst.suffix + ".part")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got, shown = 0, -1
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    pct = got * 100 // total
                    if pct // 10 != shown:
                        shown = pct // 10
                        print(f"    {label} {pct}% ({got / 1e6:.0f}/{total / 1e6:.0f}MB)", flush=True)
        tmp.replace(dst)
        return True
    except Exception as exc:                      # 네트워크 오류는 종류가 많다
        tmp.unlink(missing_ok=True)
        print(f"    내려받지 못했습니다: {type(exc).__name__}: {exc}")
        return False


def _works(exe: Path, flag: str) -> bool:
    try:
        r = subprocess.run([str(exe), flag], capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _extract(zpath: Path, wanted: dict[str, Path]) -> None:
    """zip 안에서 이름이 wanted 의 키로 끝나는 파일을 꺼내 대상 경로에 쓴다."""
    with zipfile.ZipFile(zpath) as z:
        for member in z.namelist():
            base = member.rsplit("/", 1)[-1].lower()
            if base in wanted and not member.endswith("/"):
                wanted[base].parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as src, open(wanted[base], "wb") as dst:
                    shutil.copyfileobj(src, dst)


# ------------------------------------------------------------------ bin\
def ensure_tools(fetch: bool) -> None:
    print("\n[1] bin\\ 의 실행파일 (ffmpeg · yt-dlp · deno)")
    need_ff = [BIN / "ffmpeg.exe", BIN / "ffprobe.exe"]
    if all(_present(p, 1_000_000) for p in need_ff):
        note("OK", "ffmpeg · ffprobe")
    elif not fetch:
        note("없음", "ffmpeg · ffprobe", "준비 단계에서 내려받습니다 (약 200MB)")
    else:
        print("  ffmpeg 를 내려받습니다 (약 200MB, 몇 분 걸립니다)")
        z = BIN / "_ffmpeg.zip"
        if download(FFMPEG_ZIP, z, "ffmpeg"):
            try:
                _extract(z, {"ffmpeg.exe": BIN / "ffmpeg.exe", "ffprobe.exe": BIN / "ffprobe.exe"})
            except (zipfile.BadZipFile, OSError) as exc:
                print(f"    압축을 풀지 못했습니다: {exc}")
            z.unlink(missing_ok=True)
        ok = all(_present(p, 1_000_000) for p in need_ff) and _works(need_ff[0], "-version")
        note("받음" if ok else "없음", "ffmpeg · ffprobe",
             "" if ok else "수동: https://github.com/BtbN/FFmpeg-Builds/releases 에서 받아 bin\\ 에 넣으세요")

    ytd = BIN / "yt-dlp.exe"
    if _present(ytd, 1_000_000):
        note("OK", "yt-dlp")
    elif not fetch:
        note("없음", "yt-dlp")
    else:
        ok = download(YTDLP_EXE, ytd, "yt-dlp") and _works(ytd, "--version")
        note("받음" if ok else "없음", "yt-dlp",
             "" if ok else "수동: https://github.com/yt-dlp/yt-dlp/releases")

    deno = BIN / "deno.exe"
    if _present(deno, 1_000_000):
        note("OK", "deno", "yt-dlp 가 유튜브 주소를 풀 때 씁니다")
    elif not fetch:
        note("없음", "deno")
    else:
        z = BIN / "_deno.zip"
        if download(DENO_ZIP, z, "deno"):
            try:
                _extract(z, {"deno.exe": deno})
            except (zipfile.BadZipFile, OSError) as exc:
                print(f"    압축을 풀지 못했습니다: {exc}")
            z.unlink(missing_ok=True)
        ok = _present(deno, 1_000_000) and _works(deno, "--version")
        note("받음" if ok else "없음", "deno",
             "" if ok else "수동: https://github.com/denoland/deno/releases")

    llama = BIN / "llama" / "llama-server.exe"
    note("OK" if llama.exists() else "없음", "bin\\llama",
         "" if llama.exists() else "GitHub 에 들어 있어야 합니다 — git pull 을 확인하세요")


# ------------------------------------------------------------------ 배포자료\
def _find(name_glob: str, *dirs: Path) -> Path | None:
    for d in dirs:
        if d.exists():
            hit = sorted(d.rglob(name_glob))
            if hit:
                return hit[0]
    return None


def _copy_if_missing(src: Path, dst: Path) -> bool:
    if _present(dst, 1) and dst.stat().st_size == src.stat().st_size:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def ensure_assets(fetch: bool) -> None:
    print("\n[2] 깃에 없는 자료 (성문 · 고칠말 · AI 모델)")
    for fname, label, why in (
        ("voiceprint.npz", "성문(목사님 목소리)", "없으면 설치본에서 '자동으로 설교 구간 찾기'가 안 됩니다"),
        ("고칠말.txt", "고칠말 사전", "없어도 되지만, 모아 둔 오인식 사전이 빠집니다"),
    ):
        dst = DATA / fname
        src = _find(fname, PACK)
        if src and fetch and _copy_if_missing(src, dst):
            note("받음", label, f"배포자료에서 가져왔습니다")
        elif _present(dst, 1):
            note("OK", label)
        elif src:
            note("없음", label, "배포자료에 있습니다 — 준비 단계에서 가져옵니다")
        else:
            note("없음", label, f"{why}. 배포자료\\{fname} 로 넣어 주세요")

    # 학습시킨 AI 모델
    gdst = DATA / "models" / "llm"
    have = sorted(gdst.glob("sermon-*.gguf")) if gdst.exists() else []
    if have:
        note("OK", "AI 모델(학습)", f"{have[0].name} {have[0].stat().st_size / 1e9:.1f}GB")
    else:
        src = _find("sermon-*.gguf", PACK, OLD_PACK)
        if src and fetch:
            print(f"  AI 모델을 복사합니다 ({src.stat().st_size / 1e9:.1f}GB, 잠시 걸립니다)")
            _copy_if_missing(src, gdst / src.name)
            note("받음", "AI 모델(학습)", src.name)
        elif src:
            note("없음", "AI 모델(학습)", f"{src} 에 있습니다 — 준비 단계에서 가져옵니다")
        else:
            note("주의", "AI 모델(학습)",
                 "배포자료\\ 에 sermon-*.gguf 가 없습니다. 이대로 빌드하면 모델 없는 zip 이 됩니다")

    # 뜻 보기 모델 (공개 모델이라 내려받을 수 있다)
    edir = DATA / "models" / "text-embed"
    if _present(edir / "model.onnx", 1_000_000) and _present(edir / "tokenizer.json", 1000):
        note("OK", "뜻 보기 모델")
        return
    src = _find("model.onnx", PACK)
    if src and fetch:
        for f in src.parent.iterdir():
            if f.is_file():
                _copy_if_missing(f, edir / f.name)
        note("받음", "뜻 보기 모델", "배포자료에서 가져왔습니다")
    elif fetch:
        print("  뜻 보기 모델을 내려받습니다 (약 135MB)")
        try:
            sys.path.insert(0, str(ROOT))
            from core import embed
            from core.config import Config
            embed.download(Config())
            note("받음", "뜻 보기 모델")
        except Exception as exc:
            note("없음", "뜻 보기 모델", f"내려받지 못했습니다 ({type(exc).__name__}). 받는 분이 처음 쓸 때 받게 됩니다")
    else:
        note("없음", "뜻 보기 모델", "준비 단계에서 내려받습니다")


# ------------------------------------------------------------------ 모으기
def _place(src: Path, dst: Path) -> str:
    """src 를 dst 에 놓는다. 같은 디스크면 하드링크(공간을 더 쓰지 않는다), 안 되면 복사."""
    if dst.is_file() and dst.stat().st_size == src.stat().st_size:
        return "이미"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
        return "연결"
    except OSError:
        shutil.copy2(src, dst)
        return "복사"


# 배포자료\\시작하기.bat — 영문만 쓴다. cmd.exe 는 .bat 을 OEM 코드 페이지로 읽어서
# 한글이 깨진다. 이 파일이 하는 일: 옆에 GitHub 코드를 받고(있으면 최신으로),
# 이 폴더의 자료를 코드에 합쳐 빌드 준비까지 한다.
START_BAT = r"""@echo off
setlocal
rem Run this from the pack folder (the folder that holds this file).
rem It gets the code from GitHub next to this folder, then fills in the assets from here.
rem ASCII only: cmd.exe reads .bat files in the OEM code page.

set "PACK=%~dp0"
if "%PACK:~-1%"=="\" set "PACK=%PACK:~0,-1%"
for %%I in ("%PACK%\..") do set "BASE=%%~fI"
set "PROJ=%BASE%\sermon-tool"
set "REPO=https://github.com/doosanpark/sermon-tool.git"

echo ============================================
echo  SermonTool - get the code and set up
echo ============================================
echo Pack folder : %PACK%
echo Code folder : %PROJ%
echo.

where git >nul 2>&1
if errorlevel 1 goto nogit

if exist "%PROJ%\.git" goto pull
if exist "%PROJ%" goto asis

echo Downloading the code from GitHub...
git clone "%REPO%" "%PROJ%"
if errorlevel 1 goto fail
goto prep

:pull
echo The code folder already exists. Updating it...
git -C "%PROJ%" pull --ff-only
if errorlevel 1 goto pullfail
goto prep

:asis
echo The folder exists but is not a git checkout. Using it as it is.

:prep
set "SERMON_PACK_DIR=%PACK%"
set "SERMON_NOPAUSE=1"
call "%PROJ%\build_prep.bat"
if errorlevel 1 goto fail

echo.
choice /c YN /m "Build now - zip and installer, takes a while"
if errorlevel 2 goto later
set "SERMON_NOPAUSE="
call "%PROJ%\build_all.bat"
goto end

:later
echo.
echo Later, run: %PROJ%\build_all.bat
goto end

:nogit
echo Git is not installed. Install it and run this file again:
echo   winget install -e --id Git.Git
goto fail

:pullfail
echo Could not update - the code folder has local changes.
echo Commit or discard them, or delete the folder and run again.

:fail
echo.
echo NOT DONE - see messages above.
pause
exit /b 1

:end
pause
exit /b 0
"""

README_TXT = r"""이 폴더는 설교영상 편집기를 다른 PC 에서 빌드하려고 모아 둔 자료입니다.

들어 있는 것
  voiceprint.npz   목사님 성문(목소리 정보)
  고칠말.txt        지금까지 모은 오인식 사전
  llm\             이 교회 설교로 학습한 AI 모델 (약 2.5GB)
  text-embed\      뜻 보기 모델

쓰는 법
  1. 이 폴더를 새 PC 의 아무 곳에 복사합니다 (예: D:\work\배포자료).
  2. 먼저 Python 3.12 정식판, Git, Inno Setup 6 을 설치합니다.
       winget install -e --id Python.Python.3.12
       winget install -e --id Git.Git
  3. 이 폴더의 '시작하기.bat' 을 두 번 누릅니다.
       - 이 폴더 옆(D:\work\sermon-tool)에 GitHub 코드를 받습니다.
         이미 있으면 최신으로 고칩니다.
       - 패키지, ffmpeg, yt-dlp, deno 를 설치하고 이 폴더의 자료를 코드에 합칩니다.
       - 끝에 '지금 빌드할까요' 를 묻습니다.
  4. 빌드가 끝나면 sermon-tool\dist\SermonTool_v버전.zip 이 만들어집니다.

주의
  성문은 사람의 목소리 정보이고 AI 모델은 이 교회 설교로 학습한 것입니다.
  이 교회 용도로만 쓰고, 공개된 곳(GitHub 등)에 올리지 마세요.
"""


def pack(out: Path = PACK_OUT) -> int:
    """이 PC 에 있는 자료를 dist\\배포자료\\ 로 모으고 시작하기.bat 을 넣는다."""
    print(f"\n=== 배포자료 만들기: {out} ===")
    out.mkdir(parents=True, exist_ok=True)
    plan = [
        (DATA / "voiceprint.npz", out / "voiceprint.npz"),
        (DATA / "고칠말.txt", out / "고칠말.txt"),
    ]
    edir = DATA / "models" / "text-embed"
    for f in (sorted(edir.glob("*")) if edir.exists() else []):
        plan.append((f, out / "text-embed" / f.name))
    gdir = DATA / "models" / "llm"
    tuned = sorted(gdir.glob("sermon-*.gguf")) if gdir.exists() else []
    if not tuned:
        old = _find("sermon-*.gguf", OLD_PACK)
        tuned = [old] if old else []
    for f in tuned:
        plan.append((f, out / "llm" / f.name))
    done = 0
    for src, dst in plan:
        if not src.is_file():
            print(f"  [없음] {src.name}")
            continue
        how = _place(src, dst)
        print(f"  [{how}] {dst.relative_to(out)} ({src.stat().st_size / 1e6:.0f}MB)")
        done += 1
    (out / "시작하기.bat").write_bytes(START_BAT.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))
    (out / "읽어주세요.txt").write_text(README_TXT, encoding="utf-8-sig")
    print(f"  [OK ] 시작하기.bat · 읽어주세요.txt")
    print(f"  자료 {done}개. 받는 분은 이 폴더를 복사해 가서 '시작하기.bat' 을 누르면 됩니다.")
    print("  주의: 성문은 사람의 목소리 정보입니다. 이 폴더를 GitHub 에 올리지 마세요.")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="새 PC 에서 빌드할 수 있게 준비")
    ap.add_argument("--check", action="store_true", help="받지 않고 확인만")
    ap.add_argument("--pack", action="store_true", help="이 PC 의 자료를 배포자료\\ 로 모은다")
    args = ap.parse_args()
    if args.pack:
        return pack()

    fetch = not args.check
    print("=" * 56)
    print(" 빌드 준비" + (" (확인만)" if args.check else ""))
    print("=" * 56)
    ensure_tools(fetch)
    ensure_assets(fetch)

    miss = [r for r in results if r[0] == "없음"]
    warn = [r for r in results if r[0] == "주의"]
    print("\n" + "-" * 56)
    must = {"ffmpeg · ffprobe", "yt-dlp", "deno", "bin\\llama"}
    blocking = [r for r in miss if r[1] in must]
    if blocking:
        print("빌드 전에 꼭 채워야 할 것이 남았습니다:")
        for _s, n, t in blocking:
            print(f"  - {n} {t}")
        return 1
    if miss or warn:
        print("빌드는 되지만, 아래가 빠진 채로 나옵니다:")
        for _s, n, t in miss + warn:
            print(f"  - {n}: {t}")
    else:
        print("모두 갖춰졌습니다.")
    print("\n다음: build_all.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
