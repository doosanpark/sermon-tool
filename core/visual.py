"""음성 설교에 사진·영상을 입혀 가로 영상을 만든다 (2026-11-02).

왜 필요한가:
    유튜브에 소리만 올라온 설교가 있다 (예: C7Py9ZtdtZ0 — 화면 없이 음성만). 그대로
    잘라 올리면 40분 내내 화면이 멈춰 있다. 설교 구간만 잘라 낸 소리 위에 교회가 가진
    사진·영상을 깔고 제목과 본문을 얹어, 바로 올릴 수 있는 영상으로 만든다.
    유튜브에 올릴지 인스타그램에 올릴지는 쓰는 분이 정한다 — 그래서 파일 이름과
    글에 특정 곳을 적지 않는다 (2026-11-02 사용자 뜻).

만드는 것 (cfg.export_dir):
    날짜_제목_영상.mp4          1920x1080 · 30fps · H.264 + AAC 스테레오
    날짜_제목_썸네일.jpg        1280x720 — 목록에 보이는 미리보기 그림
    날짜_제목_설명글.txt        제목·설명 칸에 붙여 넣을 글

화면 짜기:
    폴더의 사진·영상을 차례로(또는 섞어서) 깐다. 사진은 정해 둔 시간(기본 20초) 동안
    천천히 확대·축소하거나 옆으로 흐르고(켄 번스), 다음 것으로 넘어갈 때 1초 동안 겹쳐
    넘긴다(디졸브). 영상은 제 길이만큼(길면 60초까지) 소리 없이 쓴다. 다 쓰면 처음부터
    되풀이한다. 세로 사진·세로 영상은 가운데에 두고 양옆을 흐리게 채운다.
    처음 8초 동안 제목과 본문을 크게 보여 주고, 그 뒤에는 왼쪽 위에 작게 둔다.

왜 조각으로 나눠 만드나:
    40분을 ffmpeg 한 번으로 만들려면 사진 120장을 한꺼번에 열어야 한다. 메모리가
    빠듯한 PC 에서는 거기서 넘어진다 (2026-10-20 실측: 더 쓸 수 있는 양 0.7GB).
    사진 한 장이 화면에 머무는 동안을 조각 하나로 만들면 ffmpeg 는 많아야 두 장만 문다.
    조각은 같은 설정으로 인코딩하므로 화면을 다시 만들지 않고 그대로 이어붙인다.
    조각끼리는 서로 기다리지 않으므로 코어가 넉넉하면 두세 개를 동시에 만든다.

    조각 i 는 '앞 칸에서 i 칸으로 넘어가는 1초' 와 'i 칸의 나머지(다음 넘김 직전까지)'
    로 이뤄진다. 앞 칸의 마지막 1초를 다시 그려 겹치는데, 사진의 움직임은 칸 안에서
    몇 번째 장면인지로만 정해지므로 앞 조각과 정확히 이어진다.

사진을 어떻게 움직이나 (실측 2026-11-02, 2코어 컨테이너, 1080p 20초 = 600장):
    zoompan 은 자를 자리를 정수 픽셀로 잡는다. 1080p 원본에서 하면 1픽셀씩 뚝뚝
    끊기므로 사진을 미리 2배(3840x2160)로 준비해 두고 그 안에서 움직인다 — 화면 기준
    0.5픽셀 단위가 된다. yuv420 에서는 자리가 짝수로 맞춰져 다시 1픽셀이 되므로
    RGB 로 돌린다. 그게 가장 빠르기도 했다.
        zoompan 2배 RGB          6.7초 (+ x264 = 14.2초)
        zoompan 2배 yuv444      14.9초
        zoompan 3배 RGB         13.9초
        perspective 1배 cubic   34.3초 (x264 포함)

FilmCraft(MIT/Apache-2.0, ArtCraft 팀)에서 가져온 것:
    배경 음악 자동 덕킹 — core/ducking.py. 방법과 기본값을 그대로 옮겼고, FilmCraft 의
               시험 값이 그대로 나오는지 selftest.py 가 확인한다.
    글자 바탕 — crates/captions/src/burn.rs 의 자막처럼 1080줄 화면을 기준으로 글자
               크기를 정하고 반투명 바탕을 깐다. 어떤 사진 위에서도 읽히게.
    넘김 — FilmCraft 의 Cross Dissolve 와 같은 뜻의 겹쳐 넘기기. 그리기는 ffmpeg
               xfade(fade) 가 한다. 글자가 나타나고 사라지는 것도 ffmpeg fade 다.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from . import cut as cut_mod
from . import ducking, machine
from .util import (ToolError, ffprobe_duration, has_audio, is_memory_error, require,
                   run, run_stream, tool_env)

WIDTH, HEIGHT, FPS = 1920, 1080, 30
PREP_W, PREP_H = 3840, 2160          # 사진을 이 크기로 준비해 두고 그 안에서 움직인다
XFADE = 30                           # 넘길 때 겹치는 장면 수 (1초)
PHOTO_SECONDS = 20.0                 # 사진 한 장을 보여 줄 기본 시간
VIDEO_MAX_SECONDS = 60.0             # 영상 하나를 이보다 길게 쓰지 않는다
MIN_SLOT_SECONDS = 4.0
MOTION_PER_SECOND = 0.006            # 사진이 1초에 커지는 비율 (20초면 12%)

# 처음에 크게 보여 주는 제목 카드와, 그 뒤 왼쪽 위의 작은 글자
CARD_IN, CARD_FADE, CARD_OUT = 0.5, 0.8, 7.5     # 0.5초부터 드러나 7.5초부터 사라진다
CORNER_IN, CORNER_FADE = CARD_OUT + CARD_FADE, 0.8
CORNER_X, CORNER_Y = 44, 40
BLACK_IN, BLACK_OUT = 1.0, 2.0       # 처음엔 검은 화면에서 드러나고 끝엔 검게 닫힌다
TAIL_PAD = 1.0                       # 말이 끝난 뒤 덧붙이는 조용한 시간 (끝 화면이 닫히는 몫)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".mts", ".m2ts", ".3gp"}
MUSIC_EXT = "*.mp3 *.m4a *.aac *.wav *.flac *.ogg *.opus *.wma"

# 배경 음악 크기: 목사님이 쉬실 때 말소리보다 몇 dB 아래인가. 말씀하실 때는 여기서
# 15dB 를 더 줄인다 (FilmCraft 덕킹 기본값). 작게(-10) 면 말씀 중에는 -25dB 다.
MUSIC_LEVELS = {"softer": -14.0, "low": -10.0, "medium": -6.0}
MUSIC_LABELS = {"softer": "아주 작게", "low": "작게 (권장)", "medium": "보통"}
MUSIC_GUESS_LUFS = -14.0             # 음악 크기를 못 재면 흔한 음원 크기로 본다

# 사진 움직임: (시작 확대, 끝 확대, 시작 가로 자리, 끝 가로, 시작 세로, 끝 세로)
# 자리는 잘라 낼 수 있는 폭 안에서의 비율(0 왼쪽 ~ 1 오른쪽). 확대 A 는 칸 길이에 맞춘다.
MOTIONS = [
    (0.0, 1.0, 0.5, 0.5, 0.5, 0.5),     # 가운데로 다가가기
    (1.0, 0.0, 0.5, 0.5, 0.5, 0.5),     # 가운데에서 물러나기
    (0.0, 1.0, 0.30, 0.55, 0.35, 0.50),  # 왼쪽 위에서 다가가며 가운데로
    (1.0, 0.0, 0.70, 0.45, 0.60, 0.50),  # 오른쪽 아래에서 물러나며 가운데로
    (1.0, 1.0, 0.15, 0.85, 0.5, 0.5),   # 오른쪽으로 흐르기
    (1.0, 1.0, 0.85, 0.15, 0.5, 0.5),   # 왼쪽으로 흐르기
    (0.0, 0.0, 0.5, 0.5, 0.5, 0.5),     # 움직이지 않기 (배경 없이 만들 때)
]
MOTION_ORDER = [0, 3, 1, 4, 2, 5]
STILL = 6

# 편집기에서 고르는 움직임 (열쇠 → (보이는 이름, MOTIONS 번호)). auto 는 칸마다 번갈아 쓴다.
MOTION_CHOICES = {
    "auto": ("자동 (번갈아)", -1),
    "in": ("가운데로 다가가기", 0),
    "out": ("가운데에서 물러나기", 1),
    "in_tl": ("왼쪽 위에서 다가가기", 2),
    "out_br": ("오른쪽 아래에서 물러나기", 3),
    "right": ("오른쪽으로 흐르기", 4),
    "left": ("왼쪽으로 흐르기", 5),
    "still": ("움직이지 않기", STILL),
}

# 앞 장면에서 넘어올 때 (열쇠 → (보이는 이름, ffmpeg xfade 이름)). cut 은 겹치지 않고 바로.
# FilmCraft 의 Cross Dissolve · Dip to Black/White · Push · Wipe · Iris 에 해당한다.
TRANSITIONS = {
    "dissolve": ("겹쳐 넘기기", "fade"),
    "black": ("검게 넘기기", "fadeblack"),
    "white": ("하얗게 넘기기", "fadewhite"),
    "push_left": ("왼쪽으로 밀기", "slideleft"),
    "push_right": ("오른쪽으로 밀기", "slideright"),
    "wipe_left": ("왼쪽으로 닦기", "wipeleft"),
    "wipe_right": ("오른쪽으로 닦기", "wiperight"),
    "circle": ("동그랗게 열기", "circleopen"),
    "cut": ("바로 넘기기 (겹치지 않음)", ""),
}
TRANS_MIN, TRANS_MAX = 0.3, 3.0

# 배경 사진·영상 없이 만들 때의 바탕 — 짙은 곤색 (위가 조금 밝다). 2026-11-02 의견:
# "검정 대신 짙은 곤색이면 사람들이 조금 더 편안하게 누를 것 같다".
PLAIN_TOP, PLAIN_BOTTOM = "0x22355E", "0x0C1631"

# EXIF 방향 → ffmpeg 필터. 휴대폰 사진은 대개 6(시계 방향 90도)이다.
ORIENT = {2: "hflip", 3: "hflip,vflip", 4: "vflip", 5: "transpose=0",
          6: "transpose=1", 7: "transpose=3", 8: "transpose=2"}


# ------------------------------------------------------------------ 고른 것
@dataclass
class Options:
    folder: str = ""
    photo_seconds: float = PHOTO_SECONDS
    shuffle: bool = False
    title: str = ""
    verse: str = ""
    corner: bool = True          # 제목 카드가 사라진 뒤에도 왼쪽 위에 작게 둔다
    music: str = ""              # 배경 음악 파일. 비우면 넣지 않는다
    music_level: str = "low"
    # 편집기에서 타임라인에 놓은 장면들 (item 사전 — new_item() 참고). 비어 있으면
    # folder 를 예전처럼 차례로 깔고, 그것도 없으면 곤색 바탕으로 만든다.
    items: list = field(default_factory=list)
    fill: str = "repeat"         # 장면이 설교보다 짧을 때: repeat(처음부터 되풀이) | hold(끝 장면 늘이기)


@dataclass
class Media:
    path: Path
    kind: str                    # "photo" | "video"
    seconds: float = 0.0         # 영상 길이
    width: int = 0               # 화면에 보이는 크기 (회전 반영)
    height: int = 0
    prepared: Path | None = None  # 사진: 2배 크기로 준비해 둔 그림
    src_start: float = 0.0       # 영상: 원본의 어디부터 쓸지 (초)


@dataclass
class Slot:
    media: Media
    frames: int                  # 넘김(앞뒤)을 포함한 이 칸의 장면 수
    motion: int = 0
    xfade: int = 0               # 앞 칸과 겹치는 장면 수 (첫 칸·바로 넘기기는 0)
    transition: str = "fade"     # 앞 칸에서 넘어오는 모양 (ffmpeg xfade 이름)
    item: int = -1               # 편집기 타임라인의 몇 번째 장면인가 (-1: 폴더 자동)
    ghost: bool = False          # 장면이 모자라 되풀이한 칸


# ------------------------------------------------------------------ 폴더 읽기
def _natural(p: Path) -> list:
    """IMG_2.jpg 가 IMG_10.jpg 보다 앞에 오게."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def list_media(folder: str | Path) -> tuple[list[Path], list[Path]]:
    """폴더 안의 (사진, 영상) 파일. 열어 보지 않으므로 빠르다 — 대화상자에서 쓴다.

    하위 폴더는 보지 않는다. 숨김 파일(.으로 시작)과 윈도우 썸네일 파일도 뺀다.
    """
    root = Path(folder) if folder else None
    if not root or not root.is_dir():
        return [], []
    files = sorted((p for p in root.iterdir()
                    if p.is_file() and not p.name.startswith((".", "~$"))), key=_natural)
    photos = [p for p in files if p.suffix.lower() in IMAGE_EXT]
    videos = [p for p in files if p.suffix.lower() in VIDEO_EXT]
    return photos, videos


