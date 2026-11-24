"""세로 요약 영상(숏폼 — 릴스·쇼츠)의 모양 (2026-11-02).

다른 교회 릴스 두 개를 보고 정했다 (사용자가 보여 준 것):
    우리가교회       목사님이 화면을 꽉 채우고, 위에 노란 둥근 상자 + 검은 굵은 글씨의
                     끌리는 제목. 자막은 짧게 끊어 가운데 아래에 반투명 검은 바탕으로.
    달라스하나로교회 꽉 찬 화면, 자막은 위쪽에 짧게. 아래에 검은 띠 + 왼쪽 노란 세로줄 +
                     흰 굵은 세 줄 제목.
그리고 교회 안에서 나온 의견: 검정 띠 대신 짙은 곤색, 아래 가운데에 창의진 로고,
목사님 제목보다 쉬운 질문형 제목 (예: "우리는 무엇을 믿고 도전하나").

나누는 일:
    여기(core)     모양 값(Look)·틀(PRESETS)·자막 짧게 끊기·AI 제목 추천·ffmpeg 자막 목록
    ui_reels.py    같은 Look 으로 Qt 가 그림을 그린다 — 미리 보기와 완성본의 글자·띠·
                   로고 그림이 **같은 함수**에서 나온다. 완성본에는 그 그림(PNG)을 얹는다.
    shorts.build   look 을 받으면 화면 자리·크롭을 거기에 맞추고 그림을 얹는다.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

W, H = 1080, 1920
BANDS_TOP, BANDS_VIDEO_H = 330, 1140          # 예전 모양: 위 제목 띠 · 영상 · 아래 자막 띠

NAVY = "#0C1631"


@dataclass
class Look:
    preset: str = "navy"
    layout: str = "bands"         # bands: 위아래 띠 사이에 영상 | full: 영상이 화면을 꽉 채움
    bg: str = NAVY                # 띠 색
    hook_style: str = "plain"     # plain: 글자만 | box: 둥근 상자 | bar: 띠 + 왼쪽 세로줄
    hook_pos: str = "top"         # top | bottom
    hook_fg: str = "#FFFFFF"
    hook_bg: str = "#FFD43B"      # box 의 상자 색 · bar 의 세로줄 색
    hook_size: int = 76
    show_verse: bool = True
    sub_y: float = 0.80           # 자막 가운데 높이 (화면 높이에 대한 비율)
    sub_size: int = 54
    sub_chars: int = 14           # 자막 한 번에 몇 글자까지 — 넘으면 끊는다
    sub_box: bool = False         # 자막 뒤 반투명 검은 상자
    sub_color: str = "#FFFFFF"
    logo: str = ""                # 로고 그림 파일 (png 권장 — 바탕이 투명하면 좋다)
    logo_pos: str = "bottom"      # bottom | top_right | top_left | none
    logo_w: int = 200
    cx: float = 0.44              # 목사님 자리 (원본 가로·세로 비율)
    cy: float = 0.61
    zoom: float = 0.80            # 원본 높이의 몇 배를 담을지 — 작을수록 크게
    # 원본의 어디를 쓸지 (2026-11-02): crop = 가로 설교 영상에서 목사님만 잘라 크게 |
    # whole = 세로 영상 화면 그대로 | middle = 예전 요약 영상(위아래 띠)의 가운데 영상만.
    # whole·middle 의 자리·크기는 vcx·vcy·vzoom 에 따로 둔다 (가로 영상의 값을 지키려고).
    source: str = "crop"
    vcx: float = 0.5
    vcy: float = 0.5
    vzoom: float = 1.0
    extra: dict = field(default_factory=dict)

    def area(self) -> tuple[int, int]:
        """영상이 놓이는 (위 y, 높이)."""
        return (0, H) if self.layout == "full" else (BANDS_TOP, BANDS_VIDEO_H)

    def region(self, src_w: int, src_h: int) -> tuple[int, int, int, int] | None:
        """원본에서 쓸 부분 (x, y, 폭, 높이). crop 이면 None (목사님 자리 식을 쓴다)."""
        if self.source == "middle":
            y = int(round(src_h * BANDS_TOP / H))
            return (0, y, src_w, int(round(src_h * BANDS_VIDEO_H / H)))
        if self.source == "whole":
            return (0, 0, src_w, src_h)
        return None

    def crop(self, src_w: int, src_h: int) -> tuple[int, int, int, int]:
        """원본에서 잘라 쓸 (폭, 높이, x, y) — 미리 보기와 만들기가 같이 쓴다."""
        from . import shorts
        _y, ah = self.area()
        reg = self.region(src_w, src_h)
        if reg is None:
            return shorts.crop_rect(src_w, src_h, self.cx, self.cy, self.zoom, ah)
        return shorts.region_crop(src_w, src_h, reg, self.vcx, self.vcy, self.vzoom, ah)

    def build_args(self, src_w: int, src_h: int) -> dict:
        """shorts.build 의 look 에 넣을 화면 자리 값."""
        reg = self.region(src_w, src_h)
        if reg is None:
            return {"area": self.area(), "cx": self.cx, "cy": self.cy, "zoom": self.zoom}
        return {"area": self.area(), "region": reg, "cx": self.vcx, "cy": self.vcy,
                "zoom": self.vzoom}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Look":
        if not isinstance(data, dict):
            return cls()
        known = set(cls.__dataclass_fields__)            # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


# 고를 수 있는 틀. 고른 뒤 무엇이든 바꿀 수 있다 (로고·목사님 자리는 틀을 바꿔도 그대로).
PRESETS: dict[str, tuple[str, dict]] = {
    "navy": ("창의진 기본 — 곤색 띠 · 아래 로고", dict(
        layout="bands", bg=NAVY, hook_style="plain", hook_pos="top", hook_fg="#FFFFFF",
        hook_bg="#FFD43B", hook_size=76, show_verse=True, sub_y=0.80, sub_size=54,
        sub_chars=16, sub_box=False, sub_color="#FFFFFF", logo_pos="bottom")),
    "yellow_box": ("노란 상자 제목 — 꽉 찬 화면", dict(
        layout="full", bg="#000000", hook_style="box", hook_pos="top", hook_fg="#111111",
        hook_bg="#FFD43B", hook_size=70, show_verse=False, sub_y=0.70, sub_size=50,
        sub_chars=14, sub_box=True, sub_color="#FFFFFF", logo_pos="none")),
    "bottom_bar": ("아래 제목 띠 — 노란 세로줄", dict(
        layout="full", bg="#000000", hook_style="bar", hook_pos="bottom", hook_fg="#FFFFFF",
        hook_bg="#FFD43B", hook_size=92, show_verse=False, sub_y=0.17, sub_size=50,
        sub_chars=14, sub_box=True, sub_color="#FFFFFF", logo_pos="none")),
    "navy_full": ("곤색 제목 상자 — 꽉 찬 화면 · 아래 로고", dict(
        layout="full", bg=NAVY, hook_style="box", hook_pos="top", hook_fg="#FFFFFF",
        hook_bg=NAVY, hook_size=70, show_verse=True, sub_y=0.66, sub_size=50,
        sub_chars=14, sub_box=True, sub_color="#FFFFFF", logo_pos="bottom")),
}


def apply_preset(look: Look, key: str) -> Look:
    """틀을 고른다. 로고 파일·목사님 자리처럼 틀과 상관없는 것은 그대로 둔다."""
    _label, values = PRESETS.get(key, PRESETS["navy"])
    data = look.to_dict()
    data.update(values)
    data["preset"] = key
    return Look.from_dict(data)


# ------------------------------------------------------------------ 자막 짧게 끊기
_SRT_TIME = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def _sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000.0


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    out = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        rows = [r for r in block.splitlines() if r.strip()]
        for i, r in enumerate(rows[:2]):
            hit = _SRT_TIME.search(r)
            if hit:
                body = " ".join(x.strip() for x in rows[i + 1:])
                if body:
                    out.append((_sec(*hit.group(1, 2, 3, 4)), _sec(*hit.group(5, 6, 7, 8)),
                                body))
                break
    return out


def _split_words(text: str, limit: int) -> list[str]:
    """띄어쓰기 단위로 limit 글자 안쪽 덩어리로. 덩어리 길이가 고르게 되도록 나눈다."""
    words = text.split()
    if not words:
        return []
    total = len(text.replace(" ", ""))
    n = max(1, -(-total // max(4, limit)))          # 몇 덩어리로 나눌지
    target = total / n
    chunks: list[list[str]] = [[]]
    size = 0
    for w in words:
        wl = len(w)
        if chunks[-1] and (size + wl > limit or (size >= target * 0.85 and len(chunks) < n)):
            chunks.append([])
            size = 0
        chunks[-1].append(w)
        size += wl
    return [" ".join(c) for c in chunks if c]


def chunk_captions(cues: list[tuple[float, float, str]], limit: int = 14,
                   min_len: float = 0.6) -> list[tuple[float, float, str]]:
    """자막 한 줄을 릴스처럼 짧게 끊는다. 시간은 글자 수에 비례해 나눈다."""
    out: list[tuple[float, float, str]] = []
    for a, b, text in cues:
        parts = _split_words(text, limit)
        if not parts:
            continue
        weights = [max(1, len(p.replace(" ", ""))) for p in parts]
        span = max(0.01, b - a)
        if span / len(parts) < min_len:            # 너무 빨리 넘어가면 덜 끊는다
            keep = max(1, int(span / min_len))
            parts = _split_words(text, max(limit, -(-len(text) // keep)))
            weights = [max(1, len(p.replace(" ", ""))) for p in parts]
        tot = sum(weights)
        t = a
        for p, wgt in zip(parts, weights):
            d = span * wgt / tot
            out.append((t, t + d, p))
            t += d
    return out


def caption_at(chunks: list[tuple[float, float, str]], t: float) -> str:
    for a, b, text in chunks:
        if a <= t < b:
            return text
    return ""


def ffconcat(entries: list[tuple[str, float]], blank: str, path: Path) -> Path:
    """그림 차례 목록 (ffmpeg concat). entries 는 (그림 파일 이름, 보여 줄 초)."""
    lines = ["ffconcat version 1.0"]
    last = blank
    for name, dur in entries:
        if dur <= 0.0005:
            continue
        lines.append(f"file '{name}'")
        lines.append(f"duration {dur:.4f}")
        last = name
    lines.append(f"file '{last}'")             # 마지막 길이가 먹게 한 번 더 적는다
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def caption_track(chunks: list[tuple[float, float, str | None]], t0: float, t1: float
                  ) -> list[tuple[str | None, float]]:
    """[t0, t1) 구간의 자막 차례 — (그림 이름 또는 None=빈 그림, 길이)."""
    out: list[tuple[str | None, float]] = []
    t = t0
    for a, b, name in chunks:
        if b <= t0 or a >= t1:
            continue
        a, b = max(a, t0), min(b, t1)
        if a > t + 1e-4:
            out.append((None, a - t))
        out.append((name, b - a))
        t = b
    if t1 > t + 1e-4:
        out.append((None, t1 - t))
    return out


# ------------------------------------------------------------------ AI 제목 추천
HOOK_SYSTEM = (
    "당신은 교회 설교로 만든 짧은 세로 영상(릴스)의 제목을 짓는 편집자입니다. "
    "교회를 잘 모르는 사람도 눌러 보고 싶게, 쉬운 우리말로 짧게 짓습니다. "
    "제목만 한 줄에 하나씩 쓰고 다른 말은 쓰지 않습니다."
)

HOOK_PROMPT = """설교 제목: {title}
본문: {verse}

