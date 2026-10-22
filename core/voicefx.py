"""말소리 다듬기 — 예배당 울림 줄이기 (spectral de-reverberation).

FilmCraft(MIT/Apache-2.0, ArtCraft 팀)의 `audio-dsp/effects/essential.rs` DeReverb 를
읽고 그 방법을 numpy 로 다시 쓴 것이다. 코드를 베낀 것이 아니라 같은 교과서 모델을
같은 매개변수로 구현했다 — 원본은 Rust 라 파이썬 프로그램에 그대로 붙일 수 없다.

방법 (Lebart et al. 2001, Habets 2007 — 늦은 울림의 통계 모델):
    예배당처럼 울리는 공간에서 소리는 말한 뒤에도 지수적으로 꺼지며 남는다(RT60 = 60dB
    줄어드는 데 걸리는 시간, 예배당은 보통 0.8~1.5초). 지금 들리는 소리의 일부는 T_d 전에
    난 소리의 꼬리다. 그 꼬리의 세기는
        λ_r(t) = e^{−2Δ·T_d} · λ_x(t − T_d),   Δ = 3·ln10 / RT60
    로 예측할 수 있으므로, 주파수마다 그만큼을 빼 준다(spectral subtraction). 과하게 빼면
    말이 금속성으로 변하므로 over-subtraction 과 gain floor 로 세기를 조절한다.

왜 ffmpeg 로 하지 않는가: ffmpeg 에는 울림 제거 필터가 없다. 잡음 제거(afftdn)는
**일정한** 배경 소음만 걷어 낸다. 울림은 말소리 자체의 꼬리라 다른 방법이 필요하다.

CPU 만 쓰고 numpy 만 필요하다. 36분 설교(48kHz) 기준 수십 초.
"""
from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

TD = 0.05            # 늦은 울림으로 치는 지연 (초) — 이보다 늦게 오는 반사음을 울림으로 본다
PSD_TAU = 0.02       # 스펙트럼 세기 평활 시간 (초)


def _fft_size(sr: int) -> int:
    return 2048 if sr <= 50_000 else 4096 if sr <= 100_000 else 8192


def dereverb(x: np.ndarray, sr: int, amount: float = 0.5, rt60: float = 0.8,
             progress=None) -> np.ndarray:
    """한 채널 float32 소리에서 늦은 울림을 줄인다. 길이는 그대로, 지연 없음.

    amount 0~1: 0 이면 그대로, 1 이면 가장 세게(바닥 −18dB). 0.5 가 기본.
    rt60: 공간의 울림 시간(초). 예배당 0.8~1.5.
    """
    amount = float(min(max(amount, 0.0), 1.0))
    if amount <= 0.0 or len(x) == 0:
        return x.astype(np.float32, copy=True)
    n = _fft_size(sr)
    hop = n // 4
    bins = n // 2 + 1
    delay = max(1, round(TD * sr / hop))                 # 몇 프레임 전을 '울림의 원인' 으로 볼지
    alpha = math.exp(-hop / (sr * PSD_TAU))
    delta = 3.0 * math.log(10) / max(rt60, 0.05)
    decay = math.exp(-2.0 * delta * delay * hop / sr)     # 그 사이에 울림이 얼마나 꺼졌나
    beta = 1.0 + amount                                   # over-subtraction
    floor = 10 ** (-18.0 * amount / 20.0)                 # 이보다 더 깎지는 않는다

    win = np.sqrt(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)).astype(np.float32)  # √Hann (75% 겹침)
    pad = np.concatenate([np.zeros(n - hop, np.float32), x.astype(np.float32),
                          np.zeros(n, np.float32)])
    frames = 1 + (len(pad) - n) // hop
    out = np.zeros(len(pad), np.float32)

    psd = np.zeros(bins, np.float64)
    hist = np.zeros((delay, bins), np.float64)
    gains = np.ones(bins, np.float64)
    chunk = 512                                           # 프레임 묶음 단위로 FFT (메모리 절약)
    scale = 0.5                                           # √Hann 분석·합성 쌍(=Hann)은 75% 겹침에서 2.0 으로 더해진다
    for c0 in range(0, frames, chunk):
        c1 = min(frames, c0 + chunk)
        idx = np.arange(c0, c1)[:, None] * hop + np.arange(n)[None, :]
        spec = np.fft.rfft(pad[idx] * win, axis=1)
        power = (spec.real ** 2 + spec.imag ** 2).astype(np.float64)
        g_all = np.empty((c1 - c0, bins), np.float64)
        for j in range(c1 - c0):
            f = c0 + j
            psd = alpha * psd + (1.0 - alpha) * power[j]
            psd[psd < 1e-30] = 0.0
            slot = f % delay
            late = decay * hist[slot] if f >= delay else np.zeros(bins)
            hist[slot] = psd
            with np.errstate(divide="ignore", invalid="ignore"):
                target = np.where(psd > 0.0, np.maximum(1.0 - beta * late / psd, floor), 1.0)
            # 말이 시작될 때는 빨리 열고, 울림을 깎을 때는 천천히 (음악적 잡음을 줄인다)
            coef = np.where(target > gains, 0.8, 0.5)
            gains = gains + (target - gains) * coef
            g_all[j] = gains
        # 이웃 주파수와 섞어 거친 자국을 없앤다
        sm = g_all.copy()
        sm[:, 1:-1] = 0.25 * g_all[:, :-2] + 0.5 * g_all[:, 1:-1] + 0.25 * g_all[:, 2:]
        sm[:, 0] = 0.75 * g_all[:, 0] + 0.25 * g_all[:, 1]
        sm[:, -1] = 0.75 * g_all[:, -1] + 0.25 * g_all[:, -2]
        y = np.fft.irfft(spec * sm, n=n, axis=1).astype(np.float32) * win * scale
        for j in range(c1 - c0):
            s = (c0 + j) * hop
            out[s:s + n] += y[j]
        if progress and (c1 % (chunk * 8) == 0 or c1 == frames):
            progress(c1 / frames)
    return out[n - hop:n - hop + len(x)]


