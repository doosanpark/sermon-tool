#!/usr/bin/env python3
"""PyInstaller 로 exe 를 만든다.

    python build_exe.py            # lite  — 편집·다운로드 전용 (약 200~300MB)
    python build_exe.py --full     # full  — AI 분석까지 포함 (3~6GB, 빌드 20~40분)

lite 는 torch·pyannote·faster-whisper 를 넣지 않는다. 영상 받기 / 구간 편집 /
자르기 / 내보내기는 전부 되고, [구간 찾기] 버튼만 "AI 기능 없음" 안내를 낸다.
대부분의 사용자에게는 lite 로 충분하다.

결과: dist\\SermonTool\\SermonTool.exe  (원폴더 방식 — 설치본으로 묶기 좋다)
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "SermonTool"

# AI 분석에 필요한, PyInstaller 가 스스로 못 찾는 패키지들
# full 빌드에서만 넣는 무거운 것들 (화자 분리용). lite 에서는 제외한다.
AI_COLLECT = [
    "torch", "torchaudio", "pyannote", "pyannote.audio",
    "lightning", "lightning_fabric", "pytorch_lightning",
    "asteroid_filterbanks", "speechbrain", "transformers",
    "sentencepiece", "einops", "omegaconf", "hydra",
    "torchmetrics", "soundfile",
]

# yt_dlp 는 절대 collect-all 하지 않는다. 추출기 모듈이 수천 개라 PyInstaller 의
# 바이트코드 스캐너가 IndexError 로 죽는다. 어차피 우리는 yt-dlp.exe 를 외부
# 실행파일로 호출하므로 bin\yt-dlp.exe 만 함께 묶으면 된다.
# PySide6 도 collect-all 하지 않는다. PyInstaller 에 전용 훅이 이미 있어서
# Qt 플러그인·DLL 을 알아서 챙기는데, collect-all 을 얹으면 Qt 개발도구 모듈까지
# 전부 스캔하다가 modulegraph 가 IndexError 로 죽는다.
GUI_COLLECT: list[str] = []

# lite 빌드 구성.
#
# --collect-all 은 그 패키지의 모든 하위 모듈을 하나씩 스캔한다. av, tokenizers,
# huggingface_hub 처럼 큰 패키지에 쓰면 PyInstaller 의 바이트코드 파서가
# IndexError 로 죽는다. 그래서 꼭 필요한 것만 collect-all 하고, 나머지는
# 데이터·바이너리만 가져오거나 hidden-import 로 이름만 알려 준다.

LITE_COLLECT_ALL = ["sherpa_onnx", "numpy"]              # 검증된 조합
LITE_COLLECT_DATA = ["faster_whisper"]                   # 번들 설정·모델 파일
LITE_COLLECT_BINARIES = ["ctranslate2", "onnxruntime"]   # DLL
LITE_HIDDEN = ["faster_whisper", "ctranslate2", "av", "tokenizers",
               "huggingface_hub", "onnxruntime"]

GUI_COLLECT: list[str] = []

# lite 빌드에 넣는 것들.
#   sherpa_onnx : 성문 검출 (ONNX, CPU)
#   faster_whisper + ctranslate2 : 자막. torch 가 아니라 CTranslate2 기반이라
#                                   torch 없이도 돌아간다. 모델은 첫 사용 시 내려받는다.
LITE_INCLUDE = ["sherpa_onnx", "numpy", "faster_whisper", "ctranslate2",
                "tokenizers", "huggingface_hub", "av", "onnxruntime"]
NEVER_COLLECT = ["yt_dlp", "torchcodec", "tkinter", "matplotlib", "PIL", "IPython", "pytest"]


def installed(name: str) -> bool:
    """설치돼 있고 import 가능한 패키지만 --collect-all 에 넘긴다.

    없는 패키지를 넘기면 PyInstaller 가 바로 실패한다.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def filter_installed(names: list[str]) -> tuple[list[str], list[str]]:
    have, missing = [], []
    for n in names:
        (have if installed(n) else missing).append(n)
    return have, missing


def check_python() -> None:
    """정식 출시판인지 확인한다.

    Python 3.10.0rc1 같은 출시 후보(rc/beta)에는 dis 모듈 버그가 있어
    PyInstaller 가 어떤 패키지 조합으로도 IndexError 로 죽는다.
    """
    info = sys.version_info
    if info.releaselevel == "final":
        return
    print("=" * 62)
    print(f"이 파이썬은 정식판이 아닙니다: {sys.version.split()[0]}")
    print(f"  경로: {sys.executable}")
    print()
    print("출시 후보(rc)·베타판에는 PyInstaller 가 쓰는 dis 모듈에 버그가 있어")
    print("빌드가 IndexError 로 실패합니다. 정식판에서 빌드하세요.")
    print()
    print("  winget install -e --id Python.Python.3.12")
    print("  (새 터미널을 연 뒤)")
    print("  cd D:\\homily\\sermon-tool")
    print("  py -3.12 -m venv .venv312")
    print("  .venv312\\Scripts\\activate")
    print("  pip install PySide6 sherpa-onnx numpy faster-whisper pyinstaller")
    print("  python build_exe.py --clean")
    print("=" * 62)
    raise SystemExit(1)


