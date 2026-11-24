"""배경 음악을 말소리 아래로 낮춘다 — 자동 덕킹 (2026-11-02).

FilmCraft(MIT/Apache-2.0, ArtCraft 팀)의 Essential Sound 자동 덕킹을 읽고, 같은 방법과
같은 기본값으로 파이썬에 다시 썼다. 원본은 Rust 라 이 프로그램에 그대로 붙일 수 없다.
    crates/audio-dsp/src/ducking.rs        envelope_db · activity · duck_keyframes
    crates/engine/src/essential_sound.rs   10ms 간격 · 50ms 창 · 0.1초 · 0.25초
    crates/project/src/essential.rs        감도 6 · 15dB · 0.8초 (Ducking::default)

어떻게 하나:
    1) 말소리 크기 곡선. 10ms 마다, 그 자리를 가운데로 한 50ms 의 RMS(dBFS).
    2) 말하는 구간. 문턱(감도 6 → -44 dBFS)을 넘는 곳. 0.25초보다 짧은 틈은 잇고,
       0.1초보다 짧은 소리(기침·마이크 잡음)는 버린다.
    3) 음악 크기의 열쇠 지점(keyframe). 말하는 구간마다 0.8초 **앞에서부터** 15dB 를
       내려 두었다가, 말이 끝나면 0.8초에 걸쳐 되돌린다. 앞 구간의 오르막과 뒤 구간의
       내리막이 겹치면(틈이 1.6초보다 짧으면) 내려 둔 채로 잇는다. 그래서 문장과 문장
       사이마다 음악이 들썩이지 않고, 목사님이 한참 쉬실 때만 살짝 올라온다.

열쇠 지점 사이는 dB 로 곧게 잇는다 (FilmCraft 의 볼륨 열쇠 지점과 같다). ffmpeg 에
넘길 때는 1000Hz 짜리 '이득 소리' 파일로 만들어 음악에 곱한다 (core/visual.py).

전부 순수 함수다. 시각은 초, 크기는 dBFS / dB.
"""
from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

# 조용함으로 칠 바닥 (dBFS)
FLOOR_DB = -120.0

# FilmCraft 기본값 (crates/engine/src/essential_sound.rs, crates/project/src/essential.rs)
HOP_S = 0.01            # 10ms 마다
WINDOW_HOPS = 5         # 그 자리를 가운데로 한 50ms 창
MIN_ON_S = 0.1          # 이보다 짧은 소리는 말이 아니다
BRIDGE_S = 0.25         # 이보다 짧은 틈은 잇는다
SENSITIVITY = 6.0       # 0~10. 클수록 작은 말소리에도 반응한다
REDUCE_DB = 15.0        # 말할 때 줄이는 양
FADE_S = 0.8            # 줄이고 되돌리는 데 걸리는 시간


def sensitivity_threshold_db(sensitivity: float) -> float:
    """감도(0~10) → 말소리로 칠 문턱(dBFS). -20 - 4·s (5 → -40, 6 → -44)."""
    s = min(max(float(sensitivity), 0.0), 10.0)
    return -20.0 - 4.0 * s


def envelope_db(x: np.ndarray, sample_rate: float, hop_s: float = HOP_S,
                window_s: float = HOP_S * WINDOW_HOPS) -> np.ndarray:
    """k 번째 값 = k·hop 을 가운데로 한 window_s 초의 RMS (dBFS). 길이는 ceil(len/hop).

    x 는 (샘플,) 또는 (샘플, 채널). 채널은 전력을 평균한다. 조용하면 FLOOR_DB.
    """
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 2:
        power = np.mean(x * x, axis=1)
    else:
        power = x * x
    n = len(power)
    if n == 0:
        return np.zeros(0, np.float32)
    hop = max(1, int(round(hop_s * sample_rate)))
    half = max(1, int(round(window_s * sample_rate / 2.0)))
    pre = np.concatenate([[0.0], np.cumsum(power)])
    centres = np.arange(0, n, hop)
    a = np.clip(centres - half, 0, n)
    b = np.clip(centres + half, 0, n)
    ms = (pre[b] - pre[a]) / np.maximum(b - a, 1)
    ms = np.maximum(ms, 0.0)
    out = np.full(len(centres), FLOOR_DB, np.float64)
    loud = ms > 1e-12
    out[loud] = np.maximum(10.0 * np.log10(ms[loud]), FLOOR_DB)
    return out.astype(np.float32)