이 영상에 들어가는 목사님 말씀:
{text}

이 영상 화면 맨 위(또는 아래)에 크게 얹을 제목을 {n}개 지어 주세요.
- 절반은 질문으로 (예: 우리는 무엇을 믿고 도전하나)
- 어려운 성경 용어·인명 대신 일상의 쉬운 말로
- 듣는 사람 자신의 이야기처럼 (우리, 나, 당신)
- 20자 안팎, 한 줄에 하나, 번호·따옴표·설명 없이"""


def clean_hooks(text: str, n: int = 5) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        s = re.sub(r"^\s*(?:[-*•·]|\d+[.)]|[①-⑩])\s*", "", s)
        s = s.strip(" \"'“”‘’「」『』")
        s = re.sub(r"\s+", " ", s)
        if not (4 <= len(s) <= 40):
            continue
        if re.fullmatch(r"[\d\s,\[\]]+", s) or s.startswith(("제목", "설교", "본문", "예)")):
            continue
        if s not in out:
            out.append(s)
    return out[:n]


def suggest_hooks(cfg, title: str, verse: str, text: str, n: int = 5,
                  say=print) -> list[str]:
    """이 교회 PC 의 AI 모델에게 끌리는 제목을 묻는다. 모델 올리는 데 시간이 걸린다."""
    from . import llm
    from .util import ToolError

    if not llm.find_server():
        raise ToolError(llm.why_not(cfg))
    # 학습한 모델은 '번호 고르기' 를 배운 것이라, 글을 짓는 데는 기본 모델이 낫다.
    base = llm.find_in_dirs(llm.model_dirs(cfg), cfg.llm_model_file) \
        if hasattr(llm, "find_in_dirs") else None
    model = base if base else llm.model_path(cfg)
    if not Path(model).exists():
        raise ToolError(llm.why_not(cfg))
    server = llm.start(cfg, progress=say, model=Path(model))
    try:
        body = text.strip()
        if len(body) > 2400:
            body = body[:2400] + " …"
        reply = llm.ask(server, HOOK_SYSTEM,
                        HOOK_PROMPT.format(title=title or "(없음)", verse=verse or "(없음)",
                                           text=body, n=n),
                        max_tokens=300, temperature=0.7, progress=False)
    finally:
        server.close()
    hooks = clean_hooks(reply, n)
    if not hooks:
        raise ToolError("AI 가 제목을 내놓지 못했습니다. 한 번 더 눌러 보거나 직접 적어 주세요.")
    return hooks
