"""이 컴퓨터가 얼마나 빠르고 메모리가 얼마나 남았는지, 그래서 얼마나 걸릴지.

왜 필요한가 (2026-10-20):
    교회 PC 는 메모리가 넉넉하지 않다. 자막 모델을 올리다 프로그램이 통째로
    사라지는 일이 있었다. 메모리가 거의 바닥난 상태에서 큰 모델을 올리면
    계산 라이브러리가 오류를 던지지 못하고 프로세스를 끝내 버리기 때문이다.
    그래서 **올리기 전에 미리 재서** 올라갈 만한 것을 고른다.

    그리고 느린 PC 에서는 자막 하나에 한 시간이 걸릴 수도 있다. 얼마나 걸릴지
    모르고 기다리는 것이 제일 답답하므로, 시작할 때 예상 시간을 알려 준다.

예상 시간은 어떻게 구하나:
    처음에는 아래 _RATE 의 대략값에 이 PC 의 속도를 곱해 쓴다. 대략값은
    말 그대로 대략이다. 대신 **실제로 걸린 시간을 기록해 두었다가** 다음부터는
    이 PC 의 기록으로 말한다. 쓸수록 정확해진다.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# 기준 PC 에서 256x256 float32 행렬곱 8번에 걸린 시간(초). 이 값을 1.0 으로 본다.
_REF_SECONDS = 0.0017

# 일 1분당 걸리는 시간(초), 기준 PC 에서. 처음 한 번만 쓰고 그 뒤에는 실제 기록을 쓴다.
_RATE = {
    "find": 1.5,        # 성문으로 설교 찾기 — 영상 1분당
    "stt": 60.0,        # 자막 받아쓰기 — 소리 1분당 (medium 기준)
    "dereverb": 6.0,    # 울림 줄이기 — 소리 1분당
    "export": 3.0,      # 영상 만들기 — 결과 1분당
}

# 자막 모델 크기별 배수. medium 을 1.0 으로 본다.
_MODEL_SPEED = {"large-v3": 2.0, "medium": 1.0, "small": 0.45, "base": 0.25, "tiny": 0.15}

# 자막 모델을 올리는 데 드는 대략의 메모리(MB, int8 기준).
_MODEL_MB = {"large-v3": 1600, "medium": 900, "small": 300, "base": 150, "tiny": 80}

_speed: float | None = None


# ------------------------------------------------------------------ 메모리
def free_mb() -> float | None:
    """지금 쓸 수 있는 메모리(MB). 알 수 없으면 None — 그때는 그냥 해 본다."""
    try:
        if os.name == "nt":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _Status()
            st.dwLength = ctypes.sizeof(_Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None
            return st.ullAvailPhys / 2 ** 20
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2 ** 20
    except Exception:
        return None


def total_mb() -> float | None:
    """이 컴퓨터에 달린 전체 메모리(MB)."""
    try:
        if os.name == "nt":
            import ctypes

            class _S(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _S()
            st.dwLength = ctypes.sizeof(_S)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None
            return st.ullTotalPhys / 2 ** 20
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2 ** 20
    except Exception:
        return None


def free_commit_mb() -> float | None:
    """앞으로 더 쓸 수 있는 메모리(MB) — 실제 메모리 + 페이징 파일의 남은 몫.

    윈도우는 이 값이 바닥나면 프로그램을 아예 띄우지 못한다
    (WinError 1455 "페이징 파일이 너무 작습니다").
    """
    try:
        if os.name != "nt":
            return free_mb()
        import ctypes

        class _S(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _S()
        st.dwLength = ctypes.sizeof(_S)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return st.ullAvailPageFile / 2 ** 20
    except Exception:
        return None


def advice() -> str:
    """지금 메모리 형편이 수상하면 무엇을 봐야 하는지 한 줄. 괜찮으면 빈 글.

    2026-10-20 교회 PC 에서 하루를 날린 증상이 여기 들어 있다:
    실제 메모리는 5.8GB 남아 있는데 윈도우가 "더 내줄 수 없다"(0.1GB)고 했다.
    하이퍼-V 가 켜져 있어 커밋을 통째로 예약하고 있었고, 프로그램 목록에는
    잡히지 않았다. 끄고 다시 켜니 29.7GB 가 1.7GB 로 떨어졌다.

    같은 증상은 '실제 메모리는 남았는데 더 쓸 수 있는 양만 바닥' 이라는
    모양으로 나타난다. 그 조합을 보면 바로 짚어 준다.
    """
    have, commit, total = free_mb(), free_commit_mb(), total_mb()
    if have is None or commit is None:
        return ""
    # 커밋 한도에서 실제 메모리를 빼면 페이징 파일 크기가 나온다.
    page = None
    if total is not None:
        limit = commit_limit_mb()
        if limit is not None:
            page = max(limit - total, 0.0)

    if commit >= 1500:
        return ""
    if page is not None and page < 4096:
        # 페이징 파일이 작으면 커밋 한도가 실제 메모리보다 조금 큰 수준에 머문다.
        # 평소 쓰는 프로그램만으로도 한도를 다 쓰고, ffmpeg 이 버퍼 하나를 더
        # 달라고 할 때 내줄 것이 없다 (2026-10-20 교회 PC: 한도 14.7GB, 남은 0.7GB).
        return ("쓸 수 있는 메모리가 {0:.1f}GB 뿐입니다. 가상 메모리(페이징 파일)가 "
                "{1:.1f}GB 로 작아서 그렇습니다. [기록] 을 열고 "
                "[메모리 넉넉하게 만들기] 를 한 번 눌러 주시면 늘려 드립니다 — "
                "누른 뒤 윈도우를 다시 시작하면 풀립니다."
                ).format(commit / 1024, page / 1024)
    if have > 2500:
        return ("실제 메모리는 {0:.1f}GB 남았는데 윈도우가 더 내줄 수 있는 양은 {1:.1f}GB 뿐입니다. "
                "가상 머신(하이퍼-V·WSL·VirtualBox·Docker 등)이 메모리를 통째로 잡고 있을 때 "
                "이렇게 됩니다. 작업 관리자 [성능] [메모리] 의 '커밋됨' 을 보시고, 돌고 있는 "
                "가상 머신을 끄거나 컴퓨터를 다시 시작하면 풀립니다.").format(have / 1024, commit / 1024)
    return ("쓸 수 있는 메모리가 {0:.1f}GB 뿐입니다. 다른 프로그램을 닫고 쓰시면 좋습니다."
            ).format(commit / 1024)


def commit_limit_mb() -> float | None:
    """윈도우가 통틀어 내줄 수 있는 양(MB) = 실제 메모리 + 페이징 파일."""
    try:
        if os.name != "nt":
            return total_mb()
        import ctypes

        class _S(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _S()
        st.dwLength = ctypes.sizeof(_S)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return st.ullTotalPageFile / 2 ** 20
    except Exception:
        return None


def whisper_model_for(wanted: str, ladder: tuple[str, ...]) -> tuple[str, str]:
    """지금 메모리로 올릴 만한 자막 모델을 고른다. (고른 것, 까닭) 을 돌려준다.

    받아쓰는 동안 쓰는 작업 공간까지 있어야 하므로 모델 크기의 세 배를 본다.
    메모리를 못 재면 정해 둔 것을 그대로 쓴다.

    남은 메모리가 아니라 **더 쓸 수 있는 양**(headroom_mb)을 본다. 커밋 한도에
    걸린 PC 는 메모리가 6.5GB 남아도 0.8GB 밖에 못 쓴다 — 남은 메모리를 보면
    큰 모델을 올리다 넘어지고 나서야 작은 모델로 내려간다 (2026-10-20).
    """
    have = headroom_mb()
    if have is None or wanted not in ladder:
        return wanted, ""
    for name in ladder[ladder.index(wanted):]:
        if have > _MODEL_MB.get(name, 900) * 3:
            if name == wanted:
                return name, ""
            return name, (f"쓸 수 있는 메모리가 {have:.0f}MB 라 '{wanted}' 대신 "
                          f"'{name}' 모델로 받아씁니다 — 글자가 조금 더 틀릴 수 있습니다")
    last = ladder[-1]
    return last, (f"쓸 수 있는 메모리가 {have:.0f}MB 뿐이라 가장 작은 '{last}' 모델로 "
                  "받아씁니다 — 글자가 꽤 틀릴 수 있습니다")


def headroom_mb() -> float | None:
    """실제로 더 쓸 수 있는 양. 남은 메모리와 커밋 여유 중 **작은 쪽**.

    메모리는 남아 있는데 커밋 한도에 걸려 실패하는 PC 가 있다 (페이징 파일이
    896MB 로 잡힌 경우: 메모리 6.7GB 남았는데 더 쓸 수 있는 양은 0.7GB).
    그래서 둘 중 작은 쪽을 봐야 한다.
    """
    vals = [v for v in (free_mb(), free_commit_mb()) if v is not None]
    return min(vals) if vals else None


def ffmpeg_threads() -> int:
    """ffmpeg·x264 에 줄 스레드 수.

    스레드마다 화면 버퍼를 따로 잡는다. 코어가 16개인 PC 에서 제한을 안 두면
    1080x1920 세로 영상을 만들 때 그것만으로 바닥난다.

    **남은 메모리가 아니라 "더 쓸 수 있는 양" 을 본다.** 2026-10-20 에 메모리
    6.4GB 가 남은 PC 에서 스레드 8개로 돌렸다가 실패했다 — 그 PC 는 커밋 한도에
    걸려 실제로 더 쓸 수 있는 양이 0.5GB 뿐이었다. 남은 메모리를 보면 이걸 놓친다.
    """
    cores = os.cpu_count() or 4
    have = headroom_mb()
    if have is None:
        return max(1, min(2, cores))
    if have > 6000:
        return max(1, min(8, cores))
    if have > 3000:
        return max(1, min(4, cores))
    if have > 1500:
        return 2
    return 1


def x264_preset(want: str) -> tuple[str, str]:
    """쓸 preset 과 바꾼 까닭. **화질 낮추기는 마지막 수단이다.**

    메모리가 모자랄 때 먼저 하는 일은 조각을 나눠 여러 번에 걸쳐 만드는 것이다
    (batch_runs). 그래도 모자랄 만큼 바닥일 때만 화질을 내린다
    — 사용자 뜻: "화질을 낮추기보다 쪼개서 만들기" (2026-10-20).
    """
    light = ("veryfast", "superfast", "ultrafast")
    have = headroom_mb()
    if have is None or have > 700:
        return want, ""
    pick = "ultrafast" if have <= 450 else "veryfast"
    if want == pick or (want in light and light.index(want) >= light.index(pick)):
        return want, ""
    return pick, (f"조각을 하나씩 만들어도 모자랄 만큼 메모리가 바닥입니다"
                  f"({have:.0f}MB). 마지막 수단으로 화질 설정을 "
                  f"'{want}' 에서 '{pick}' 으로 낮춥니다")


def x264_extra() -> list[str]:
    """빠듯할 때 x264 에 더 붙일 설정. 넉넉하면 빈 목록.

    x264 는 앞 화면을 몇 장 쥐고 뒤를 내다본다(lookahead). 그게 메모리의 큰
    몫이다. 내다보기를 줄이면 쓰는 양이 준다. 화질 손실은 preset 을 내리는
    것보다 훨씬 작아서, 쪼개기 다음·화질 낮추기 앞에 둔다.
    """
    have = headroom_mb()
    if have is None or have > 1200:
        return []
    return ["-x264-params", "rc-lookahead=10:sync-lookahead=0:ref=1:bframes=0"]


def batch_runs(count: int) -> int:
    """한 번의 ffmpeg 호출에 넣을 조각 수. count 그대로면 안 쪼갠다.

    ffmpeg 은 조각마다 원본에서 따로 잘라내는 가지를 만들고, 그 가지들을
    **동시에** 들고 있다가 마지막에 이어붙인다. 그래서 조각이 20개면 원본을
    20군데에서 동시에 물고 있는 셈이고, 메모리는 거기서 가장 많이 샌다.

    화질을 깎는 대신 조각을 몇 개씩 나눠 여러 번 만들고 마지막에 이어붙인다.
    이어붙일 때는 화면을 그대로 복사하므로 화질이 깎이지 않는다.
    """
    if count <= 1:
        return max(1, count)
    have = headroom_mb()
    if have is None:
        return min(count, 4)
    if have > 6000:
        return count                     # 넉넉하면 예전처럼 한 번에
    if have > 3000:
        return min(count, 8)
    if have > 1500:
        return min(count, 4)
    if have > 800:
        return min(count, 2)
    return 1


def export_check(width: int = 1080, height: int = 1920,
                 count: int = 1) -> tuple[bool, str]:
    """영상 만들기를 시작해도 되는가. (괜찮은가, 알릴 말).

    몇 분 돌다가 터지는 것보다 시작 전에 말해 주는 쪽이 낫다. 다만 **되는 일을
    막지는 않는다** — 조각을 하나씩 만들면 빠듯해도 끝나는 경우가 많아서,
    한 조각조차 못 만들 만큼일 때만 막는다.

    ffmpeg 은 화면 한 장(1080x1920 이면 3MB)을 여러 장 쥐고, 소리·먹싱 버퍼까지
    합쳐 아무리 아껴도 400MB 안팎이 필요하다.
    """
    have = headroom_mb()
    if have is None:
        return True, ""
    frame_mb = width * height * 1.5 / 2 ** 20
    need = max(400.0, 250.0 + frame_mb * 12)        # 조각 하나를 만들 때의 최소치
    if have > need * 3:
        return True, ""

    free_ram, limit, total = free_mb(), commit_limit_mb(), total_mb()
    stuck = (free_ram is not None and limit is not None and free_ram > have * 2)
    msg = f"더 쓸 수 있는 메모리가 {have:.0f}MB 뿐입니다"
    if stuck:
        msg += (f" — 메모리는 {free_ram / 1024:.1f}GB 남아 있는데도 못 씁니다.\n"
                f"윈도우가 정한 한도(커밋 한도 {limit / 1024:.1f}GB)에 이미 닿아서입니다.\n")
        page = (limit - total) if total is not None else None
        if page is not None and page < total * 0.5:
            # 페이징 파일이 작아서 한도 자체가 낮다 → 단추가 보이고, 누르면 풀린다
            msg += ("[기록] 을 열고 [메모리 넉넉하게 만들기] 를 한 번 눌러 주세요 — "
                    "한도를 늘려 드립니다.")
        else:
            # 한도는 넉넉한데 무언가가 다 쓰고 있다 (2026-10-20 실측: 한도 55.8GB 인데
            # 0.7GB 남음). 이때는 단추가 보이지 않으므로 누르라고 하면 안 된다.
            msg += ("한도는 넉넉한데 다른 무언가가 다 쓰고 있습니다. 가상 머신"
                    "(하이퍼-V·WSL·VirtualBox·Docker)이나 큰 프로그램을 끄고 다시 해 보세요.")
    else:
        msg += ". 다른 프로그램을 닫으면 나아집니다."

    if have < need:
        return False, (f"{msg}\n\n{width}x{height} 영상은 한 조각을 만드는 데만도 "
                       f"{need:.0f}MB 가 필요해서, 시작하지 않았습니다. "
                       "몇 분 돌다가 실패하는 것보다 낫다고 보았습니다.")

    per = batch_runs(count) if count > 1 else 1
    if count > 1 and per < count:
        msg += (f"\n조각 {count}개를 {per}개씩 나눠 만든 뒤 이어붙입니다 — "
                "화질은 그대로 두고 메모리만 아끼는 길입니다. 시간은 조금 더 걸립니다.")
    else:
        msg += "\n가장 적게 쓰는 설정으로 바꿔 진행합니다 — 느려지지만 끝까지 갑니다."
    return True, msg


def llm_context_for(want: int) -> tuple[int, str]:
    """AI 에게 줄 context 길이와 바꾼 까닭.

    llama-server 는 context 길이만큼 KV 칸을 미리 잡는다. 8192 면 그것만으로
    수백 MB 다. 우리는 후보 문장만 넘기므로 2048 이어도 잘린 적이 없다.
    빠듯한 PC 에서는 내려서라도 돌아가게 한다.
    """
    have = headroom_mb()
    if have is None or have > 4000:
        return want, ""
    limit = 4096 if have > 2500 else 2048
    if want <= limit:
        return want, ""
    return limit, (f"더 쓸 수 있는 메모리가 {have:.0f}MB 뿐이라 AI 가 한 번에 "
                   f"읽는 분량을 {want} 에서 {limit} 로 줄입니다")


def llm_threads_for(want: int) -> int:
    """AI 에게 줄 스레드 수. 스레드마다 계산 버퍼를 따로 잡는다."""
    cores = os.cpu_count() or 4
    have = headroom_mb()
    top = min(want, cores)
    if have is None:
        return max(1, top)
    if have < 1500:
        return 1
    if have < 2500:
        return max(1, min(2, top))
    return max(1, top)


# ------------------------------------------------------------------ 속도
def speed_index() -> float:
    """이 PC 가 기준 PC 의 몇 배 속도인가. 1.0 이면 같고, 0.5 면 두 배 느리다."""
    global _speed
    if _speed is None:
        try:
            import numpy as np

            a = np.random.rand(256, 256).astype(np.float32)
            a @ a                                   # 예열
            best = None
            for _ in range(3):
                t0 = time.perf_counter()
                for _ in range(8):
                    a @ a
                took = time.perf_counter() - t0
                best = took if best is None else min(best, took)
            _speed = max(0.05, min(20.0, _REF_SECONDS / max(best, 1e-9)))
        except Exception:
            _speed = 1.0
    return _speed


# ------------------------------------------------------------------ 기록과 예상
def _store(work_dir: Path | None) -> Path:
    base = Path(work_dir) if work_dir else Path(__file__).resolve().parent.parent / "data"
    return base / "속도기록.json"


def _load(work_dir: Path | None) -> dict:
    try:
        return json.loads(_store(work_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def remember(kind: str, minutes: float, seconds: float, work_dir: Path | None = None) -> None:
    """실제로 걸린 시간을 남긴다. 다음부터 이 기록으로 예상 시간을 말한다.

    최근 다섯 번의 가운데값을 쓴다 — 한 번 유난히 느렸다고 끌려가지 않게.
    """
    if minutes <= 0 or seconds <= 0:
        return
    try:
        data = _load(work_dir)
        rates = data.setdefault(kind, [])
        rates.append(round(seconds / minutes, 3))
        data[kind] = rates[-5:]
        path = _store(work_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass                                        # 기록 실패가 일을 막으면 안 된다


def rate_for(kind: str, work_dir: Path | None = None, model: str = "") -> tuple[float, bool]:
    """일 1분당 걸리는 시간(초)과, 그것이 이 PC 의 실제 기록인지 여부."""
    rates = _load(work_dir).get(kind) or []
    if rates:
        mid = sorted(rates)[len(rates) // 2]
        return mid, True
    base = _RATE.get(kind, 10.0) / max(speed_index(), 1e-6)
    if kind == "stt" and model:
        base *= _MODEL_SPEED.get(model, 1.0)
    return base, False


def estimate(kind: str, minutes: float, work_dir: Path | None = None,
             model: str = "") -> str:
    """'예상 12분' 처럼 사람이 읽는 글. 잴 수 없으면 빈 글."""
    if minutes <= 0:
        return ""
    rate, measured = rate_for(kind, work_dir, model)
    total = rate * minutes
    if total < 60:
        text = f"{total:.0f}초"
    elif total < 3600:
        text = f"{total / 60:.0f}분"
    else:
        text = f"{total / 3600:.1f}시간"
    return f"예상 {text}" + ("" if measured else " 쯤")


def summary() -> str:
    """이 컴퓨터가 어떤 형편인지 한 줄. 켤 때 기록에 남긴다."""
    have, total = free_mb(), total_mb()
    speed = speed_index()
    bits = []
    if total:
        bits.append(f"메모리 {total / 1024:.1f}GB 중 {have / 1024:.1f}GB 남음"
                    if have else f"메모리 {total / 1024:.1f}GB")
    bits.append(f"속도 기준 대비 {speed:.1f}배" if speed >= 1 else
                f"속도 기준 대비 {1 / speed:.1f}배 느림")
    commit = free_commit_mb()
    if commit is not None:
        bits.append(f"더 쓸 수 있는 양 {commit / 1024:.1f}GB")
    bits.append(f"코어 {os.cpu_count()}개")
    return " · ".join(bits)
