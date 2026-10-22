"""조각마다 말소리 크기를 재어, 붙이기 전에 같게 맞춘다.

왜 필요한가 (2026-10-20):
    요약은 설교 여기저기에서 문장을 뽑아 붙인다. 그런데 녹음 자체가 구간마다
    음량이 다를 수 있다. 실측(아브라함 설교 pKl_sDTuScI): 15분 지점의 말소리는
    -13dB, 30분 지점은 -40dB 로 **26dB** 차이가 났다. 음향 콘솔에서 누가
    페이더를 올렸다 내렸거나, 중계 장비가 자동으로 음량을 조절한 자국이다.

    이런 조각들을 그냥 붙이면, 뒤에 거는 음량 맞추기(loudnorm)가 조각마다
    따라잡느라 소리가 줄었다 늘었다 한다. 낱말이 들렸다 안 들렸다 하는 것이
    이것이다 (2026-10-20 사용자 지적).

어떻게 하나:
    조각마다 말소리 크기를 재어, **한 번에 정해지는 이득**(volume 필터)으로
    같은 크기에 맞춘 뒤 붙인다. 조각 안에서 크기를 움직이지 않으므로, 목사님이
    한 문장 안에서 힘을 주었다 뺐다 하는 결은 그대로 남는다. 조각끼리의 턱만
    없어진다.

    너무 작은 조각을 무리하게 끌어올리면 잡음까지 커지므로, 올리고 내리는 폭에
    한계를 둔다.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

FRAME = 0.02                 # 20밀리초
SPEECH_BELOW_PEAK = 20.0     # 봉우리에서 이만큼 아래까지를 말소리로 본다
# 올리고 내리는 한계(dB). 실측에서 한 설교 안에 29dB 차이가 있었으므로
# 그 정도는 맞출 수 있어야 한다. 더 키우면 조용한 조각의 잡음까지 커진다.
MAX_LIFT = 15.0
MAX_DROP = 15.0


def _read(path: Path, start: float, end: float) -> tuple[np.ndarray, int]:
    """그 구간만 읽어 모노 float32 로. 파일 전체를 메모리에 올리지 않는다."""
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        if wf.getsampwidth() != 2:
            return np.zeros(0, np.float32), sr
        a = max(0, int(start * sr))
        b = min(wf.getnframes(), int(end * sr))
        if b <= a:
            return np.zeros(0, np.float32), sr
        wf.setpos(a)
        raw = wf.readframes(b - a)
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def speech_level(x: np.ndarray, sr: int) -> float | None:
    """그 소리의 말소리 크기(dB). 말이 없으면 None.

    쉼까지 같이 평균 내면 쉼이 긴 조각이 작게 나온다. 그래서 봉우리에서
    일정 폭 안에 드는 칸만 모아 평균을 낸다.
    """
    if len(x) < sr * 0.1:
        return None
    hop = max(1, int(sr * FRAME))
    n = len(x) // hop
    if n < 3:
        return None
    e = np.sqrt((x[:n * hop] ** 2).reshape(n, hop).mean(axis=1))
    db = 20 * np.log10(np.maximum(e, 1e-7))
    peak = np.percentile(db, 95)
    loud = db[db > peak - SPEECH_BELOW_PEAK]
    if len(loud) < 2:
        return None
    return float(loud.mean())


def _split_inside(x: np.ndarray, sr: int, min_pause: float = 0.35) -> list[float]:
    """조각 안에서 말 덩어리가 갈리는 자리(초). 쉼 한가운데서 가른다.

    여기서 이득을 바꿔야 턱이 안 들린다. 말하는 도중에 바꾸면 그게 들린다.
    """
    hop = max(1, int(sr * FRAME))
    n = len(x) // hop
    if n < 10:
        return []
    db = 20 * np.log10(np.maximum(
        np.sqrt((x[:n * hop] ** 2).reshape(n, hop).mean(axis=1)), 1e-7))
    thr = np.percentile(db, 95) - 25.0
    quiet = db < thr
    cuts: list[float] = []
    i = 0
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            if (j - i) * FRAME >= min_pause:
                cuts.append((i + j) / 2 * FRAME)
            i = j
        else:
            i += 1
    span = n * FRAME
    return [c for c in cuts if 0.3 < c < span - 0.3]


def plan_gains(wav: Path | None, runs: list[tuple[float, float]],
               strength: float = 0.7, say=None) -> list[str]:
    """조각마다 걸 volume 식. 할 일이 없으면 빈 글.

    조각 **안에서도** 문장마다 말소리 크기를 재어, 조용한 문장을 끌어올린다.
    실측(2026-10-20 '내부의 무너짐' 설교): 한 조각(34초) 안에서 문장끼리
    9.3dB 차이가 났다. "우리 하나님을 경외하는…" 이 -32.2dB, 11초 뒤
    "저도 우리 성도님들도…" 가 -41.6dB 였다. 듣는 분은 뒤 문장에서 소리가
    내려간다고 느낀다.

    **끝까지 맞추지는 않는다.** 전부 같은 크기로 펴면 목사님이 힘주어 말한
    대목과 낮춰 말한 대목의 차이가 사라진다. 차이의 7할만 좁힌다 — 실측에서
    5.7dB → 3.0dB 가 되었고, 10할로 하면 되레 3.6dB 로 어긋났다.

    이득은 쉼 한가운데서 바뀌므로 바뀌는 순간이 들리지 않는다.
    """
    tell = say or (lambda t: None)
    n = len(runs)
    if not wav or not runs:
        return [""] * n
    wav = Path(wav)
    if not wav.exists():
        return [""] * n

    # 1) 조각마다 쉼에서 갈라, 작은 덩어리들의 말소리 크기를 잰다
    per_run: list[list[tuple[float, float, float | None]]] = []
    for s, e in runs:
        try:
            x, sr = _read(wav, s, e)
        except (OSError, wave.Error):
            per_run.append([])
            continue
        if len(x) == 0:
            per_run.append([])
            continue
        bounds = [0.0] + _split_inside(x, sr) + [(e - s)]
        rows = []
        for k in range(len(bounds) - 1):
            a, b = bounds[k], bounds[k + 1]
            rows.append((a, b, speech_level(x[int(a * sr):int(b * sr)], sr)))
        per_run.append(rows)

    known = [v for rows in per_run for _a, _b, v in rows if v is not None]
    if len(known) < 2:
        return [""] * n
    ref = float(np.median(known))
    spread = max(known) - min(known)
    if spread < 3.0:
        return [""] * n

    # 2) 작은 덩어리마다 이득을 정해 volume 식으로 묶는다
    out, moved = [], 0
    for rows in per_run:
        terms = []
        for a, b, v in rows:
            g = 0.0 if v is None else (ref - v) * strength
            g = min(MAX_LIFT, max(-MAX_DROP, g))
            if abs(g) >= 0.5:
                moved += 1
            terms.append((b, 10 ** (g / 20.0)))
        if not terms or all(abs(20 * np.log10(t[1])) < 0.5 for t in terms):
            out.append("")
            continue
        ex = f"{terms[-1][1]:.3f}"
        for b, lin in reversed(terms[:-1]):
            ex = f"if(lt(t,{b:.2f}),{lin:.3f},{ex})"
        out.append(ex)

    tell(f"문장끼리 말소리 크기가 {spread:.0f}dB 차이 나서 {moved}곳을 고르게 폈습니다")
    return out


def volume_filter(expr: str) -> str:
    """ffmpeg 필터 한 토막. 할 일이 없으면 빈 글."""
    if not expr:
        return ""
    return f"volume=eval=frame:volume='{expr}'"
