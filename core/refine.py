"""자막 기반 경계 미세보정.

화자 분리는 '누가 말하는가'만 알 뿐 봉독과 설교를 구분하지 못한다. 초 단위
정확도는 STT 텍스트의 관용구로 잡는다.
"""
from __future__ import annotations

import re

from .config import Config

# 설교 시작 후보 — 본문 인용으로 넘어가는 지점
SERMON_START_PATTERNS = [
    r"오늘\s*(의)?\s*본문",
    r"본문\s*말씀",
    r"본문은",
    r"말씀을\s*(함께\s*)?봉독",
    r"\d+\s*장\s*\d+\s*절",
    r"아멘\s*으로\s*받",
]

# 설교 종료 후보 — 마무리 기도로 넘어가는 지점
SERMON_END_PATTERNS = [
    r"기도(하|하시)겠습니다",
    r"함께\s*기도",
    r"기도로\s*마치",
    r"축원합니다",
]

# 축도 시작 후보 — 고린도후서 13:13 정형구
BENE_START_PATTERNS = [
    r"축도",
    r"축복\s*기도",
    r"주\s*예수\s*그리스도의\s*은혜",
    r"성부와\s*성자와\s*성령",
    r"하나님의\s*사랑과",
    r"성령의\s*교통하심",
    r"복\s*주시기를",
]

BENE_END_PATTERNS = [
    r"아멘",
]


def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p) for p in patterns]


_S_START = _compile(SERMON_START_PATTERNS)
_S_END = _compile(SERMON_END_PATTERNS)
_B_START = _compile(BENE_START_PATTERNS)
_B_END = _compile(BENE_END_PATTERNS)


def _matches(text: str, pats: list[re.Pattern]) -> bool:
    return any(p.search(text) for p in pats)


def _window(segments: list[dict], center: float, span: float) -> list[dict]:
    return [s for s in segments if center - span <= s["start"] <= center + span]


def refine_start(segments: list[dict], boundary: float, pats: list[re.Pattern],
                 cfg: Config) -> tuple[float, str | None]:
    """boundary 근처에서 패턴에 걸리는 첫 문장의 시작으로 당긴다."""
    for seg in sorted(_window(segments, boundary, cfg.refine_window), key=lambda s: s["start"]):
        if _matches(seg["text"], pats):
            return max(0.0, seg["start"] - cfg.refine_pad), seg["text"]
    return boundary, None


def refine_end(segments: list[dict], boundary: float, pats: list[re.Pattern],
               cfg: Config) -> tuple[float, str | None]:
    """boundary 근처에서 패턴에 걸리는 마지막 문장의 끝으로 맞춘다."""
    hits = [s for s in _window(segments, boundary, cfg.refine_window) if _matches(s["text"], pats)]
    if not hits:
        return boundary, None
    last = max(hits, key=lambda s: s["end"])
    return last["end"] + cfg.refine_pad, last["text"]


def refine_sermon(segments: list[dict], start: float, end: float,
                  cfg: Config) -> tuple[float, float, list[str]]:
    notes: list[str] = []
    new_start, hit = refine_start(segments, start, _S_START, cfg)
    if hit:
        notes.append(f"설교 시작 보정 {start:.1f}s → {new_start:.1f}s ('{hit[:24]}')")
    new_end, hit = refine_end(segments, end, _S_END, cfg)
    if hit:
        notes.append(f"설교 끝 보정 {end:.1f}s → {new_end:.1f}s ('{hit[:24]}')")
    if new_end <= new_start:
        return start, end, ["설교 보정 결과가 뒤집혀 원본 유지"]
    return new_start, new_end, notes


def refine_benediction(segments: list[dict], start: float, end: float,
                       cfg: Config) -> tuple[float, float, list[str]]:
    notes: list[str] = []
    new_start, hit = refine_start(segments, start, _B_START, cfg)
    if hit:
        notes.append(f"축도 시작 보정 {start:.1f}s → {new_start:.1f}s ('{hit[:24]}')")
    new_end, hit = refine_end(segments, end, _B_END, cfg)
    if hit:
        notes.append(f"축도 끝 보정 {end:.1f}s → {new_end:.1f}s ('{hit[:24]}')")
    if new_end <= new_start:
        return start, end, ["축도 보정 결과가 뒤집혀 원본 유지"]
    return new_start, new_end, notes