# ------------------------------------------------------------------ 메모리
# ------------------------------------------------------------------ 파일 단위
def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"지원하지 않는 wav 비트 수: {sw * 8}")
    if ch > 1:
        x = x.reshape(-1, ch)
    return x, sr


def write_wav(path: Path, x: np.ndarray, sr: int) -> None:
    x = np.clip(x, -1.0, 1.0)
    ch = x.shape[1] if x.ndim == 2 else 1
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 32767.0).astype("<i2").tobytes())


def dereverb_file(src: Path, dst: Path, amount: float = 0.5, rt60: float = 0.8, progress=None) -> Path:
    """wav → wav. 스테레오면 채널마다 따로 처리한다."""
    x, sr = read_wav(src)
    if x.ndim == 2:
        y = np.stack([dereverb(x[:, c], sr, amount, rt60, progress) for c in range(x.shape[1])], axis=1)
    else:
        y = dereverb(x, sr, amount, rt60, progress)
    write_wav(dst, y, sr)
    return dst


# ------------------------------------------------------------------ 설교 영상에 붙이기
def cache_path(cfg, video_id: str) -> Path:
    return Path(cfg.cache_dir) / f"{video_id}.voice.wav"


def voice_wav(media: Path, cfg, video_id: str, progress=None) -> Path | None:
    """영상의 소리 전체에서 울림을 줄인 wav 를 만들어 둔다. 실패하면 None.

    왜 미리 통째로 만드나: 울림 줄이기는 ffmpeg 필터가 아니라 numpy 라 ffmpeg 그래프
    안에 넣을 수 없다. 원본과 **같은 타임라인**의 wav 를 따로 만들어 두면, 자르기
    ffmpeg 호출에서 `-i 영상 -i 이wav` 두 입력을 주고 소리만 이 wav 에서 가져오면
    된다 — atrim 시각이 그대로 맞으므로 기존 자르기 코드가 그대로 돈다.

    한 번 만들면 [설교 영상 만들기]·[mp3]·[3분 요약] 이 같은 파일을 다시 쓴다.
    """
    from .util import ToolError, ffprobe_duration, require, run_stream
    from . import machine

    amount = float(getattr(cfg, "dereverb_amount", 0.0) or 0.0)
    if amount <= 0.0:
        return None
    out = cache_path(cfg, video_id)
    if out.exists() and out.stat().st_size > 1024:
        print(f"  울림 줄인 소리를 이미 만들어 두었습니다: {out.name}")
        return out
    say = progress or (lambda t: print(f"  {t}"))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_in = out.with_suffix(".src.wav")
    try:
        # 꺼내기 전에 먼저 재 본다. x·pad·out 세 벌이 48kHz float32 로 동시에 떠 있는다.
        seconds = ffprobe_duration(Path(media)) or 0.0
        need = seconds * 48000 * 4 * 3 / 2 ** 20
        have = machine.headroom_mb()     # 남은 메모리가 아니라 더 쓸 수 있는 양
        if seconds and have is not None and need > have * 0.6:
            say(f"울림 줄이기를 건너뜁니다 — {seconds / 60:.0f}분 소리에 약 {need:.0f}MB 가 필요한데 "
                f"지금 쓸 수 있는 메모리는 {have:.0f}MB 입니다. 다른 프로그램을 닫으면 켜집니다")
            return None
        say("예배당 울림을 줄입니다 — 소리를 꺼내는 중")
        run_stream([
            require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-stats",
            "-i", str(Path(media).resolve()),
            "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le",
            str(tmp_in.resolve()),
        ], tag="[ffmpeg] ")
        x, sr = read_wav(tmp_in)
        if x.ndim == 2:
            x = x.mean(axis=1)
        mins = len(x) / sr / 60
        say(f"울림 줄이는 중 — {mins:.0f}분 (이 줄 다음에 진행률이 나옵니다)")
        last = [0.0]

        def tick(frac: float) -> None:
            if frac - last[0] >= 0.1 or frac >= 1.0:
                last[0] = frac
                print(f"  [울림] {frac * 100:3.0f}%", flush=True)

        y = dereverb(x, sr, amount=amount,
                     rt60=float(getattr(cfg, "dereverb_rt60", 1.0)), progress=tick)
        write_wav(out, y, sr)
        say(f"울림을 줄였습니다 ({out.name})")
        return out
    except (ToolError, OSError, ValueError, MemoryError) as exc:
        # 울림 줄이기는 덧붙이는 기능이다 — 실패해도 원본 소리로 그대로 간다.
        say(f"울림 줄이기를 건너뜁니다 ({type(exc).__name__}: {exc})")
        out.unlink(missing_ok=True)
        return None
    finally:
        tmp_in.unlink(missing_ok=True)