def _probe(path: Path) -> dict:
    """화면 크기(회전 반영)와 길이. 읽지 못하면 빈 dict."""
    try:
        out = run([require("ffprobe"), "-v", "error", "-select_streams", "v:0",
                   "-show_entries",
                   "stream=width,height:stream_tags=rotate:stream_side_data=rotation"
                   ":format=duration",
                   "-of", "json", str(path)])
        data = json.loads(out or "{}")
    except (ToolError, ValueError, OSError):
        return {}
    streams = data.get("streams") or []
    if not streams:
        return {}
    st = streams[0]
    w, h = int(st.get("width") or 0), int(st.get("height") or 0)
    rot = 0.0
    try:
        rot = float((st.get("tags") or {}).get("rotate") or 0)
    except ValueError:
        rot = 0.0
    for sd in st.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = float(sd["rotation"])
            except (TypeError, ValueError):
                pass
    if int(round(abs(rot))) % 180 == 90:
        w, h = h, w
    try:
        dur = float((data.get("format") or {}).get("duration") or 0.0)
    except ValueError:
        dur = 0.0
    return {"width": w, "height": h, "seconds": dur}


def exif_orientation(path: Path) -> int:
    """JPEG 의 EXIF 방향(1~8). 없거나 읽지 못하면 1.

    ffmpeg 판마다 EXIF 방향을 따르기도 하고 안 따르기도 해서, 직접 읽고 ffmpeg 에는
    -noautorotate 를 준다. 그래야 어느 판에서나 같은 그림이 나온다.
    """
    try:
        with open(path, "rb") as f:
            data = f.read(512 * 1024)
    except OSError:
        return 1
    if data[:2] != b"\xff\xd8":
        return 1
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return 1
        marker = data[i + 1]
        if marker == 0xFF:              # 채움 바이트
            i += 1
            continue
        if marker in (0xD9, 0xDA):      # 그림 끝 · 그림 데이터 시작
            return 1
        size = int.from_bytes(data[i + 2:i + 4], "big")
        if size < 2:
            return 1
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
            return _tiff_orientation(data[i + 10:i + 2 + size])
        i += 2 + size
    return 1


def _tiff_orientation(t: bytes) -> int:
    if len(t) < 8:
        return 1
    order = {b"II": "little", b"MM": "big"}.get(t[:2])
    if not order:
        return 1
    off = int.from_bytes(t[4:8], order)
    if off + 2 > len(t):
        return 1
    count = int.from_bytes(t[off:off + 2], order)
    for k in range(min(count, 512)):
        e = off + 2 + 12 * k
        if e + 12 > len(t):
            break
        if int.from_bytes(t[e:e + 2], order) == 0x0112:
            value = int.from_bytes(t[e + 8:e + 10], order)
            return value if 1 <= value <= 8 else 1
    return 1


def _fills_frame(w: int, h: int) -> bool:
    """가로 그림은 화면을 꽉 채우고(위아래가 조금 잘림), 세로·정사각 그림은 가운데에
    두고 양옆을 흐리게 채운다.

    휴대폰 가로 사진(4:3)도 꽉 채운다 — 위아래가 1/8 씩 잘리지만, 양옆에 흐린 띠가
    생기는 것보다 자연스럽다. 정사각형(1:1)부터는 너무 많이 잘려 흐린 바탕을 쓴다.
    """
    if w <= 0 or h <= 0:
        return True
    ratio = (w / h) / (WIDTH / HEIGHT)
    return 0.70 <= ratio <= 1.35


def scan(folder: str | Path) -> tuple[list[Media], list[str]]:
    """쓸 사진·영상과, 건너뛴 파일의 까닭."""
    photos, videos = list_media(folder)
    media: list[Media] = []
    skipped: list[str] = []
    for p in sorted(photos + videos, key=_natural):
        if p.suffix.lower() in IMAGE_EXT:
            media.append(Media(p, "photo"))
            continue
        info = _probe(p)
        if not info or info["width"] <= 0:
            skipped.append(f"{p.name} — 열지 못했습니다")
        elif info["seconds"] < 1.5:
            skipped.append(f"{p.name} — 너무 짧습니다 ({info['seconds']:.1f}초)")
        else:
            media.append(Media(p, "video", seconds=info["seconds"],
                               width=info["width"], height=info["height"]))
    return media, skipped


