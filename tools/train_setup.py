#!/usr/bin/env python3
"""학습 준비 — 학습용 파이썬 환경을 만들고, 교회 유튜브 채널의 영상 목록을 받는다.

[학습_1_준비.bat] 이 이 파일을 시스템 파이썬으로 돌린다. 표준 라이브러리만 쓴다
(아직 아무것도 깔리지 않은 PC 에서 돌아야 하므로).

하는 일:
    1. 채널 영상 목록 받기 (영상은 받지 않는다 — 제목과 길이만)  → data/train/videos.json
    2. 학습용 파이썬 환경 .venv-train 만들기
       설교 편집기의 .venv 와 따로 둔다. 학습에는 torch(약 3GB)가 필요한데, CLAUDE.md 가
       편집기 실행에는 torch 를 쓰지 않기로 정했기 때문이다.
    3. 그래픽카드로 학습·자막이 되는지 확인                     → data/train/setup_report.txt

멈췄다가 다시 돌려도 된다. 이미 깔린 것은 pip 가 건너뛴다.
받는 파일(파이썬 꾸러미 캐시·모델)은 모두 이 폴더의 data/train 아래에 둔다 — C: 드라이브를 채우지 않게.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAIN = ROOT / "data" / "train"
VENV = ROOT / ".venv-train"
VPY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
LOG = TRAIN / "setup_log.txt"
CHANNEL = "https://www.youtube.com/channel/UChEuQBM25zkthvIhVkdtx3Q"   # 창의진교회
# torch 는 그래픽카드(CUDA) 판을 받아야 한다. RTX 50 계열(블랙웰)은 CUDA 12.8 이상 판이어야 돈다.
TORCH_INDEXES = ("cu128", "cu130", "cu129")


def say(text: str = "") -> None:
    print(text, flush=True)
    TRAIN.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")


def env() -> dict:
    e = os.environ.copy()
    e["PIP_CACHE_DIR"] = str(TRAIN / "pip-cache")
    e["HF_HOME"] = str(TRAIN / "hf")
    e["PYTHONUTF8"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    e["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    bin_dir = ROOT / "bin"
    e["PATH"] = os.pathsep.join([str(bin_dir), e.get("PATH", "")])
    return e


def run(cmd: list[str], quiet: bool = False) -> int:
    """명령을 돌리며 나오는 글을 그대로 보여 주고 기록에도 남긴다."""
    say("  > " + " ".join(str(c) for c in cmd)[:200])
    proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env(), cwd=str(ROOT))
    with LOG.open("a", encoding="utf-8") as fh:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not quiet:
                print("    " + line, flush=True)
            fh.write("    " + line + "\n")
    return proc.wait()


# ------------------------------------------------------------------ 1. 목록
def yt_dlp() -> str:
    exe = ROOT / "bin" / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
    if exe.exists():
        return str(exe)
    if VPY.exists():
        return str(VENV / ("Scripts/yt-dlp.exe" if os.name == "nt" else "bin/yt-dlp"))
    return "yt-dlp"


def list_channel() -> None:
    say("[1/3] 교회 유튜브 채널의 영상 목록을 받습니다 (영상은 받지 않습니다)")
    exe = yt_dlp()
    run([exe, "-U"], quiet=True)          # 유튜브가 자주 바뀌어 받기 프로그램을 먼저 새로 한다
    seen: dict[str, dict] = {}
    for tab in ("videos", "streams"):
        try:
            out = subprocess.run([exe, "-J", "--flat-playlist", f"{CHANNEL}/{tab}"],
                                 capture_output=True, env=env(), timeout=600)
        except Exception as exc:                         # 시간 초과 등
            say(f"  {tab} 목록을 받지 못했습니다 ({exc})")
            continue
        if out.returncode != 0:
            msg = out.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            say(f"  {tab} 목록을 받지 못했습니다: {msg[0][:200]}")
            continue
        try:
            data = json.loads(out.stdout.decode("utf-8", "replace"))
        except ValueError:
            say(f"  {tab} 목록을 읽지 못했습니다")
            continue
        n = 0
        for e in data.get("entries") or []:
            if not e or not e.get("id") or e["id"] in seen:
                continue
            seen[e["id"]] = {
                "id": e["id"],
                "title": e.get("title") or "",
                "duration": float(e.get("duration") or 0.0),
                "tab": tab,
                "live_status": e.get("live_status") or "",
                "url": f"https://www.youtube.com/watch?v={e['id']}",
            }
            n += 1
        say(f"  {tab}: {n}편")
    videos = list(seen.values())
    (TRAIN / "videos.json").write_text(json.dumps({
        "channel": CHANNEL, "listed_at": datetime.now().isoformat(timespec="seconds"),
        "videos": videos}, ensure_ascii=False, indent=1), encoding="utf-8")
    mins = [v["duration"] / 60 for v in videos if v["duration"]]
    short = sum(1 for m in mins if m < 15)
    mid = sum(1 for m in mins if 15 <= m <= 70)
    long = sum(1 for m in mins if m > 70)
    say(f"  모두 {len(videos)}편 — 15분 미만 {short} · 15~70분 {mid} · 70분 넘음 {long}"
        f" · 길이 모름 {len(videos) - len(mins)}")


# ------------------------------------------------------------------ 2. 환경
def make_venv() -> bool:
    say("[2/3] 학습용 파이썬 환경을 만듭니다 (처음 한 번만, 10~20분)")
    say("  그래픽카드용 학습 도구(torch)가 약 3GB 라 시간이 걸립니다. 글이 계속 올라가면 잘 되는 겁니다.")
    if not VPY.exists():
        if run([sys.executable, "-m", "venv", str(VENV)]) != 0 or not VPY.exists():
            say("  파이썬 환경을 만들지 못했습니다. 파이썬 3.12 가 깔려 있는지 확인해 주세요.")
            return False
    run([VPY, "-m", "pip", "install", "--upgrade", "pip"], quiet=True)

    have_torch = subprocess.run([str(VPY), "-c", "import torch; assert torch.cuda.is_available()"],
                                capture_output=True, env=env()).returncode == 0
    if have_torch:
        say("  torch(그래픽카드판)는 이미 깔려 있습니다")
    else:
        for tag in TORCH_INDEXES:
            say(f"  torch 받는 중 (CUDA 판 {tag})")
            code = run([VPY, "-m", "pip", "install", "--upgrade", "torch",
                        "--index-url", f"https://download.pytorch.org/whl/{tag}"])
            ok = subprocess.run([str(VPY), "-c", "import torch; assert torch.cuda.is_available()"],
                                capture_output=True, env=env()).returncode == 0
            if code == 0 and ok:
                break
            say(f"  {tag} 판으로는 그래픽카드가 잡히지 않았습니다 — 다음 판을 시도합니다")
        else:
            say("  그래픽카드용 torch 를 깔지 못했습니다. 기록(data/train/setup_log.txt)을 Claude 에게 보여 주세요.")
    say("  나머지 도구를 깝니다")
    if run([VPY, "-m", "pip", "install", "-r", str(ROOT / "tools" / "train_requirements.txt")]) != 0:
        say("  도구를 다 깔지 못했습니다. 인터넷 연결을 확인하고 다시 실행해 주세요.")
        return False
    return True


# ------------------------------------------------------------------ 3. 확인
def check() -> None:
    say("[3/3] 그래픽카드로 학습과 자막이 되는지 확인합니다 (처음엔 자막 모델 1.5GB 를 받습니다)")
    report = TRAIN / "setup_report.txt"
    report.write_text(f"확인한 때: {datetime.now():%Y-%m-%d %H:%M}\n", encoding="utf-8")
    for what in ("check-torch", "check-whisper"):
        run([VPY, str(ROOT / "tools" / "collect.py"), what])


def main() -> int:
    began = time.monotonic()
    say("=" * 60)
    say(" 학습 준비 — 이 창을 닫지 마세요")
    say("=" * 60)
    list_channel()
    if not make_venv():
        return 1
    check()
    say("")
    say(f"끝났습니다 ({(time.monotonic() - began) / 60:.0f}분). 이 창을 닫고 Claude 에게 '준비 끝' 이라고 알려 주세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
