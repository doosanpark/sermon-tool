"""오디오에서 목사님이 숨을 고르는 자리를 찾는다.

전사문에는 쉼이 기록되지 않는다. faster-whisper 를 vad_filter 로 돌리면
무음이 잘려 나가고 말만 이어 붙기 때문이다.
실측(2026-07-26 설교): 551문장 사이 간격 550건이 **전부 0.000초** 였다.

그래서 자를 자리는 오디오를 직접 봐야 한다. 50밀리초 프레임의 RMS 를 재서
배경 소음 바닥보다 일정 폭 아래로 떨어지는 구간을 쉼으로 본다.
numpy 와 표준 wave 만 쓰므로 새로 받을 것이 없고, torch 도 필요 없다.

실측(같은 설교 36.2분, CPU 몇 초):
    0.8초 이상 쉼  300곳  — 평균 7초에 한 번 (호흡)
    2.0초 이상 쉼   87곳  — 평균 25초에 한 번 (문단 전환)

왜 필요한가:
    문장 끝에서 그냥 자르면 말꼬리가 잘린다. 실제로 전사문 문장 시작점에서
    가장 가까운 쉼까지 중앙값 1.98초, 최대 49초 떨어져 있었다.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .util import ToolError

SAMPLE_RATE = 16000          # voiceprint.to_wav 가 만드는 규격과 같다
FRAME = 0.05                 # 50밀리초. 이보다 짧으면 잡음에 흔들린다
MIN_PAUSE = 0.30             # 이보다 짧은 무음은 말 사이 끊김이지 쉼이 아니다
MARGIN_DB = 8.0              # 배경 소음 바닥 + 이만큼 아래면 무음으로 본다


@dataclass
class Pause:
    """소리가 멎은 구간. start 에서 멎고 end 에서 다시 말이 시작된다."""
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def read_range(wav_path: Path, start: float, end: float) -> np.ndarray:
    """wav 의 [start, end] 구간만 읽는다. 전체를 메모리에 올리지 않는다."""
    wav_path = Path(wav_path)
    if not wav_path.exists():
        raise ToolError(
            f"소리 파일이 없습니다: {wav_path.name}\n"
            "먼저 [자동으로 찾기] 를 눌러 영상을 분석하세요.")

    with wave.open(str(wav_path), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ToolError("16bit PCM wav 만 지원합니다.")
        rate = wf.getframerate()
        channels = wf.getnchannels()
        total = wf.getnframes()

        a = max(0, int(start * rate))
        b = min(total, int(end * rate))
        if b <= a:
            return np.zeros(0, dtype=np.float32)
        wf.setpos(a)
        raw = wf.readframes(b - a)

    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio


def find_pauses(wav_path: Path, start: float, end: float,
                min_len: float = MIN_PAUSE, frame: float = FRAME,
                margin_db: float = MARGIN_DB) -> list[Pause]:
    """[start, end] 안에서 소리가 멎는 구간을 모두 찾는다.

    판정선을 절대값으로 두지 않고 **그 영상의 배경 소음 바닥에서 띄워** 잡는다.
    교회마다 마이크 험과 에어컨 소리가 달라서, 고정값으로는 어떤 영상에서는
    쉼이 하나도 안 잡히고 어떤 영상에서는 말까지 무음으로 본다.
    """
    audio = read_range(wav_path, start, end)
    step = max(1, int(frame * SAMPLE_RATE))
    n = len(audio) // step
    if n < 4:
        return []

    rms = np.sqrt((audio[:n * step].reshape(n, step) ** 2).mean(axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-6))

    floor = float(np.percentile(db, 5))       # 하위 5% = 아무도 말 안 할 때
    threshold = floor + margin_db
    quiet = db < threshold

    pauses: list[Pause] = []
    i = 0
    while i < n:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < n and quiet[j]:
            j += 1
        length = (j - i) * frame
        if length >= min_len:
            pauses.append(Pause(start + i * frame, start + j * frame))
        i = j
    return pauses


def snap_start(t: float, pauses: list[Pause], limit: float = 3.0,
               lead: float = 0.20) -> float:
    """시작 시각을 **앞쪽** 쉼으로 당긴다.

    가장 가까운 쉼으로 붙이면 안 된다. 뒤쪽 쉼이 더 가까우면 첫 마디가
    통째로 잘린다 — 실제로 규칙만으로 자른 시험판에서 "두 번째입니다" 가
    1초 차이로 날아갔다.

    말이 다시 시작되는 지점(pause.end)보다 lead 만큼 앞에서 연다.
    아주 짧은 정적으로 시작해야 갑자기 튀어나오지 않는다.
    """
    before = [p for p in pauses if p.end <= t + 0.5 and p.end >= t - limit]
    if not before:
        return max(0.0, t - lead)
    best = max(before, key=lambda p: p.end)
    return max(0.0, best.end - lead)


def snap_end(t: float, pauses: list[Pause], limit: float = 3.0,
             tail: float = 0.30) -> float:
    """끝 시각을 **뒤쪽** 쉼으로 민다. 말이 끝나고 tail 만큼 여운을 둔다."""
    after = [p for p in pauses if p.start >= t - 0.5 and p.start <= t + limit]
    if not after:
        return t + tail
    best = min(after, key=lambda p: p.start)
    return best.start + tail


def snap_span(start: float, end: float, pauses: list[Pause],
              limit: float = 3.0) -> tuple[float, float]:
    """구간 하나를 통째로 쉼에 맞춘다. 뒤집히면 원래 값을 그대로 둔다."""
    s = snap_start(start, pauses, limit)
    e = snap_end(end, pauses, limit)
    if e - s < 0.5:
        return start, end
    return s, e


def make_snap(pauses: list[Pause], limit: float = 3.0):
    """구간을 쉼에 맞춰 주는 함수를 만들어 돌려준다.

    highlight 쪽에 넘겨 두면 화면에 뜨는 길이가 실제로 나올 영상 길이와 같아진다.
    쉼 목록은 한 번만 계산해 두고 이 함수만 계속 부르므로 클릭할 때마다
    다시 훑지 않는다.
    """
    def snap(start: float, end: float) -> tuple[float, float]:
        return snap_span(start, end, pauses, limit)
    return snap


def paragraph_pauses(pauses: list[Pause], min_len: float = 2.0) -> list[Pause]:
    """문단이 바뀌는 자리. 요약 조각의 시작점으로 삼기 좋다."""
    return [p for p in pauses if p.duration >= min_len]


def describe(pauses: list[Pause]) -> str:
    """사람이 읽을 요약 한 줄. 로그와 selftest 에 쓴다."""
    if not pauses:
        return "쉼을 찾지 못했습니다"
    para = len(paragraph_pauses(pauses))
    return (f"쉼 {len(pauses)}곳 (0.8초 이상 "
            f"{sum(1 for p in pauses if p.duration >= 0.8)}곳, "
            f"문단급 2초 이상 {para}곳)")