def envelope_from_wav(path: Path, hop_s: float = HOP_S,
                      window_s: float = HOP_S * WINDOW_HOPS,
                      block_s: float = 30.0) -> np.ndarray:
    """wav 파일을 조금씩 읽으며 크기 곡선을 만든다. 40분짜리도 메모리를 거의 안 쓴다.

    envelope_db 와 **똑같은** 값을 낸다. 작은 칸마다 전력의 합을 먼저 구해 두고, 창은
    그 칸들을 이어 붙여 잰다 — 파일 전체를 메모리에 올리지 않는다.

    칸 크기는 hop 과 창 반쪽의 최대공약수다 (48kHz 면 480 과 1200 → 240 샘플).
    그래야 창의 양 끝이 늘 칸 경계에 떨어져 어림 없이 정확하다. 10ms 칸으로 하면
    창 끝이 칸 가운데에 걸려, 말이 시작되는 자리에서 0.4dB 쯤 어긋났다.
    """
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        width = wf.getsampwidth()
        total = wf.getnframes()
        if width != 2:
            raise ValueError(f"16비트 wav 만 읽습니다 (지금 {width * 8}비트)")
        hop = max(1, int(round(hop_s * sr)))
        half = max(1, int(round(window_s * sr / 2.0)))
        size = math.gcd(hop, half)
        # 칸마다 전력 합. 마지막 칸은 짧을 수 있다.
        sums: list[np.ndarray] = []
        carry = np.zeros(0, np.float64)
        per_block = max(size, int(block_s * sr) // size * size)
        left = total
        while left > 0:
            take = min(per_block, left)
            raw = wf.readframes(take)
            if not raw:
                break
            left -= take
            x = np.frombuffer(raw, "<i2").astype(np.float64) / 32768.0
            if ch > 1:
                x = x[: len(x) // ch * ch].reshape(-1, ch)
                p = np.mean(x * x, axis=1)
            else:
                p = x * x
            p = np.concatenate([carry, p])
            whole = len(p) // size * size
            if whole:
                sums.append(p[:whole].reshape(-1, size).sum(axis=1))
            carry = p[whole:]
        if len(carry):
            sums.append(np.array([carry.sum()]))
    if not sums:
        return np.zeros(0, np.float32)
    cell = np.concatenate(sums)                      # 칸 j = [j·size, (j+1)·size)
    pre = np.concatenate([[0.0], np.cumsum(cell)])   # 칸 단위 누적
    n = total
    centres = np.arange(0, n, hop)
    a = np.clip(centres - half, 0, n)
    b = np.clip(centres + half, 0, n)

    def cum(at: np.ndarray) -> np.ndarray:
        # 샘플 at 까지의 누적 전력. at 은 칸 경계이거나 파일 끝(n)이다.
        return pre[np.where(at >= n, len(cell), at // size)]

    ms = (cum(b) - cum(a)) / np.maximum(b - a, 1)
    out = np.full(len(centres), FLOOR_DB, np.float64)
    loud = ms > 1e-12
    out[loud] = np.maximum(10.0 * np.log10(ms[loud]), FLOOR_DB)
    return out.astype(np.float32)


def activity(env_db: np.ndarray, hop_s: float, threshold_db: float,
             min_on_s: float = MIN_ON_S, bridge_s: float = BRIDGE_S) -> list[tuple[float, float]]:
    """크기 곡선이 문턱을 넘는 구간 (시작, 끝) 초.

    bridge_s 보다 짧은 틈을 먼저 잇고, 그다음 min_on_s 보다 짧은 구간을 버린다.
    칸 k 는 [k·hop, (k+1)·hop) 을 덮는다.
    """
    hop = round(float(hop_s) * 1e6) / 1e6       # 0.01 이 정확히 0.01 초가 되게
    raw: list[tuple[float, float]] = []
    start: int | None = None
    for k, v in enumerate(np.asarray(env_db)):
        on = v > threshold_db
        if on and start is None:
            start = k
        elif not on and start is not None:
            raw.append((start * hop, k * hop))
            start = None
    if start is not None:
        raw.append((start * hop, len(env_db) * hop))

    merged: list[list[float]] = []
    for a, b in raw:
        if merged and a - merged[-1][1] < bridge_s - 1e-9:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_on_s - 1e-9]


def duck_keyframes(regions: list[tuple[float, float]], clip_start_s: float,
                   clip_end_s: float, base_db: float, reduce_db: float,
                   fade_s: float) -> list[tuple[float, float]]:
    """음악 크기 열쇠 지점 (시각 초, 크기 dB).

    구간마다 a - fade 에서 base_db 로 시작해 a 에서 base_db - |reduce_db| 까지 내리고,
    b 까지 그대로 두었다가 b + fade 에서 base_db 로 되돌린다. 오르막과 내리막이 닿거나
    겹치는 구간끼리는 하나로 묶는다(그 사이는 내려 둔 채). 전부 [clip_start_s,
    clip_end_s] 안으로 자르고, 가장자리에 잘린 경사는 그 자리의 값으로 열쇠 지점을
    둔다. 시각은 늘 증가한다. 구간이 없으면 열쇠 지점도 없다.
    """
    fade = max(float(fade_s), 1e-3)
    ducked = base_db - abs(reduce_db)
    rs = sorted(((a, b) for a, b in regions
                 if b > a and b > clip_start_s and a < clip_end_s), key=lambda r: r[0])
    groups: list[list[float]] = []
    for a, b in rs:
        if groups and a - fade <= groups[-1][1] + fade:
            groups[-1][1] = max(groups[-1][1], b)
        else:
            groups.append([a, b])

    out: list[list[float]] = []

    def push(t: float, v: float) -> None:
        if t < clip_start_s or t > clip_end_s:
            return
        if out and t <= out[-1][0] + 1e-9:
            out[-1][1] = v
        else:
            out.append([t, v])

    def down(a: float, t: float) -> float:
        return base_db + (ducked - base_db) * min(max((t - (a - fade)) / fade, 0.0), 1.0)

    def up(b: float, t: float) -> float:
        return ducked + (base_db - ducked) * min(max((t - b) / fade, 0.0), 1.0)

    for a, b in groups:
        ramp_start = a - fade
        if ramp_start >= clip_start_s:
            push(ramp_start, base_db)
        else:
            push(clip_start_s, down(a, clip_start_s))
        push(max(a, clip_start_s), ducked)
        if b < clip_end_s:
            push(b, ducked)
            if b + fade <= clip_end_s:
                push(b + fade, base_db)
            else:
                push(clip_end_s, up(b, clip_end_s))
        else:
            push(clip_end_s, ducked)
    return [(t, v) for t, v in out]


def gain_curve(keyframes: list[tuple[float, float]], duration_s: float,
               base_db: float, rate: int = 1000) -> np.ndarray:
    """열쇠 지점을 rate Hz 로 펴 놓은 곱셈 이득(0~1). 열쇠 지점 사이는 dB 로 곧게 잇는다.

    첫 열쇠 지점 앞과 마지막 뒤는 그 값을 그대로 이어 간다. 열쇠 지점이 없으면 base_db.
    """
    n = max(1, int(math.ceil(max(duration_s, 0.0) * rate)) + 1)
    t = np.arange(n, dtype=np.float64) / rate
    if keyframes:
        kt = np.array([k[0] for k in keyframes], np.float64)
        kv = np.array([k[1] for k in keyframes], np.float64)
        db = np.interp(t, kt, kv)
    else:
        db = np.full(n, float(base_db))
    return np.power(10.0, db / 20.0).astype(np.float32)


def write_gain_wav(path: Path, gains: np.ndarray, rate: int = 1000) -> Path:
    """이득 곡선을 모노 16비트 wav 로. ffmpeg 가 음악과 같은 빠르기로 늘려 곱한다."""
    g = np.clip(np.asarray(gains, np.float64), 0.0, 1.0)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(rate))
        wf.writeframes(np.round(g * 32767.0).astype("<i2").tobytes())
    return Path(path)


def plan(speech_wav: Path, duration_s: float, base_db: float,
         reduce_db: float = REDUCE_DB, fade_s: float = FADE_S,
         sensitivity: float = SENSITIVITY) -> tuple[list[tuple[float, float]],
                                                     list[tuple[float, float]]]:
    """말소리 wav 에서 (말하는 구간, 음악 열쇠 지점) 을 만든다. FilmCraft 기본값 그대로."""
    env = envelope_from_wav(Path(speech_wav), HOP_S, HOP_S * WINDOW_HOPS)
    regions = activity(env, HOP_S, sensitivity_threshold_db(sensitivity), MIN_ON_S, BRIDGE_S)
    keys = duck_keyframes(regions, 0.0, float(duration_s), base_db, reduce_db, fade_s)
    return regions, keys
