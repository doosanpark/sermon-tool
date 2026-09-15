"""공용 유틸리티."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


class ToolError(RuntimeError):
    pass


def _force_utf8_stdio() -> None:
    """윈도우에서 stdout/stderr 를 UTF-8 로 맞춘다.

    macOS/리눅스는 콘솔이 늘 UTF-8 이라 문제가 없지만, 윈도우는 출력이 파이프나
    파일로 갈 때 로캘 코드페이지(한국어 윈도우는 cp949)를 쓴다. 이 코드는 '—' 같은
    글자를 cp949 로 적을 수 없어 print() 한 줄에서 UnicodeEncodeError 로 죽는다.
    (예: `python auto.py once > log.txt`, 작업 스케줄러, 다른 프로그램이 받아 읽을 때)

    exe 를 창 모드로 묶으면 stdout 이 None 이므로 그 경우는 건너뛴다.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


_force_utf8_stdio()


def _project_root() -> Path:
    """개발 중엔 소스 폴더, exe 로 묶였을 땐 임시 해제 폴더(_MEIPASS)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


IS_FROZEN = getattr(sys, "frozen", False)
PROJECT_ROOT = _project_root()
EXE_DIR = Path(sys.executable).parent if IS_FROZEN else PROJECT_ROOT
LOCAL_BIN = PROJECT_ROOT / "bin"


def _candidate_dirs() -> list[Path]:
    """PATH에 없어도 ffmpeg/ffprobe가 있을 만한 곳들."""
    dirs: list[Path] = [LOCAL_BIN, EXE_DIR / "bin", EXE_DIR]

    env_dir = os.environ.get("SERMON_FFMPEG_DIR")
    if env_dir:
        dirs.append(Path(env_dir))

    localappdata = os.environ.get("LOCALAPPDATA", "")
    programfiles = os.environ.get("ProgramFiles", r"C:\Program Files")

    fixed = [
        Path(r"C:\ffmpeg\bin"),
        Path(programfiles) / "ffmpeg" / "bin",
    ]
    if localappdata:
        fixed.append(Path(localappdata) / "Microsoft" / "WinGet" / "Links")
    dirs.extend(fixed)

    # winget / chocolatey 설치본은 버전 폴더 아래에 들어간다
    globs = []
    if localappdata:
        globs.append((Path(localappdata) / "Microsoft" / "WinGet" / "Packages", "*FFmpeg*/**/bin"))
    globs.append((Path(r"C:\ProgramData\chocolatey\lib"), "ffmpeg*/tools/**/bin"))
    for base, pattern in globs:
        if base.exists():
            try:
                dirs.extend(p for p in base.glob(pattern) if p.is_dir())
            except OSError:
                pass
    return dirs


def find_tool(name: str) -> str | None:
    """프로젝트 bin/ → PATH → 흔한 설치 경로 → imageio-ffmpeg 순으로 찾는다.

    bin/ 을 PATH 보다 앞에 두는 이유: 개발 PC 에 따로 깔린 오래된 ffmpeg 가 먼저
    잡히면, tool_env() 로 bin/ 을 PATH 앞에 얹어 주는 서브프로세스(yt-dlp 의 병합)와
    파이썬 쪽이 서로 다른 ffmpeg 를 쓰게 된다. 동봉한 판으로 통일해야 교회 PC 와
    같은 결과가 나온다.
    """
    for d in (LOCAL_BIN, EXE_DIR / "bin"):
        for suffix in ("", ".exe"):
            p = d / f"{name}{suffix}"
            if p.exists():
                return str(p)

    exe = shutil.which(name)
    if exe:
        return exe

    for d in _candidate_dirs():
        for suffix in ("", ".exe"):
            p = d / f"{name}{suffix}"
            if p.exists():
                return str(p)

    if name == "ffmpeg":
        try:
            import imageio_ffmpeg  # type: ignore
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    return None


_FFMPEG_HELP = (
    "\n[설치 방법 — 셋 중 하나]\n"
    "  1) winget (가장 쉬움, 새 터미널 필요)\n"
    "       winget install --id Gyan.FFmpeg -e\n"
    "  2) 압축 풀어서 이 프로젝트 bin 폴더에 넣기 (PATH 건드릴 필요 없음)\n"
    "       https://www.gyan.dev/ffmpeg/builds/ → ffmpeg-release-essentials.zip\n"
    "       압축 안의 bin\\ffmpeg.exe, bin\\ffprobe.exe 두 개를\n"
    f"       {LOCAL_BIN}\\ 에 복사\n"
    "  3) 이미 어딘가 있다면 그 폴더를 환경변수로 알려주기\n"
    '       set SERMON_FFMPEG_DIR=C:\\경로\\ffmpeg\\bin\n'
    "\n주의: `pip install ffmpeg` 는 이름만 같은 빈 패키지입니다. 실행파일이 아니라 무용지물이니\n"
    "      `pip uninstall -y ffmpeg` 로 지우세요.\n"
)


def require(name: str) -> str:
    """외부 실행파일 경로를 돌려주고, 없으면 설치 방법을 알려준다."""
    exe = find_tool(name)
    if exe:
        return exe

    if name in ("ffmpeg", "ffprobe"):
        raise ToolError(f"'{name}' 을(를) 찾을 수 없습니다." + _FFMPEG_HELP)
    if name == "yt-dlp":
        raise ToolError(
            "'yt-dlp' 를 찾을 수 없습니다.\n"
            "  python -m pip install -U yt-dlp\n"
            "설치했는데도 안 잡히면 Scripts 폴더가 PATH에 없는 경우입니다."
        )
    raise ToolError(f"'{name}' 을(를) 찾을 수 없습니다. 설치 후 PATH에 등록하세요.")


def check_tools() -> dict[str, str | None]:
    """설치 상태 점검용."""
    return {n: find_tool(n) for n in ("yt-dlp", "ffmpeg", "ffprobe")}


def run(cmd: list[str]) -> str:
    """서브프로세스 실행. 실패 시 stderr를 그대로 담아 예외를 던진다."""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=tool_env(),
    )
    if proc.returncode != 0:
        raise ToolError(
            f"명령 실패 (exit {proc.returncode})\n"
            f"  cmd: {' '.join(cmd)}\n"
            f"  err: {(proc.stderr or '').strip()[-1500:]}"
        )
    return proc.stdout


_PCT = re.compile(r"(\d{1,3}(?:\.\d)?)%")
_FF_TIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_FF_SPEED = re.compile(r"speed=\s*([\d.]+)x")


def _mmss(sec: float) -> str:
    m, s = divmod(int(max(sec, 0)), 60)
    return f"{m}:{s:02d}"


def tool_env() -> dict:
    """bin\ 을 PATH 맨 앞에 얹은 환경.

    yt-dlp 는 JS 런타임(deno)을 PATH 에서 찾는다. 없으면 유튜브 포맷 목록이
    반쪽만 와서 "Requested format is not available" 이 난다.
    """
    env = os.environ.copy()
    extra = [str(d) for d in (LOCAL_BIN, EXE_DIR / "bin") if d.exists()]
    if extra:
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def run_stream(cmd: list[str], tag: str = "", pct_step: float = 2.0,
               min_interval: float = 0.4, total: float | None = None,
               cwd: Path | str | None = None) -> str:
    """진행 상황을 실시간으로 print 한다.

    yt-dlp 와 ffmpeg 은 진행률을 캐리지리턴(\r)으로 덮어쓰며 출력한다. 줄 단위로
    읽으면 다 끝날 때까지 아무것도 안 나오므로 한 글자씩 읽어 \r 도 줄바꿈으로
    취급한다. 진행률 줄은 너무 잦아서 pct_step 만큼 변했을 때만 흘린다.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=tool_env(),
        cwd=str(cwd) if cwd else None,
    )

    collected: list[str] = []
    buf = ""
    last_pct = -100.0
    last_at = 0.0
    prefix = f"{tag} " if tag else "  "
    # 파일이 바뀌면(영상 → 소리, 합치기) 진행률이 0부터 다시 시작한다
    NEW_STAGE = ("Destination:", "[Merger]", "[dashsegments]", "[ExtractAudio]",
                 "Merging formats", "[VideoConvertor]")
    QUIET_LIMIT = 15.0      # 이만큼 조용하면 멈춘 줄 알게 되므로 한 줄 흘린다

    def emit(line: str) -> None:
        nonlocal last_pct, last_at
        line = line.strip()
        if not line:
            return
        collected.append(line)
        now = time.monotonic()

        # 새 단계가 시작되면 진행률 기준을 초기화한다. 안 하면 앞 파일이
        # 100% 로 끝난 뒤 0% 부터 받는 다음 파일이 한 줄도 안 나와
        # 멈춘 것처럼 보인다.
        if any(k in line for k in NEW_STAGE):
            last_pct = -100.0

        # ffmpeg 진행 줄 — frame=/size=/bitrate= 를 그대로 흘리면 로그를 뒤덮는다.
        # 전체 길이를 알면 퍼센트와 남은 시간으로 바꿔 준다.
        ff = _FF_TIME.search(line)
        if ff and "frame=" in line or (ff and "size=" in line):
            done = int(ff.group(1)) * 3600 + int(ff.group(2)) * 60 + float(ff.group(3))
            if total and total > 0:
                pct = min(done / total * 100, 100.0)
                quiet = now - last_at > QUIET_LIMIT
                if pct + 5 < last_pct:      # 새 파일이 시작됐다
                    last_pct = -100.0
                if pct < 100 and not quiet and (
                        pct - last_pct < pct_step or now - last_at < min_interval):
                    return
                last_pct, last_at = pct, now
                sm = _FF_SPEED.search(line)
                speed = float(sm.group(1)) if sm else 0.0
                eta = (total - done) / speed if speed > 0 else 0
                tail = f"   남은 시간 약 {_mmss(eta)}" if eta > 1 else ""
                print(f"{prefix}{pct:3.0f}%   {_mmss(done)} / {_mmss(total)}"
                      f"   {speed:.0f}배속{tail}")
            else:
                if now - last_at < 2.0:
                    return
                last_at = now
                print(f"{prefix}처리 중   {_mmss(done)} 지점")
            return

        m = _PCT.search(line)
        if m and ("[download]" in line or "%" in line):
            pct = float(m.group(1))
            quiet = now - last_at > QUIET_LIMIT
            if pct + 5 < last_pct:          # 새 파일이 시작됐다
                last_pct = -100.0
            if pct < 100 and not quiet and (
                    pct - last_pct < pct_step or now - last_at < min_interval):
                return
            last_pct, last_at = pct, now
        print(prefix + line)

    assert proc.stdout is not None
    while True:
        ch = proc.stdout.read(1)
        if not ch:
            break
        if ch in "\r\n":
            emit(buf)
            buf = ""
        else:
            buf += ch
    emit(buf)
    proc.wait()

    if proc.returncode != 0:
        tail = "\n".join(collected[-12:])
        raise ToolError(f"명령 실패 (exit {proc.returncode})\n  cmd: {' '.join(cmd[:3])} ...\n{tail}")
    return "\n".join(collected)


def hms(sec: float) -> str:
    """3661.5 -> '01:01:01'"""
    sec = max(0.0, float(sec))
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def hms_ms(sec: float) -> str:
    """yt-dlp / ffmpeg 용 '01:01:01.500'"""
    sec = max(0.0, float(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def srt_time(sec: float) -> str:
    """SRT 타임코드 '01:01:01,500'"""
    return hms_ms(sec).replace(".", ",")


def has_audio(media: Path) -> bool:
    """오디오 트랙이 있는지 확인한다.

    yt-dlp 가 영상과 소리를 따로 받아 합치는 도중 끊기면 영상만 있는 파일이
    남는다. 그대로 쓰면 알 수 없는 종료 코드만 나오므로 미리 걸러 낸다.
    """
    try:
        out = run([require("ffprobe"), "-v", "error",
                   "-select_streams", "a", "-show_entries", "stream=index",
                   "-of", "csv=p=0", str(media)])
    except ToolError:
        return False
    return bool(out.strip())


def ffprobe_duration(path: Path) -> float:
    exe = require("ffprobe")
    out = run([
        exe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    try:
        return float(out.strip())
    except ValueError as exc:
        raise ToolError(f"길이를 읽지 못했습니다: {path}") from exc