# ------------------------------------------------------------------ 음성 설교인가
def _gray_frame(path: Path, at: float, w: int = 64, h: int = 36) -> bytes | None:
    """그 시각의 화면을 아주 작은 흑백 그림(바이트)으로. 못 읽으면 None."""
    try:
        proc = subprocess.run(
            [require("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{at:.2f}", "-i", str(path),
             "-frames:v", "1", "-vf", f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=tool_env(), timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError, ToolError):
        return None
    return proc.stdout if len(proc.stdout) == w * h else None


def looks_audio_only(path: Path | str, title: str = "") -> tuple[bool, str]:
    """소리만 있는 설교(음성 설교)로 보이는가. (그렇다, 까닭).

    세 가지로 본다 (2026-11-02):
      - 영상 제목에 '음성' 이 있다 — 교회가 "이른 아침예배 음성 LIVE 방송" 처럼 올린다.
      - 화면이 없는 파일이다 — mp3·m4a·wav.
      - 화면이 처음부터 끝까지 그대로다 — 소리만 올린 영상은 그림 한 장을 띄워 둔다.
        고르게 여섯 장을 뽑아 서로 견준다. 48분 영상에 3초 안팎이라 뒤에서 돌린다.
    틀릴 수 있으므로 화면에는 체크로 보여 주고 사람이 끌 수 있게 한다.
    """
    if "음성" in (title or ""):
        return True, "영상 제목에 '음성' 이 들어 있습니다"
    p = Path(path)
    if not p.exists():
        return False, ""
    info = _probe(p)
    if not info or info.get("width", 0) <= 0:
        if has_audio(p):
            return True, "화면 없이 소리만 있는 파일입니다"
        return False, ""
    seconds = float(info.get("seconds") or 0.0)
    if seconds < 30:
        return False, ""
    shots = [f for f in (_gray_frame(p, seconds * k / 7) for k in range(1, 7)) if f]
    if len(shots) < 3:
        return False, ""
    base = shots[0]
    worst = max(sum(abs(a - b) for a, b in zip(base, other)) / len(base) for other in shots[1:])
    if worst < 2.0:
        return True, "화면이 처음부터 끝까지 그대로입니다"
    return False, ""


# ------------------------------------------------------------------ 사진 준비
def prepare_photo(media: Media, cache_dir: Path) -> Path | None:
    """사진을 3840x2160 으로 한 번 준비해 둔다 (방향 바로잡기 · 꽉 채우기 또는 흐린 바탕).

    같은 사진은 다시 하지 않는다 — 파일 크기와 고친 시각이 같으면 그대로 쓴다.
    """
    src = media.path
    try:
        st = src.stat()
    except OSError:
        return None
    key = hashlib.sha1(f"{src.resolve()}|{st.st_size}|{st.st_mtime_ns}|"
                       f"{PREP_W}x{PREP_H}|v2".encode("utf-8")).hexdigest()[:20]
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{key}.jpg"
    if out.exists() and out.stat().st_size > 1024:
        return out

    try:
        info = json.loads(run([require("ffprobe"), "-v", "error", "-select_streams", "v:0",
                               "-show_entries", "stream=width,height", "-of", "json",
                               str(src)]) or "{}")
        st0 = (info.get("streams") or [{}])[0]
        w, h = int(st0.get("width") or 0), int(st0.get("height") or 0)
    except (ToolError, ValueError, OSError):
        return None
    if w <= 0 or h <= 0:
        return None
    orient = exif_orientation(src) if src.suffix.lower() in (".jpg", ".jpeg") else 1
    if orient in (5, 6, 7, 8):
        w, h = h, w
    turn = (ORIENT[orient] + ",") if orient in ORIENT else ""
    if _fills_frame(w, h):
        # 위아래를 자를 때는 가운데가 아니라 조금 위(35%)를 남긴다. 사람 사진은 머리가
        # 위쪽에 있어서, 가운데로 자르면 4:3 사진에서 이마가 잘린다.
        graph = (f"[0:v]{turn}scale={PREP_W}:{PREP_H}:force_original_aspect_ratio=increase"
                 f":flags=lanczos,crop={PREP_W}:{PREP_H}:(iw-ow)/2:(ih-oh)*0.35,setsar=1[out]")
    else:
        graph = (f"[0:v]{turn}split=2[a][b];"
                 f"[a]scale=960:540:force_original_aspect_ratio=increase,crop=960:540,"
                 f"boxblur=18:2,scale={PREP_W}:{PREP_H},eq=brightness=-0.12:saturation=0.8[bg];"
                 f"[b]scale={PREP_W}:{PREP_H}:force_original_aspect_ratio=decrease"
                 f":flags=lanczos[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[out]")
    tmp = out.with_name(out.stem + ".part.jpg")
    try:
        run([require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
             "-noautorotate", "-i", str(src), "-filter_complex", graph,
             "-map", "[out]", "-frames:v", "1", "-q:v", "2", str(tmp)])
        os.replace(tmp, out)
    except (ToolError, OSError):
        tmp.unlink(missing_ok=True)
        return None
    return out


# ------------------------------------------------------------------ 칸 나누기
def plan(media: list[Media], total_frames: int, photo_seconds: float = PHOTO_SECONDS,
         shuffle: bool = False, seed: str = "", titled: bool = True,
         still: bool = False) -> list[Slot]:
    """설교 길이를 사진·영상 칸으로 나눈다. 칸끼리는 XFADE 장면씩 겹친다.

    다 쓰면 처음부터 되풀이한다. 섞기를 고르면 설교마다 같은 순서로 섞는다
    (seed = 영상 id) — 다시 만들어도 같은 영상이 나온다.
    글자를 얹을 때(titled)는 첫 칸이 제목 카드와 구석 글자가 다 나타날 만큼 길어야
    해서, 첫 것이 짧은 영상이면 사진을 앞으로 가져온다. 사진이 없으면 그 영상을
    되풀이해 채운다 (_source).
    """
    if not media:
        raise ToolError("배경으로 쓸 사진이나 영상이 없습니다.")
    total_frames = max(1, int(total_frames))
    order = list(media)
    if shuffle:
        random.Random(seed or "sermon").shuffle(order)
    first_min = (int(math.ceil((CORNER_IN + CORNER_FADE + 0.7) * FPS)) + XFADE) if titled else 0
    if titled and order[0].kind == "video" and order[0].seconds * FPS < first_min:
        photo = next((m for m in order if m.kind == "photo"), None)
        if photo is not None:
            order.remove(photo)
            order.insert(0, photo)

    photo_n = max(int(round(photo_seconds * FPS)), int(MIN_SLOT_SECONDS * FPS), 2 * XFADE + FPS)
    slots: list[Slot] = []
    end = 0
    k = 0
    while True:
        m = order[k % len(order)]
        if m.kind == "photo":
            n = photo_n
        else:
            n = int(min(m.seconds, VIDEO_MAX_SECONDS) * FPS) - 1
        if not slots:
            n = max(n, first_min)
        n = max(n, 2 * XFADE + FPS)
        start = 0 if not slots else end - XFADE
        motion = STILL if still else MOTION_ORDER[len(slots) % len(MOTION_ORDER)]
        # 남는 몫이 2초도 안 되면 따로 칸을 만들지 않고 이 칸을 끝까지 늘린다.
        xf = XFADE if slots else 0
        if start + n >= total_frames - (XFADE + FPS):
            slots.append(Slot(m, max(1, total_frames - start), motion, xf))
            break
        slots.append(Slot(m, n, motion, xf))
        end = start + n
        k += 1
    return slots


def slot_starts(slots: list[Slot]) -> list[int]:
    """각 칸이 이어붙인 화면에서 시작하는 장면 번호 (앞 칸과 겹치는 첫 장면)."""
    out, at = [], 0
    for i, s in enumerate(slots):
        at = at - s.xfade if i else 0
        out.append(at)
        at += s.frames
    return out


def segment_frames(slots: list[Slot], i: int) -> int:
    """조각 i 의 장면 수. 다음 칸과 겹치는 몫은 다음 조각이 그린다."""
    s = slots[i]
    return s.frames if i == len(slots) - 1 else s.frames - slots[i + 1].xfade


# ------------------------------------------------------------------ 편집기의 장면
def new_item(path: str | Path, info: dict | None = None,
             seconds: float = PHOTO_SECONDS) -> dict | None:
    """타임라인에 놓을 장면 하나 (marker 에 그대로 적을 수 있는 사전). 못 쓰면 None.

    kind · 길이 · 크기는 넣을 때 한 번 재어 적어 둔다 — 미리 보기와 타임라인이
    그릴 때마다 파일을 열지 않게.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        return {"path": str(p), "kind": "photo", "len": 0.0, "w": 0, "h": 0,
                "dur": float(seconds), "motion": "auto", "trans": "dissolve",
                "trans_s": 1.0, "in": 0.0}
    if ext not in VIDEO_EXT:
        return None
    info = info if info is not None else _probe(p)
    if not info or info.get("width", 0) <= 0 or info.get("seconds", 0) < 1.0:
        return None
    length = float(info["seconds"])
    return {"path": str(p), "kind": "video", "len": length,
            "w": int(info["width"]), "h": int(info["height"]),
            "dur": round(min(length, VIDEO_MAX_SECONDS), 2), "motion": "auto",
            "trans": "dissolve", "trans_s": 1.0, "in": 0.0}


def item_media(item: dict) -> Media:
    kind = "video" if item.get("kind") == "video" else "photo"
    return Media(Path(item["path"]), kind, seconds=float(item.get("len") or 0.0),
                 width=int(item.get("w") or 0), height=int(item.get("h") or 0),
                 src_start=float(item.get("in") or 0.0))


def _item_frames(item: dict) -> int:
    return max(int(round(float(item.get("dur") or PHOTO_SECONDS) * FPS)), 2 * FPS)


def _item_xfade(item: dict, prev_frames: int, frames: int) -> tuple[int, str]:
    key = item.get("trans") or "dissolve"
    name = TRANSITIONS.get(key, TRANSITIONS["dissolve"])[1]
    if not name:
        return 0, ""
    secs = min(max(float(item.get("trans_s") or 1.0), TRANS_MIN), TRANS_MAX)
    # 넘김이 칸을 다 잡아먹지 않게 — 앞뒤 칸 길이의 1/3 을 넘지 않는다
    return max(1, min(int(round(secs * FPS)), prev_frames // 3, frames // 3)), name


def item_layout(items: list[dict]) -> list[tuple[int, int, int]]:
    """장면마다 (시작 장면, 장면 수, 앞과 겹치는 장면 수) — 설교 길이와 상관없이 차례로.

    타임라인이 설교 끝을 넘는 장면까지 그릴 때 쓴다. 겹침 규칙은 slots_from_items 와 같다.
    """
    out: list[tuple[int, int, int]] = []
    end = 0
    for k, it in enumerate(items):
        n = _item_frames(it)
        xf = _item_xfade(it, out[-1][1], n)[0] if k else 0
        start = end - xf
        out.append((start, n, xf))
        end = start + n
    return out


def slots_from_items(items: list[dict], total_frames: int, fill: str = "repeat",
                     medias: list[Media] | None = None) -> list[Slot]:
    """편집기 타임라인의 장면들을 설교 길이만큼의 칸으로.

    장면은 차례로 이어지고, 넘김 길이만큼 앞 장면과 겹친다. 장면이 설교보다 짧으면
    fill 에 따라 처음부터 되풀이하거나(ghost) 끝 장면을 늘이고, 길면 설교 끝에서 자른다.
    medias 를 주면 그 Media(준비된 사진 경로 등)를 쓴다 — 만들 때. 미리 보기·타임라인은
    주지 않는다. 같은 칸이 나오므로 미리 보기와 완성본이 어긋나지 않는다.
    """
    total_frames = max(1, int(total_frames))
    if not items:
        return []
    if medias is None:
        medias = [item_media(it) for it in items]
    slots: list[Slot] = []
    end = 0
    k = 0
    while True:
        idx = k % len(items)
        ghost = k >= len(items)
        if ghost and fill != "repeat":
            # 끝 장면을 설교 끝까지 늘인다
            last = slots[-1]
            last.frames = total_frames - slot_starts(slots)[-1]
            break
        it, m = items[idx], medias[idx]
        n = _item_frames(it)
        if slots:
            xf, name = _item_xfade(it, slots[-1].frames, n)
        else:
            xf, name = 0, ""
        start = end - xf
        key = it.get("motion") or "auto"
        mot = MOTION_CHOICES.get(key, MOTION_CHOICES["auto"])[1]
        if mot < 0:
            mot = MOTION_ORDER[len(slots) % len(MOTION_ORDER)]
        slot = Slot(m, n, mot, xf, name or "fade", idx, ghost)
        # 남는 몫이 2초도 안 되면 따로 칸을 만들지 않고 이 칸을 끝까지 늘인다.
        if start + n >= total_frames - (XFADE + FPS):
            slot.frames = max(xf + 1, total_frames - start)
            slots.append(slot)
            break
        slots.append(slot)
        end = start + n
        k += 1
    return slots


# ------------------------------------------------------------------ 글자
def _char_em(ch: str) -> float:
    """글자 하나의 폭을 글자 크기의 몇 배로 볼지. 넉넉하게 잡아 화면 밖으로 넘치지 않게."""
    o = ord(ch)
    if ch == " ":
        return 0.30
    if (0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F
            or 0x3000 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF or 0xFF00 <= o <= 0xFFEF):
        return 1.0
    if ch.isdigit():
        return 0.58
    if ch.isupper():
        return 0.70
    if ch.isalpha():
        return 0.58
    if ch in ".,:;!|'`":
        return 0.32
    if ch in "()[]{}":
        return 0.38
    if ch in "—…":
        return 1.0
    return 0.62


def text_px(text: str, size: int) -> float:
    return sum(_char_em(c) for c in text) * size


def wrap(text: str, size: int, max_w: float) -> list[str]:
    """띄어쓰기에서 줄을 바꾼다. 한 낱말이 너무 길면 글자 단위로 끊는다."""
    words = " ".join(text.split()).split(" ")
    lines: list[str] = []
    cur = ""
    for w in words:
        if not w:
            continue
        trial = f"{cur} {w}" if cur else w
        if text_px(trial, size) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = ""
        while text_px(w, size) > max_w and len(w) > 1:
            cut = len(w)
            while cut > 1 and text_px(w[:cut], size) > max_w:
                cut -= 1
            lines.append(w[:cut])
            w = w[cut:]
        cur = w
    if cur:
        lines.append(cur)
    return lines


def balance(lines: list[str], size: int, max_w: float) -> list[str]:
    """두 줄을 길이가 비슷하게 다시 나눈다. '부르심을 따라 떠나는 / 믿음' 처럼 한
    낱말만 아랫줄로 떨어지면 어색하다 → '부르심을 따라 / 떠나는 믿음'.
    """
    words = " ".join(lines).split(" ")
    if len(words) < 2:
        return lines
    best, score = lines, None
    for k in range(1, len(words)):
        a, b = " ".join(words[:k]), " ".join(words[k:])
        wa, wb = text_px(a, size), text_px(b, size)
        if wa > max_w or wb > max_w:
            continue
        s = max(wa, wb)
        if score is None or s < score:
            best, score = [a, b], s
    return best


def fit(text: str, max_w: float, sizes: tuple[int, ...], max_lines: int) -> tuple[list[str], int]:
    """정해 둔 줄 수 안에 들어가는 가장 큰 글자 크기와 그때의 줄들.

    가장 작은 크기로도 넘치면 마지막 줄을 '…' 로 줄인다.
    """
    text = " ".join((text or "").split())
    if not text:
        return [], sizes[0]
    for s in sizes:
        lines = wrap(text, s, max_w)
        if len(lines) <= max_lines:
            return (balance(lines, s, max_w) if len(lines) == 2 else lines), s
    s = sizes[-1]
    lines = wrap(text, s, max_w)[:max_lines]
    last = lines[-1]
    while last and text_px(last + "…", s) > max_w:
        last = last[:-1]
    lines[-1] = last.rstrip() + "…"
    return lines, s


def _drawtext(textfile: str, size: int, color: str, x: str, y: int, shadow: int = 3) -> str:
    return (f"drawtext=fontfile=font.ttf:textfile={textfile}:expansion=none"
            f":fontsize={size}:fontcolor={color}:x={x}:y={y}"
            f":shadowcolor=black@0.55:shadowx=0:shadowy={shadow}")


GOLD = "0xE9C46A"
VERSE_COLOR = "0xF3E6CC"


def make_card(tmpdir: Path, title: str, verse: str, big: bool = False,
              dim: float | None = None, name: str | None = None) -> Path | None:
    """처음 몇 초 동안 화면 가운데에 크게 띄울 제목 카드 (1920x1080 투명 PNG).

    화면 전체를 반쯤 어둡게 덮어 어떤 사진 위에서도 글자가 읽힌다.
    big=True 는 썸네일용이다. 영상 목록에서는 썸네일이 손가락만 하게 보이므로
    글자를 훨씬 키우고 바탕을 조금 더 어둡게 한다.
    tmpdir 에 font.ttf 가 있어야 한다.
    """
    if big:
        t_lines, t_size = fit(title, 1720, (150, 136, 124, 112, 100), 2)
        v_lines, v_size = fit(verse, 1600, (80, 72, 64), 1)
        dim0, rule_w, gap, name0 = 0.52, 180, 46, "thumb_card"
    else:
        t_lines, t_size = fit(title, 1640, (104, 96, 88, 80, 72), 2)
        v_lines, v_size = fit(verse, 1500, (56, 50, 44), 1)
        dim0, rule_w, gap, name0 = 0.46, 120, 34, "card"
    dim = dim0 if dim is None else dim
    name = name or name0
    if not t_lines and not v_lines:
        return None
    t_lead = int(t_size * 1.28)
    if not (t_lines and v_lines):
        gap = 0
    rule = (6 if big else 4) if (t_lines and v_lines) else 0
    block = len(t_lines) * t_lead + gap * 2 + rule + (int(v_size * 1.25) if v_lines else 0)
    top = (HEIGHT - block) // 2 - 10
    parts = []
    for k, line in enumerate(t_lines):
        (tmpdir / f"{name}_t{k}.txt").write_text(line, encoding="utf-8")
        parts.append(_drawtext(f"{name}_t{k}.txt", t_size, "white", "(w-text_w)/2",
                               top + k * t_lead, 5 if big else 3))
    y = top + len(t_lines) * t_lead
    if t_lines and v_lines:
        y += gap
        parts.append(f"drawbox=x=(iw-{rule_w})/2:y={y}:w={rule_w}:h={rule}"
                     f":color={GOLD}@0.95:t=fill")
        y += rule + gap
    if v_lines:
        (tmpdir / f"{name}_v.txt").write_text(v_lines[0], encoding="utf-8")
        parts.append(_drawtext(f"{name}_v.txt", v_size, VERSE_COLOR, "(w-text_w)/2", y,
                               4 if big else 2))
    out = tmpdir / f"{name}.png"
    run([require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=black@{dim}:s={WIDTH}x{HEIGHT}:d=1,format=rgba",
         "-vf", ",".join(parts), "-frames:v", "1", out.name], cwd=tmpdir)
    return out


def make_corner(tmpdir: Path, title: str, verse: str) -> Path | None:
    """제목 카드가 사라진 뒤 왼쪽 위에 계속 둘 작은 글자 (투명 PNG).

    왼쪽에서 오른쪽으로 옅어지는 어두운 바탕을 깔아, 밝은 사진 위에서도 읽힌다.
    """
    t_lines, t_size = fit(title, 1060, (44, 40, 36), 1)
    v_lines, v_size = fit(verse, 1060, (32, 30, 28), 1)
    if not t_lines and not v_lines:
        return None
    pad_x, pad_y = 30, 22
    widest = max([text_px(t_lines[0], t_size) if t_lines else 0,
                  text_px(v_lines[0], v_size) if v_lines else 0])
    w = int(min(1180, widest + pad_x * 2 + 160)) // 2 * 2
    t_h = int(t_size * 1.25) if t_lines else 0
    v_h = int(v_size * 1.25) if v_lines else 0
    h = (pad_y * 2 + t_h + (8 if t_lines and v_lines else 0) + v_h) // 2 * 2
    parts = []
    y = pad_y
    if t_lines:
        (tmpdir / "corner_t.txt").write_text(t_lines[0], encoding="utf-8")
        parts.append(_drawtext("corner_t.txt", t_size, "white", str(pad_x), y, 2))
        y += t_h + 8
    if v_lines:
        (tmpdir / "corner_v.txt").write_text(v_lines[0], encoding="utf-8")
        parts.append(_drawtext("corner_v.txt", v_size, VERSE_COLOR, str(pad_x), y, 2))
    out = tmpdir / "corner.png"
    run([require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi",
         "-i", (f"gradients=s={w}x{h}:c0=0x000000A8:c1=0x00000000:nb_colors=2"
                f":x0=0:y0=0:x1={w}:y1=0:d=1,format=rgba"),
         "-vf", ",".join(parts), "-frames:v", "1", out.name], cwd=tmpdir)
    return out


def make_plain(tmpdir: Path, title: str, verse: str, big: bool = False,
               name: str = "plain") -> Path:
    """배경 사진·영상 없이 만들 때의 화면 한 장 (3840x2160 JPEG).

    짙은 곤색 바탕 가운데에 제목과 본문을 처음부터 끝까지 띄운다. 글자를 그림에
    바로 그려 넣으므로 제목 카드·구석 글자는 따로 얹지 않는다.
    tmpdir 에 font.ttf 가 있으면 글자를 넣고, 없으면 바탕만 만든다.
    """
    card = None
    if (tmpdir / "font.ttf").exists() and (title or verse):
        card = make_card(tmpdir, title, verse, big=big, dim=0.0, name=f"{name}_card")
    out = tmpdir / f"{name}.jpg"
    inputs = ["-f", "lavfi", "-i",
              (f"gradients=s={WIDTH}x{HEIGHT}:c0={PLAIN_TOP}:c1={PLAIN_BOTTOM}:nb_colors=2"
               f":x0=0:y0=0:x1=0:y1={HEIGHT}:d=1")]
    if card is not None:
        inputs += ["-i", str(card)]
        graph = f"[0:v][1:v]overlay=0:0,scale={PREP_W}:{PREP_H}:flags=lanczos,format=yuv444p[out]"
    else:
        graph = f"[0:v]scale={PREP_W}:{PREP_H}:flags=lanczos,format=yuv444p[out]"
    run([require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", *inputs,
         "-filter_complex", graph, "-map", "[out]", "-frames:v", "1", "-q:v", "2",
         out.name], cwd=tmpdir)
    return out


# ------------------------------------------------------------------ 화면 조각
def _motion_exprs(slot: Slot, first: int) -> tuple[str, str, str]:
    """zoompan 의 확대·가로·세로 식. 칸 안에서 몇 번째 장면인지(first + on)로만 정한다."""
    z0, z1, x0, x1, y0, y1 = MOTIONS[slot.motion % len(MOTIONS)]
    span = max(1, slot.frames - 1)
    amp = min(max(MOTION_PER_SECOND * slot.frames / FPS, 0.05), 0.15)
    p = f"(({first}+on)/{span})"
    z = f"1+{amp:.4f}*({z0}+({z1}-{z0})*{p})"
    x = f"(iw-iw/zoom)*({x0}+({x1}-{x0})*{p})"
    y = f"(ih-ih/zoom)*({y0}+({y1}-{y0})*{p})"
    return z, x, y


def _norm(label_out: str) -> str:
    return f"setsar=1,format=yuv420p,settb=1/{FPS},setpts=N[{label_out}]"


def _source(slot: Slot, first: int, count: int, idx: int, label: str) -> tuple[list[str], str]:
    """칸의 장면 first ~ first+count 를 만드는 (입력 인자, 필터)."""
    m = slot.media
    if m.kind == "photo":
        z, x, y = _motion_exprs(slot, first)
        chain = (f"[{idx}:v]format=rgb24,zoompan=z='{z}':x='{x}':y='{y}':d={count}"
                 f":s={WIDTH}x{HEIGHT}:fps={FPS},{_norm(label)}")
        return ["-i", str(m.prepared)], chain
    start = m.src_start + first / FPS
    want = count / FPS
    loop: list[str] = []
    if start + want > m.seconds - 0.05 and m.seconds > 0.5:
        # 칸이 영상보다 길다 — 멈춰 두지 않고 처음부터 되풀이한다. 몇 번째 바퀴든
        # 영상 안의 자리는 (장면 시각 % 영상 길이) 로 정해지므로 앞 조각과 이어진다.
        loop = ["-stream_loop", "-1"]
        start = start % m.seconds
    args = loop + (["-ss", f"{start:.3f}"] if start > 0 else []) + \
        ["-t", f"{want + 1.0:.3f}", "-i", str(m.path)]
    # 마지막 한두 장면이 모자랄 때만 끝 장면을 잠깐 붙든다 (안전망)
    tail = (f"tpad=stop_mode=clone:stop_duration=1.00,trim=end_frame={count},"
            + _norm(label))
    if _fills_frame(m.width, m.height):
        chain = (f"[{idx}:v]fps={FPS},scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
                 f"crop={WIDTH}:{HEIGHT},{tail}")
    else:
        chain = (f"[{idx}:v]fps={FPS},split=2[{label}a][{label}b];"
                 f"[{label}a]scale=480:270:force_original_aspect_ratio=increase,crop=480:270,"
                 f"boxblur=8:2,scale={WIDTH}:{HEIGHT},eq=brightness=-0.10:saturation=0.85[{label}g];"
                 f"[{label}b]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease[{label}f];"
                 f"[{label}g][{label}f]overlay=(W-w)/2:(H-h)/2,{tail}")
    return args, chain


def segment_command(slots: list[Slot], i: int, out: Path, card: Path | None,
                    corner: Path | None, preset: str, crf: int, threads: int,
                    extra: list[str] | None = None) -> list[str]:
    """조각 i 를 만드는 ffmpeg 명령.

    extra 는 모든 조각에 **같은 것**을 넘겨야 한다 (render_background 가 한 번 정한다).
    x264_extra() 는 그때그때 메모리를 보고 ref·bframes 를 바꾸는데, 조각마다 다르면
    화면을 다시 만들지 않고 이어붙인 뒤쪽이 깨진다 — 앞 조각의 설정으로 읽히기 때문이다.
    """
    slot = slots[i]
    count = segment_frames(slots, i)
    last = i == len(slots) - 1
    t0 = slot_starts(slots)[i] / FPS
    dur = count / FPS
    inputs: list[str] = []
    graph: list[str] = []
    k = 0
    xf = slot.xfade if i > 0 else 0
    if xf > 0:
        prev = slots[i - 1]
        a_args, a_chain = _source(prev, prev.frames - xf, xf, k, "pa")
        inputs += a_args
        graph.append(a_chain)
        k += 1
    b_args, b_chain = _source(slot, 0, count, k, "cb")
    inputs += b_args
    graph.append(b_chain)
    k += 1
    if xf > 0:
        graph.append(f"[pa][cb]xfade=transition={slot.transition or 'fade'}"
                     f":duration={xf / FPS:.3f}:offset=0[v0]")
    else:
        graph.append("[cb]null[v0]")
    cur = "v0"
    # 제목 카드는 처음 몇 초에 걸친 조각마다 얹는다 — 편집기에서 첫 장면을 짧게 두면
    # 카드가 둘째 조각까지 이어진다. 카드 시간(0초부터)으로 나타나고 사라지게 한 뒤
    # 이 조각의 몫만 잘라 쓴다. 앞쪽 조각뿐이라 버리는 장면이 몇 초 안 된다.
    card_end = CARD_OUT + CARD_FADE + 0.2
    if card is not None and t0 < card_end:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{card_end:.2f}", "-i", str(card)]
        graph.append(f"[{k}:v]format=rgba,"
                     f"fade=t=in:st={CARD_IN}:d={CARD_FADE}:alpha=1,"
                     f"fade=t=out:st={CARD_OUT}:d={CARD_FADE}:alpha=1"
                     + (f",trim=start={t0:.4f},setpts=PTS-STARTPTS" if t0 > 0 else "")
                     + "[card]")
        graph.append(f"[{cur}][card]overlay=0:0:eof_action=pass[v1]")
        cur = "v1"
        k += 1
    if corner is not None and t0 + dur > CORNER_IN:
        if t0 < CORNER_IN + CORNER_FADE:
            # 나타나는 중에 걸친 조각 — 0초부터 만들어 이 조각 몫만 쓴다
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{t0 + dur + 0.5:.2f}",
                       "-i", str(corner)]
            graph.append(f"[{k}:v]format=rgba,fade=t=in:st={CORNER_IN}:d={CORNER_FADE}:alpha=1"
                         + (f",trim=start={t0:.4f},setpts=PTS-STARTPTS" if t0 > 0 else "")
                         + "[cor]")
        else:
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur + 0.5:.2f}",
                       "-i", str(corner)]
            graph.append(f"[{k}:v]format=rgba[cor]")
        graph.append(f"[{cur}][cor]overlay={CORNER_X}:{CORNER_Y}:eof_action=pass[v2]")
        cur = "v2"
        k += 1
    ends = []
    if i == 0:
        ends.append(f"fade=t=in:st=0:d={BLACK_IN}")
    if last:
        ends.append(f"fade=t=out:st={max(0.0, dur - BLACK_OUT):.3f}:d={BLACK_OUT}")
    graph.append(f"[{cur}]{','.join(ends + ['format=yuv420p'])}[vout]")
    return [require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            *inputs,
            "-filter_complex", ";".join(graph),
            "-map", "[vout]", "-an", "-frames:v", str(count),
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-threads", str(threads),
            *(machine.x264_extra() if extra is None else extra), "-r", str(FPS), str(out)]


def _workers() -> tuple[int, int]:
    """(동시에 만들 조각 수, 조각마다 줄 스레드 수).

    zoompan 은 한 코어만 쓰므로 코어가 넉넉하면 조각을 동시에 만드는 편이 빠르다.
    조각 하나에 메모리가 500MB 안팎 들어, 더 쓸 수 있는 양에 맞춰 줄인다.
    """
    cores = os.cpu_count() or 2
    jobs = 1 if cores < 4 else min(3, cores // 3)
    have = machine.headroom_mb()
    if have is not None:
        jobs = max(1, min(jobs, int(have // 900)))
    threads = max(1, min(cores // jobs, machine.ffmpeg_threads()))
    return jobs, threads


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}시간 {seconds % 3600 // 60}분"
    if seconds >= 60:
        return f"{seconds // 60}분 {seconds % 60:02d}초"
    return f"{seconds}초"


def render_background(slots: list[Slot], tmpdir: Path, card: Path | None,
                      corner: Path | None, preset: str, crf: int,
                      extra: list[str] | None = None) -> list[Path]:
    """화면 조각을 모두 만든다. 만든 조각 파일을 차례대로 돌려준다."""
    jobs, threads = _workers()
    if extra is None:
        extra = machine.x264_extra()
    outs = [tmpdir / f"seg{i:04d}.mp4" for i in range(len(slots))]
    cmds = [segment_command(slots, i, outs[i], card, corner, preset, crf, threads, extra)
            for i in range(len(slots))]
    total = len(cmds)
    print(f"  화면을 조각 {total}개로 나눠 만듭니다"
          + (f" — 한 번에 {jobs}개씩" if jobs > 1 else "")
          + f" (x264 {preset}, crf {crf})", flush=True)
    started = time.monotonic()
    done = 0
    shown = -1
    retry: list[int] = []
    pool = cf.ThreadPoolExecutor(max_workers=jobs)
    try:
        futures = {pool.submit(run, cmd): i for i, cmd in enumerate(cmds)}
        for fut in cf.as_completed(futures):
            i = futures[fut]
            try:
                fut.result()
            except ToolError as exc:
                if not is_memory_error(exc):
                    for f in futures:
                        f.cancel()
                    raise ToolError(f"화면 조각 {i + 1}/{total} 을 만들지 못했습니다 "
                                    f"({slots[i].media.path.name})\n{exc}") from exc
                retry.append(i)
                continue
            done += 1
            pct = done * 100 // total
            if pct // 5 != shown or done == total:
                shown = pct // 5
                took = time.monotonic() - started
                left = took / done * (total - done)
                print(f"  화면 {done}/{total} ({pct}%) · 지난 {_fmt_time(took)}"
                      + (f" · 남은 시간 약 {_fmt_time(left)}" if done < total else ""),
                      flush=True)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if retry:
        print(f"  메모리가 모자라 넘어진 조각 {len(retry)}개를 하나씩, 스레드 1개로 다시 합니다",
              flush=True)
        for i in sorted(retry):
            run(segment_command(slots, i, outs[i], card, corner, preset, crf, 1, extra))
    return outs


def concat_video(pieces: list[Path], out: Path) -> Path:
    """같은 설정으로 만든 조각을 화면 그대로 이어붙인다 (다시 인코딩하지 않는다)."""
    lst = out.with_suffix(".txt")
    # 경로에 ' 가 있으면 concat 목록 문법이 깨진다 ('\'' 로 바꿔 적는다).
    lst.write_text("".join("file '" + p.resolve().as_posix().replace("'", "'\\''") + "'\n"
                           for p in pieces), encoding="utf-8")
    run([require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-an", str(out)])
    return out


# ------------------------------------------------------------------ 소리
def speech_wav(src: Path, ranges: list[tuple[float, float]], out: Path, cfg,
               audio_src: Path | None = None) -> float:
    """설교 구간을 이어붙이고 다듬은 말소리 (48kHz 모노 16비트). 길이(초)를 돌려준다.

    설교 mp3 와 같은 다듬기를 건다 (cut.audio_chain): 또렷하게 → 잡음 줄이기 →
    방송 음량. 끝에 TAIL_PAD 초의 조용함을 덧붙여 화면이 닫힐 몫을 둔다.
    """
    s = Path(audio_src) if audio_src else Path(src)
    if not s.exists():
        raise ToolError(f"소리 파일이 없습니다: {s}")
    parts, labels = [], []
    for i, (a, b) in enumerate(ranges):
        parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[a{i}]")
    graph = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(ranges)}:v=0:a=1[ac]"
    chain = cut_mod.audio_chain(cfg.denoise, cfg.normalize_audio, cfg.target_lufs,
                                cfg.target_peak_db, bool(getattr(cfg, "enhance_speech", False)))
    graph += (f";[ac]{chain + ',' if chain else ''}aresample=48000,"
              f"apad=pad_dur={TAIL_PAD},"
              "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono[aout]")
    run_stream([require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-stats",
                "-i", str(s), "-filter_complex", graph, "-map", "[aout]",
                "-c:a", "pcm_s16le", str(out)],
               tag="[소리] ", total=sum(b - a for a, b in ranges) + TAIL_PAD)
    with wave.open(str(out), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def music_lufs(path: Path) -> float | None:
    """음악의 통합 음량(LUFS). 앞 10분만 잰다. 못 재면 None."""
    try:
        proc = subprocess.run(
            [require("ffmpeg"), "-hide_banner", "-nostats", "-nostdin", "-t", "600",
             "-i", str(path), "-vn",
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=tool_env(), timeout=900,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError, ToolError):
        return None
    text = proc.stderr or ""
    start, end = text.rfind("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = float(json.loads(text[start:end + 1]).get("input_i"))
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) and value > -70 else None


def _intro(intro: Path, tmpdir: Path, preset: str, crf: int, threads: int,
           extra: list[str]) -> tuple[Path, Path, float]:
    """시작 영상을 이 영상과 같은 설정으로 다시 만든다 (화면 조각, 소리 wav, 길이)."""
    seg = tmpdir / "seg_intro.mp4"
    run([require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-i", str(intro),
         "-vf", (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                 f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,fps={FPS},"
                 "setsar=1,format=yuv420p"),
         "-an", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
         "-pix_fmt", "yuv420p", "-profile:v", "high", "-threads", str(threads),
         *extra, "-r", str(FPS), str(seg)])
    frames = 0
    try:
        out = run([require("ffprobe"), "-v", "error", "-select_streams", "v:0",
                   "-count_packets", "-show_entries", "stream=nb_read_packets",
                   "-of", "csv=p=0", str(seg)])
        frames = int(out.strip().split(",")[0])
    except (ToolError, ValueError, IndexError):
        frames = int(round(ffprobe_duration(seg) * FPS))
    seconds = frames / FPS
    wav = tmpdir / "intro.wav"
    if has_audio(intro):
        run([require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", str(intro), "-vn",
             "-af", f"aresample=48000,apad,atrim=end={seconds:.4f}",
             "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(wav)])
    else:
        run([require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{seconds:.4f}",
             "-c:a", "pcm_s16le", str(wav)])
    return seg, wav, seconds


def mux(video: Path, speech: Path, out: Path, seconds: float, music: Path | None = None,
        music_gain_db: float = 0.0, gain_wav: Path | None = None,
        intro_wav: Path | None = None, title: str = "") -> Path:
    """화면(복사)과 소리를 하나로. 음악이 있으면 이득 곡선을 곱해 말소리 아래에 깐다.

    모노 말소리를 양쪽 스피커에 나눠 담을 때 각각 -3dB(0.7071) 로 담는다. 그대로
    복사하면 음량 측정(BS.1770)이 두 채널을 더해 재므로 -16 LUFS 가 -13 이 된다
    (2026-11-02 실측 -13.1).
    """
    inputs = ["-i", str(video), "-i", str(speech)]
    graph = ["[1:a]pan=stereo|c0=0.7071*c0|c1=0.7071*c0,"
             "aformat=sample_fmts=fltp:sample_rates=48000[sp]"]
    main = "sp"
    k = 2
    if music is not None and gain_wav is not None:
        inputs += ["-stream_loop", "-1", "-i", str(music), "-i", str(gain_wav)]
        fade_out = max(0.0, seconds - 4.0)
        graph.append(f"[{k}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                     f"atrim=end={seconds:.3f},asetpts=N/SR/TB,volume={music_gain_db:.2f}dB,"
                     f"afade=t=in:st=0:d=3,afade=t=out:st={fade_out:.3f}:d=4[mu]")
        graph.append(f"[{k + 1}:a]aresample=48000,pan=stereo|c0=c0|c1=c0,"
                     "aformat=sample_fmts=fltp:sample_rates=48000[gn]")
        graph.append("[mu][gn]amultiply[md]")
        graph.append("[sp][md]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                     "alimiter=limit=0.94:level=disabled[mix]")
        main = "mix"
        k += 2
    if intro_wav is not None:
        inputs += ["-i", str(intro_wav)]
        graph.append(f"[{k}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[ia]")
        graph.append(f"[ia][{main}]concat=n=2:v=0:a=1[aout]")
    else:
        graph.append(f"[{main}]anull[aout]")
    run_stream([require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-stats",
                "-nostdin", *inputs, "-filter_complex", ";".join(graph),
                "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                *(["-metadata", f"title={title}"] if title else []),
                "-movflags", "+faststart", str(out)],
               tag="[합치기] ", total=seconds)
    return out


# ------------------------------------------------------------------ 썸네일 · 설명 글
def _first_frame_graph(m: Media, idx: int, label: str) -> tuple[list[str], str]:
    """칸 첫 장면과 같은 그림 하나 (썸네일·미리보기용)."""
    if m.kind == "photo":
        # 첫 칸은 늘 '가운데로 다가가기' 로 시작하므로 첫 장면은 준비한 그림 그대로다.
        return (["-i", str(m.prepared)],
                f"[{idx}:v]scale={WIDTH}:{HEIGHT}:flags=lanczos,setsar=1,format=yuv420p[{label}]")
    at = m.src_start + min(2.0, max(0.0, (m.seconds - m.src_start) / 2))
    args = ["-ss", f"{at:.2f}", "-i", str(m.path)]
    if _fills_frame(m.width, m.height):
        chain = (f"[{idx}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
                 f"crop={WIDTH}:{HEIGHT},setsar=1,format=yuv420p[{label}]")
    else:
        chain = (f"[{idx}:v]split=2[{label}a][{label}b];"
                 f"[{label}a]scale=480:270:force_original_aspect_ratio=increase,crop=480:270,"
                 f"boxblur=8:2,scale={WIDTH}:{HEIGHT},eq=brightness=-0.10:saturation=0.85[{label}g];"
                 f"[{label}b]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease[{label}f];"
                 f"[{label}g][{label}f]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[{label}]")
    return args, chain


def still(m: Media, out: Path, overlay: Path | None = None, at_x: int = 0, at_y: int = 0,
          size: tuple[int, int] = (WIDTH, HEIGHT), quality: int = 2) -> Path:
    """그림 한 장: 배경 첫 장면 + (있으면) 글자 PNG. 썸네일과 미리보기가 쓴다."""
    args, chain = _first_frame_graph(m, 0, "bg")
    graph = [chain]
    cur = "bg"
    if overlay is not None:
        args += ["-i", str(overlay)]
        graph.append(f"[{cur}][1:v]overlay={at_x}:{at_y}[ov]")
        cur = "ov"
    if size != (WIDTH, HEIGHT):
        graph.append(f"[{cur}]scale={size[0]}:{size[1]}:flags=lanczos[sz]")
        cur = "sz"
    run([require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-nostdin", *args,
         "-filter_complex", ";".join(graph), "-map", f"[{cur}]", "-frames:v", "1",
         "-q:v", str(quality), str(out)])
    return out


def description(title: str, verse: str, upload_date: str = "") -> str:
    """올릴 곳의 제목·설명 칸에 붙여 넣을 글. 고쳐 쓰기 쉽게 단순하게 둔다."""
    head = " | ".join(x for x in (title.strip(), verse.strip()) if x) or "주일 설교"
    when = ""
    d = (upload_date or "").strip()
    if len(d) == 8 and d.isdigit():
        when = f"{int(d[:4])}년 {int(d[4:6])}월 {int(d[6:])}일 설교"
    lines = ["■ 제목 (복사해서 제목 칸에)", head, "",
             "■ 설명 (복사해서 설명 칸에)"]
    if title.strip():
        lines.append(title.strip())
    if verse.strip():
        lines.append(f"본문: {verse.strip()}")
    if when:
        lines += ["", when]
    lines += ["", "창의진교회"]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 편집기용 작은 그림
def _look_graph(kind: str, w: int, h: int, fills: bool, turn: str = "") -> str:
    """완성본과 같은 짜임(꽉 채우기 / 가운데 + 흐린 양옆)을 w x h 로. 입력 [0:v] → [out]."""
    if fills:
        y = "(ih-oh)*0.35" if kind == "photo" else "(ih-oh)/2"
        return (f"[0:v]{turn}scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h}:(iw-ow)/2:{y},setsar=1[out]")
    bw, bh = max(16, w // 4) // 2 * 2, max(9, h // 4) // 2 * 2
    return (f"[0:v]{turn}split=2[a][b];"
            f"[a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=4:2,scale={w}:{h},eq=brightness=-0.10:saturation=0.85[g];"
            f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[f];"
            f"[g][f]overlay=(W-w)/2:(H-h)/2,setsar=1[out]")


def thumb_jpeg(item: dict, w: int = 320, h: int = 180) -> bytes | None:
    """장면이 화면에 어떻게 놓이는지 보여 주는 작은 그림 (JPEG 바이트). 못 만들면 None."""
    path = Path(item["path"])
    try:
        if item.get("kind") == "video":
            at = float(item.get("in") or 0.0) + min(1.0, float(item.get("len") or 0) / 3)
            fills = _fills_frame(int(item.get("w") or 0), int(item.get("h") or 0))
            args = ["-ss", f"{at:.2f}", "-i", str(path)]
            graph = _look_graph("video", w, h, fills)
        else:
            orient = exif_orientation(path) if path.suffix.lower() in (".jpg", ".jpeg") else 1
            iw, ih = int(item.get("w") or 0), int(item.get("h") or 0)
            if iw <= 0:
                info = json.loads(run([require("ffprobe"), "-v", "error", "-select_streams",
                                       "v:0", "-show_entries", "stream=width,height", "-of",
                                       "json", str(path)]) or "{}")
                st0 = (info.get("streams") or [{}])[0]
                iw, ih = int(st0.get("width") or 0), int(st0.get("height") or 0)
                if orient in (5, 6, 7, 8):
                    iw, ih = ih, iw
                item["w"], item["h"] = iw, ih
            turn = (ORIENT[orient] + ",") if orient in ORIENT else ""
            args = ["-noautorotate", "-i", str(path)]
            graph = _look_graph("photo", w, h, _fills_frame(iw, ih), turn)
        proc = subprocess.run(
            [require("ffmpeg"), "-v", "error", "-nostdin", *args, "-filter_complex", graph,
             "-map", "[out]", "-frames:v", "1", "-q:v", "4", "-f", "image2pipe",
             "-vcodec", "mjpeg", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=tool_env(), timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError, ToolError, ValueError):
        return None
    return proc.stdout if proc.stdout[:2] == b"\xff\xd8" else None


PROXY_FPS, PROXY_W, PROXY_H, PROXY_MAX = 12, 384, 216, 150.0


def video_proxy(item: dict) -> list[bytes]:
    """미리 보기에서 재생할 작은 영상 장면들 (JPEG 바이트, 초당 PROXY_FPS 장).

    처음부터 (쓰는 곳 끝 또는 PROXY_MAX 초)까지. 완성본과 같은 짜임으로 그린다.
    """
    path = Path(item["path"])
    upto = min(float(item.get("len") or PROXY_MAX),
               max(float(item.get("in") or 0) + float(item.get("dur") or 0) + 1.0, 10.0),
               PROXY_MAX)
    fills = _fills_frame(int(item.get("w") or 0), int(item.get("h") or 0))
    graph = _look_graph("video", PROXY_W, PROXY_H, fills).replace(
        "[0:v]", f"[0:v]fps={PROXY_FPS},", 1)
    try:
        proc = subprocess.run(
            [require("ffmpeg"), "-v", "error", "-nostdin", "-t", f"{upto:.2f}", "-i", str(path),
             "-filter_complex", graph, "-map", "[out]", "-q:v", "6", "-f", "image2pipe",
             "-vcodec", "mjpeg", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=tool_env(), timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError, ToolError):
        return []
    data = proc.stdout
    frames, at = [], 0
    while True:
        a = data.find(b"\xff\xd8", at)
        if a < 0:
            break
        b = data.find(b"\xff\xd9", a + 2)
        if b < 0:
            break
        frames.append(data[a:b + 2])
        at = b + 2
    return frames


def speech_peaks(src: Path, ranges: list[tuple[float, float]], per_second: int = 20) -> list[float]:
    """설교 구간 소리의 크기 (0~1, 초당 per_second 개). 타임라인의 파형 그림에 쓴다."""
    import numpy as np

    rate = 8000
    hop = rate // per_second
    out: list[float] = []
    for a, b in ranges:
        try:
            proc = subprocess.run(
                [require("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{a:.3f}", "-t",
                 f"{max(0.0, b - a):.3f}", "-i", str(src), "-vn", "-ac", "1", "-ar", str(rate),
                 "-f", "s16le", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=tool_env(), timeout=600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError, ToolError):
            return []
        x = np.frombuffer(proc.stdout[: len(proc.stdout) // 2 * 2], dtype="<i2").astype(np.float32)
        n = len(x) // hop
        if n:
            out.extend(np.abs(x[: n * hop]).reshape(n, hop).max(axis=1) / 32768.0)
    if not out:
        return []
    arr = np.asarray(out, dtype=np.float32)
    top = float(np.percentile(arr, 99.5)) or 1.0
    return [float(v) for v in np.clip(arr / top, 0.0, 1.0)]


def motion_rect(motion: int, frames: int, local: float) -> tuple[float, float, float]:
    """사진 칸의 local 번째 장면에서 잘라 볼 자리 (왼쪽, 위, 폭) — 그림 크기에 대한 비율.

    _motion_exprs 와 같은 식이다. 편집기의 미리 보기가 이걸로 그린다.
    """
    z0, z1, x0, x1, y0, y1 = MOTIONS[motion % len(MOTIONS)]
    p = min(max(local / max(1, frames - 1), 0.0), 1.0)
    amp = min(max(MOTION_PER_SECOND * frames / FPS, 0.05), 0.15)
    zoom = 1 + amp * (z0 + (z1 - z0) * p)
    size = 1.0 / zoom
    return (1 - size) * (x0 + (x1 - x0) * p), (1 - size) * (y0 + (y1 - y0) * p), size


# ------------------------------------------------------------------ 미리보기
def preview(opts: Options, cfg, out_dir: Path) -> tuple[list[Path], str]:
    """대화상자의 [미리 보기]. (그림 경로들, 알릴 말).

    첫 사진(영상)에 제목 카드를 얹은 장면과, 그 뒤 구석 글자만 남은 장면 두 장.
    """
    from .shorts import find_font

    media, _skipped = scan(opts.folder) if opts.folder else ([], [])
    if not media:
        return _preview_plain(opts, out_dir)
    first = None
    for m in media:
        if m.kind == "photo":
            m.prepared = prepare_photo(m, Path(cfg.cache_dir) / "visual_photos")
            if m.prepared is None:
                continue
        first = m
        break
    if first is None:
        return [], "사진을 열지 못했습니다."
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("preview_*.jpg"):     # 지난 미리보기는 지운다
        old.unlink(missing_ok=True)
    font = find_font()
    shots: list[Path] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        card = corner = None
        if font:
            shutil.copyfile(font, tmpdir / "font.ttf")
            card = make_card(tmpdir, opts.title, opts.verse)
            corner = make_corner(tmpdir, opts.title, opts.verse) if opts.corner else None
        stamp = int(time.time() * 1000)
        a = out_dir / f"preview_card_{stamp}.jpg"
        still(first, a, card, size=(960, 540), quality=3)
        shots.append(a)
        b = out_dir / f"preview_corner_{stamp}.jpg"
        still(first, b, corner, CORNER_X, CORNER_Y, size=(960, 540), quality=3)
        shots.append(b)
    note = "" if font else "한글 글꼴을 찾지 못해 글자는 들어가지 않습니다."
    return shots, note


def _preview_plain(opts: Options, out_dir: Path) -> tuple[list[Path], str]:
    """배경 없이 만들 때의 미리보기. 처음부터 끝까지 같은 화면이라 두 칸에 같은 그림."""
    from .shorts import find_font

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("preview_*.jpg"):
        old.unlink(missing_ok=True)
    font = find_font()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        if font:
            shutil.copyfile(font, tmpdir / "font.ttf")
        plain = make_plain(tmpdir, opts.title, opts.verse)
        a = out_dir / f"preview_plain_{int(time.time() * 1000)}.jpg"
        still(Media(plain, "photo", prepared=plain), a, None, size=(960, 540), quality=3)
    note = ("배경 사진·영상 없이, 짙은 곤색 바탕에 제목·본문을 처음부터 끝까지 띄웁니다."
            if font else "한글 글꼴을 찾지 못해 바탕만 나옵니다.")
    return [a, a], note


# ------------------------------------------------------------------ 한 번에
def build(marker, cfg, src: Path, opts: Options) -> dict:
    """음성 설교 → 사진·영상을 입힌 영상 + 썸네일 + 설명 글."""
    from . import export as export_mod
    from .shorts import find_font

    ranges = [(s, e) for s, e, _ in marker.ranges()]
    if not ranges:
        raise ToolError("내보낼 구간이 없습니다. 설교 구간을 먼저 정해 주세요.")
    bad = [(s, e) for s, e in ranges if e - s <= 0.2]
    if bad:
        raise ToolError(f"길이가 0에 가까운 구간이 있습니다: {bad[:3]}")
    total = sum(e - s for s, e in ranges)

    ok, note = machine.export_check(WIDTH, HEIGHT, 1)
    if note:
        for line in note.split("\n"):
            print(("  [메모리] " if line else "") + line, flush=True)
    if not ok:
        raise ToolError(note)

    # 편집기 타임라인에 놓은 장면이 있으면 그대로, 없으면 폴더를 예전처럼 차례로.
    items: list[dict] = []
    skipped: list[str] = []
    for it in opts.items or []:
        if Path(str(it.get("path", ""))).is_file():
            items.append(it)
        else:
            skipped.append(f"{Path(str(it.get('path', ''))).name} — 파일이 없습니다 (옮기거나 지웠나요?)")
    if items:
        media = [item_media(it) for it in items]
    elif opts.items:
        media = []
    else:
        media, skipped = scan(opts.folder) if opts.folder else ([], [])
    # 배경 사진·영상이 없어도 만든다 — 짙은 곤색 바탕에 제목·본문을 띄운다.
    plain = not media

    cfg.ensure_dirs()
    stem = export_mod.safe_stem(marker)
    out = cfg.export_dir / f"{stem}_영상.mp4"
    thumb = cfg.export_dir / f"{stem}_썸네일.jpg"
    desc = cfg.export_dir / f"{stem}_설명글.txt"

    # 조각 + 완성본이 잠깐 같이 있다. 대충 6Mbps 로 보고 두 배 반을 잡는다.
    need = total * 6e6 / 8 * 2.5
    try:
        free = shutil.disk_usage(cfg.cache_dir).free
    except OSError:
        free = None
    if free is not None and free < need:
        raise ToolError(f"디스크 자리가 모자랍니다 — {need / 1e9:.1f}GB 쯤 필요한데 "
                        f"{free / 1e9:.1f}GB 남았습니다.\n  {cfg.cache_dir}")

    photos = sum(1 for m in media if m.kind == "photo")
    videos = len(media) - photos
    if plain:
        print(f"음성 설교를 영상으로 만듭니다 — {_fmt_time(total)}, 배경 사진·영상 없이 "
              "짙은 곤색 바탕에 제목·본문", flush=True)
        if opts.folder:
            print(f"  고른 폴더에 쓸 수 있는 사진·영상이 없습니다: {opts.folder}", flush=True)
    else:
        print(f"음성 설교에 화면을 입힙니다 — {_fmt_time(total)}, "
              f"사진 {photos}장 · 영상 {videos}개", flush=True)
    for why in skipped:
        print(f"  건너뜀: {why}", flush=True)

    preset = str(getattr(cfg, "visual_preset", "veryfast") or "veryfast")
    preset, why = machine.x264_preset(preset)
    if why:
        print(f"  [영상] {why}", flush=True)
    crf = int(getattr(cfg, "visual_crf", 20) or 20)
    font = find_font()
    if not font and (opts.title or opts.verse):
        print("  한글 글꼴을 찾지 못해 제목 글자는 건너뜁니다", flush=True)

    with tempfile.TemporaryDirectory(dir=str(cfg.cache_dir), prefix="visual_") as tmp:
        tmpdir = Path(tmp)

        # 1) 소리 — 설교 mp3 와 같은 다듬기
        print("  설교 구간의 소리를 다듬습니다", flush=True)
        voice = export_mod.voice_audio(marker, cfg, src)
        export_mod.say_audio_steps(cfg, voice)
        speech = tmpdir / "speech.wav"
        seconds = speech_wav(Path(src), ranges, speech, cfg, audio_src=voice)
        frames = int(math.ceil(seconds * FPS))

        # 배경 음악은 화면을 그리기 **전에** 살핀다. 읽지 못하는 파일이면 수십 분
        # 그린 뒤 마지막 합치기에서 넘어지게 되기 때문이다.
        music = gain_wav = None
        music_gain = 0.0
        music_note = "없음"
        if opts.music:
            mp = Path(opts.music)
            if not mp.is_file():
                print(f"  배경 음악 파일이 없어 넣지 않습니다: {mp}", flush=True)
            elif not has_audio(mp):
                print(f"  배경 음악에서 소리를 읽지 못해 넣지 않습니다: {mp.name}", flush=True)
            else:
                lufs = music_lufs(mp)
                if lufs is None:
                    print("  음악 크기를 재지 못해 보통 음원 크기로 봅니다", flush=True)
                    lufs = MUSIC_GUESS_LUFS
                base = MUSIC_LEVELS.get(opts.music_level, MUSIC_LEVELS["low"])
                music_gain = float(cfg.target_lufs) - lufs
                regions, keys = ducking.plan(speech, seconds, base)
                gain_wav = ducking.write_gain_wav(
                    tmpdir / "music_gain.wav", ducking.gain_curve(keys, seconds, base))
                music = mp
                talk = sum(b - a for a, b in regions)
                music_note = (f"{mp.name} ({MUSIC_LABELS.get(opts.music_level, '')}, "
                              f"말씀하실 때 {ducking.REDUCE_DB:.0f}dB 더 줄임)")
                print(f"  배경 음악: {mp.name} — 음악 {lufs:.1f} LUFS 를 말소리에 맞추고 "
                      f"쉬실 때 {base:.0f}dB, 말씀하실 때 {base - ducking.REDUCE_DB:.0f}dB "
                      f"(말소리 구간 {len(regions)}곳, {talk / max(seconds, 1) * 100:.0f}%)",
                      flush=True)

        # 2) 사진 준비
        if photos:
            print(f"  사진 {photos}장을 준비합니다 (한 번 하면 다음부터는 다시 안 합니다)",
                  flush=True)
        cache = Path(cfg.cache_dir) / "visual_photos"
        if font:
            shutil.copyfile(font, tmpdir / "font.ttf")
        usable: list[Media] = []
        kept: list[dict] = []           # usable 과 짝을 이루는 편집기 장면
        if plain:
            bg = make_plain(tmpdir, opts.title, opts.verse)
            usable.append(Media(bg, "photo", prepared=bg))
        for j, m in enumerate(media):
            if m.kind == "photo":
                m.prepared = prepare_photo(m, cache)
                if m.prepared is None:
                    print(f"  건너뜀: {m.path.name} — 사진을 열지 못했습니다", flush=True)
                    continue
            usable.append(m)
            if items:
                kept.append(items[j])
        if not usable:
            print("  사진·영상을 하나도 열지 못해 곤색 바탕으로 만듭니다", flush=True)
            plain = True
            kept = []
            bg = make_plain(tmpdir, opts.title, opts.verse)
            usable.append(Media(bg, "photo", prepared=bg))

        # 3) 글자 — 칸을 나누기 전에 만든다. 글자가 없으면 첫 칸을 늘릴 까닭이 없다.
        card = corner = None
        if font and (opts.title or opts.verse) and not plain:   # 곤색 바탕은 글자가 그림에 있다
            card = make_card(tmpdir, opts.title, opts.verse)
            corner = make_corner(tmpdir, opts.title, opts.verse) if opts.corner else None

        # 4) 칸 나누기
        if kept and not plain:
            slots = slots_from_items(kept, frames, opts.fill, medias=usable)
        else:
            slots = plan(usable, frames, opts.photo_seconds, opts.shuffle, marker.video_id,
                         titled=card is not None or corner is not None, still=plain)
        rounds = len(slots) / max(1, len(usable))
        if plain:
            print(f"  곤색 바탕 화면을 조각 {len(slots)}개로 만듭니다", flush=True)
        elif kept:
            ghosts = sum(1 for sl in slots if sl.ghost)
            print(f"  타임라인 장면 {len(kept)}개 → 칸 {len(slots)}개"
                  + (f" (모자란 {ghosts}칸은 처음부터 되풀이)" if ghosts else ""), flush=True)
        else:
            print(f"  칸 {len(slots)}개 — "
                  + (f"사진·영상 {len(usable)}개를 {rounds:.1f}번 돌려 씁니다"
                     if rounds > 1.05 else f"사진·영상 {len(slots)}개를 한 번씩 씁니다"),
                  flush=True)

        # 5) 화면 조각 → 이어붙이기
        # x264 설정은 여기서 한 번만 정해 모든 조각(시작 영상 포함)에 똑같이 쓴다.
        extra = machine.x264_extra()
        pieces = render_background(slots, tmpdir, card, corner, preset, crf, extra)
        intro_wav = None
        intro = Path(getattr(cfg, "intro_path", "") or "")
        if str(intro) not in ("", ".") and intro.exists():
            print(f"  시작 영상을 앞에 붙입니다: {intro.name}", flush=True)
            seg, intro_wav, lead = _intro(intro, tmpdir, preset, crf,
                                          machine.ffmpeg_threads(), extra)
            pieces = [seg] + pieces
        print("  조각을 이어붙입니다 — 화면은 그대로 복사합니다", flush=True)
        background = concat_video(pieces, tmpdir / "background.mp4")

        # 6) 합치기 → 제자리에
        print("  화면과 소리를 합칩니다", flush=True)
        stage = tmpdir / "final.mp4"
        try:
            mux(background, speech, stage, seconds, music, music_gain, gain_wav, intro_wav,
                title=opts.title)
        except ToolError as exc:
            if music is None:
                raise
            # 그려 둔 화면을 버리지 않는다 — 음악만 빼고 다시 합친다.
            print(f"  배경 음악을 섞지 못해 음악 없이 합칩니다 ({str(exc)[-200:]})", flush=True)
            music_note = "넣지 못함 (음악 파일을 확인하세요)"
            mux(background, speech, stage, seconds, None, 0.0, None, intro_wav,
                title=opts.title)
        near = out.with_name(out.name + ".new")
        try:
            os.replace(stage, near)            # 같은 디스크면 몇 GB 를 복사하지 않고 옮긴다
        except OSError:
            shutil.copyfile(stage, near)
        try:
            os.replace(near, out)
        except OSError as exc:
            raise ToolError(
                f"만든 영상을 제자리에 놓지 못했습니다: {out.name}\n  {exc}\n"
                "재생기에서 그 파일을 열어 두었다면 닫고 다시 만들어 주세요.\n"
                f"  새로 만든 것은 여기 있습니다: {near}") from exc

        # 7) 썸네일 · 설명 글. 썸네일은 영상 첫 장면에 큰 글자 카드를 얹는다.
        first = slots[0].media
        try:
            if plain:
                # 곤색 바탕 썸네일은 글자를 썸네일 크기(크게)로 다시 그린다
                tb = make_plain(tmpdir, opts.title, opts.verse, big=True, name="plain_thumb")
                still(Media(tb, "photo", prepared=tb), thumb, None, size=(1280, 720), quality=2)
            else:
                big = (make_card(tmpdir, opts.title, opts.verse, big=True)
                       if card is not None else None)
                still(first, thumb, big, size=(1280, 720), quality=2)
        except ToolError as exc:
            print(f"  썸네일을 만들지 못했습니다: {exc}", flush=True)
            thumb = None
        desc.write_text(description(opts.title, opts.verse, marker.upload_date),
                        encoding="utf-8-sig")

    made = ffprobe_duration(out)
    size = out.stat().st_size / (1024 * 1024)
    print(f"  완성 — {_fmt_time(made)}, {size:.0f}MB", flush=True)
    result: dict = {"video": out}
    if thumb is not None:
        result["썸네일"] = thumb
    result["설명 글"] = desc
    result["길이"] = _fmt_time(made)
    result["크기"] = f"{size:.0f}MB"
    result["화면"] = ("짙은 곤색 바탕에 제목·본문" if plain
                    else f"사진 {photos}장 · 영상 {videos}개 → 칸 {len(slots)}개")
    result["배경 음악"] = music_note
    return result