def check_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller 가 없습니다.  pip install pyinstaller")
        raise SystemExit(1)


def build(full: bool, console: bool, clean: bool) -> Path:
    check_python()
    check_pyinstaller()
    sep = ";" if sys.platform == "win32" else ":"   # --add-data 구분자

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", APP_NAME,
        "--windowed" if not console else "--console",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    if clean:
        args.append("--clean")

    icon = ROOT / "assets" / "app.ico"
    if icon.exists():
        args += ["--icon", str(icon)]

    # bin\ 의 외부 실행파일(ffmpeg, ffprobe, yt-dlp)을 통째로 묶는다
    bin_dir = ROOT / "bin"
    models = sorted(f.name for f in (bin_dir / "models").glob("*.onnx"))
    if models:
        print(f"모델 포함: {', '.join(models)}")
    exes = sorted(f.name for f in bin_dir.glob("*.exe"))
    llama = bin_dir / "llama"
    if exes or models:
        # bin\ 을 통째로 넣으므로 bin\llama\ 도 함께 들어간다
        args += ["--add-data", f"{bin_dir}{sep}bin"]
        print(f"bin\\ 포함: {', '.join(exes)}")
        if (llama / "llama-server.exe").exists():
            size = sum(f.stat().st_size for f in llama.iterdir()) / 1e6
            print(f"bin\\llama\\ 포함: llama-server + DLL "
                  f"{len(list(llama.iterdir()))}개 ({size:.0f}MB) — 3분 요약 AI 고르기용")
    else:
        print("주의: bin\\ 이 비어 있습니다. 사용자가 ffmpeg 와 yt-dlp 를 따로 깔아야 합니다.")

    vp = ROOT / "data" / "voiceprint.npz"
    if vp.exists():
        args += ["--add-data", f"{vp}{sep}."]
        print(f"기본 성문 포함: {vp.name} ({vp.stat().st_size/1024:.0f} KB)")
    else:
        print("주의: data/voiceprint.npz 가 없습니다. 설치본에서 자동 찾기가 동작하지 않습니다.")
        print("      python enroll.py build  로 먼저 만드세요.")

    for pkg in NEVER_COLLECT:
        if installed(pkg):
            args += ["--exclude-module", pkg]

    if not installed("PySide6"):
        print("PySide6 가 없습니다.  pip install PySide6")
        raise SystemExit(1)
    for pkg in GUI_COLLECT:
        if installed(pkg):
            args += ["--collect-all", pkg]
    for pkg in LITE_COLLECT_ALL:
        if installed(pkg):
            args += ["--collect-all", pkg]
            print(f"포함(전체): {pkg}")
    for pkg in LITE_COLLECT_DATA:
        if installed(pkg):
            args += ["--collect-data", pkg]
            print(f"포함(데이터): {pkg}")
    for pkg in LITE_COLLECT_BINARIES:
        if installed(pkg):
            args += ["--collect-binaries", pkg]
    for pkg in LITE_HIDDEN:
        if installed(pkg):
            args += ["--hidden-import", pkg]

    print("자막 기능 포함" if installed("faster_whisper") else
          "자막 기능 없음 — pip install faster-whisper 후 다시 빌드하면 들어갑니다")

    if full:
        have, missing = filter_installed(AI_COLLECT)
        for pkg in have:
            args += ["--collect-all", pkg]
        print(f"포함: {', '.join(have) or '(없음)'}")
        if missing:
            print(f"건너뜀(미설치): {', '.join(missing)}")
        if not installed("torch"):
            print("\ntorch 가 없어 full 빌드의 의미가 없습니다. lite 로 빌드하세요.")
            raise SystemExit(1)
    else:
        for pkg in AI_COLLECT:
            base = pkg.split(".")[0]
            if installed(base):
                args += ["--exclude-module", base]

    args.append(str(ROOT / "app.py"))

    print(f"\n=== {'FULL' if full else 'LITE'} 빌드 시작 ===")
    print("몇 분 걸립니다. 경고 메시지가 많이 나오는 것은 정상입니다.\n")
    proc = subprocess.run(args)
    if proc.returncode != 0:
        print("\n빌드 실패. 위 PyInstaller 출력의 마지막 부분을 확인하세요.")
        raise SystemExit(proc.returncode)

    out = ROOT / "dist" / APP_NAME
    print(f"\n완료: {out}")
    if out.exists():
        size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
        print(f"크기: {size/1024/1024:.0f} MB")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="SermonTool exe 빌드")
    ap.add_argument("--full", action="store_true", help="AI 분석 포함 (torch·pyannote·whisper)")
    ap.add_argument("--console", action="store_true", help="콘솔 창 표시 (디버깅용)")
    ap.add_argument("--clean", action="store_true", help="이전 빌드 캐시 삭제")
    args = ap.parse_args()

    if args.clean:
        for d in ("build", "dist"):
            shutil.rmtree(ROOT / d, ignore_errors=True)

    build(args.full, args.console, args.clean)
    print("\n설치본을 만들려면 Inno Setup 으로 installer.iss 를 컴파일하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
