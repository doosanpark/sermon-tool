"""faster-whisper 래퍼.

전체가 아니라 '확정된 구간에만' 돌린다. 2시간 영상에서 설교 37분 + 축도 2분만
처리하므로 전체 대비 약 1/3 시간이면 끝난다.
"""
from __future__ import annotations

import gc
import json
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from .config import Config
from .fixwords import fix_text, fix_transcript
from .util import ToolError, _mmss, is_memory_error, require, run

_MODEL_CACHE: dict[tuple[str, str, str], object] = {}


# 메모리가 모자랄 때 내려갈 차례. 큰 것부터 작은 것 순이다.
# medium 은 int8 로도 1.5GB 안팎을 쓴다. 메모리가 빠듯한 PC 에서는 올리다 멈춘다
# (2026-10-20 교회 PC: RuntimeError: mkl_malloc: failed to allocate memory).
_LADDER = ("large-v3", "medium", "small", "base", "tiny")


def _get_model(cfg: Config, say=print):
    """자막 모델을 올린다. 메모리가 모자라면 더 가볍게 바꿔 가며 다시 해 본다.

    왜 이렇게까지 하는가:
        교회 PC 는 메모리가 넉넉하지 않다. 설정해 둔 모델을 못 올린다고 그냥
        멈춰 버리면 자막도 요약도 못 만든다. 조금 덜 정확해도 **되는 쪽**이
        아무것도 안 되는 것보다 낫다. 대신 어느 모델로 만들었는지 꼭 알린다.

    순서:
        ① 정해 둔 모델 그대로
        ② 같은 모델, 스레드 2개로 (스레드마다 작업 공간을 따로 잡는다)
        ③ 한 단계 작은 모델로 — 다시 ①②
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ToolError("faster-whisper 가 설치되어 있지 않습니다.  pip install faster-whisper") from exc

    # 올리기 전에 메모리를 먼저 재서 올라갈 만한 것을 고른다. 거의 바닥난 상태에서
    # 큰 모델을 올리면 계산 라이브러리가 오류도 못 내고 프로그램을 끝내 버린다.
    from . import machine

    wanted = cfg.whisper_model
    pick, why = machine.whisper_model_for(wanted, _LADDER)
    if why:
        say(f"  [자막] {why}", flush=True)
    chain = list(_LADDER[_LADDER.index(pick):]) if pick in _LADDER else [pick]
    last: BaseException | None = None

    for name in chain:
        for threads in (None, 2):
            key = (name, cfg.device, cfg.compute_type)
            if key in _MODEL_CACHE:
                return _MODEL_CACHE[key]
            kw = {"device": cfg.device, "compute_type": cfg.compute_type}
            if threads:
                kw["cpu_threads"] = threads
            try:
                model = WhisperModel(name, **kw)
            except Exception as exc:                  # noqa: BLE001 — 아래에서 가린다
                if not is_memory_error(exc):
                    raise
                last = exc
                continue
            if name != pick:
                say(f"  [자막] '{pick}' 도 올리지 못해 '{name}' 모델로 받아씁니다 — "
                    "글자가 조금 더 틀릴 수 있습니다", flush=True)
            elif threads:
                say(f"  [자막] 메모리를 아끼려고 스레드 {threads}개로 올렸습니다 — 조금 느립니다",
                    flush=True)
            _MODEL_CACHE[key] = model
            return model

    raise ToolError(
        "자막 모델을 올릴 메모리가 모자랍니다 (가장 작은 모델까지 해 봤습니다).\n\n"
        "해결 1: 웹 브라우저를 비롯해 다른 프로그램을 모두 닫고 다시 해 주세요.\n"
        "해결 2: 컴퓨터를 한 번 다시 켜고 바로 해 보세요.\n\n"
        "자막 없이도 [설교 영상 만들기] 와 [설교 mp3] 는 됩니다. 요약은 자막이 필요합니다."
    ) from last


def release_model() -> None:
    """올려 둔 자막 모델을 내려놓는다.

    한 번 올리면 캐시에 남아 1GB 안팎을 계속 차지한다. 자막을 다 만든 뒤
    영상을 만들 때 ffmpeg 이 쓸 메모리가 그만큼 줄어드는데, 메모리가 빠듯한
    PC 에서는 바로 그 차이로 영상 만들기가 실패한다
    (2026-10-20: ffmpeg 이 "Cannot allocate memory" 로 멈췄다).

    다시 자막을 만들 때 10초쯤 걸려 다시 올리면 된다. 그 편이 낫다.
    """
    if not _MODEL_CACHE:
        return
    _MODEL_CACHE.clear()
    gc.collect()


def _slice_wav(wav_path: Path, start: float, end: float, out_path: Path) -> Path:
    run([
        require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", str(wav_path),
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(out_path),
    ])
    return out_path


def _read_wav16k(path: Path):
    """_slice_wav 가 만든 16kHz 모노 16비트 wav 를 float32 배열로 읽는다.

    faster-whisper 에 파일 경로를 주면 PyAV(av)로 다시 디코딩하는데, 둘의 판이
    어긋나면 깨진다. 실제로 faster-whisper 1.2.1 + av 19 조합에서
    `open() got an unexpected keyword argument 'metadata_errors'` 로 자막 만들기가
    통째로 죽었다. 이미 ffmpeg 로 원하는 형식을 만들어 두었으니 그대로 읽어 넘기면
    av 를 아예 거치지 않는다. 배열을 받으면 faster-whisper 는 16kHz 로 간주한다.
    """
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ToolError(f"자막용 오디오 형식이 예상과 다릅니다: {path}")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def transcribe_range(wav_path: Path, start: float, end: float, cfg: Config,
                     label: str = "", progress: bool = True,
                     step: float = 3.0) -> list[dict]:
    """[start, end] 구간을 전사한다. 타임스탬프는 원본 영상 기준 절대시간으로 돌려준다.

    진행 상황을 흘리는 이유:
        설교 35분을 한 번에 넘기면 CPU 에서 20~60분이 걸린다. 그동안 아무것도
        안 찍히면 쓰는 분은 멈춘 줄로 안다 — 실제로 그런 신고가 있었다.
        faster-whisper 는 결과를 제너레이터로 하나씩 내주므로, 받는 족족
        어디까지 왔는지 계산해 ffmpeg 과 같은 모양으로 찍는다.
    """
    model = _get_model(cfg)
    span = max(end - start, 0.001)
    pieces = _pieces(start, end)
    out: list[dict] = []
    began = time.monotonic()
    last_at = [0.0]
    if progress:
        extra = f" · {len(pieces)}조각으로 나눠서" if len(pieces) > 1 else ""
        print(f"  [자막] {label or '구간'} {span / 60:.1f}분 받아쓰는 중{extra} — "
              "이 줄이 움직이면 잘 돌고 있는 겁니다", flush=True)
    for a, b in pieces:
        out.extend(_one_piece(model, wav_path, a, b, cfg, label,
                              base=a - start, span=span, began=began, last_at=last_at,
                              progress=progress, step=step))
    if progress:
        print(f"  [자막] 끝 — {_mmss(time.monotonic() - began)} 걸려 {len(out)}문장", flush=True)
    return out


def _piece_seconds() -> float:
    """한 번에 넘길 길이(초). 남은 메모리에 맞춰 정한다.

    faster-whisper 는 넘긴 소리 **전체**의 특징을 float64 로 한꺼번에 만든다.
    35분을 통째로 넘기면 그것만 564MB 다 — 교회 PC 가 바로 여기서 멈췄다
    (2026-10-20: Unable to allocate 564. MiB ... shape (1, 184670, 400)).

    끊는 자리마다 문장이 갈릴 수 있으므로, 메모리가 넉넉하면 길게 끊는다.
    """
    from . import machine

    have = machine.headroom_mb()        # 남은 메모리가 아니라 더 쓸 수 있는 양
    if have is None:
        return 600.0
    if have > 6000:
        return 600.0        # 10분씩 — 끊는 자리가 가장 적다
    if have > 3000:
        return 420.0        # 7분씩
    if have > 1500:
        return 300.0        # 5분씩
    return 180.0            # 3분씩 — 아주 빠듯한 PC


def _pieces(start: float, end: float) -> list[tuple[float, float]]:
    size = _piece_seconds()
    out, t = [], start
    while t < end - 0.05:
        out.append((t, min(t + size, end)))
        t += size
    return out or [(start, end)]


def _one_piece(model, wav_path: Path, a: float, b: float, cfg: Config, label: str,
               *, base: float, span: float, began: float, last_at: list[float],
               progress: bool, step: float, depth: int = 0) -> list[dict]:
    """조각 하나를 받아쓴다. 시각은 원본 영상 기준 절대시간으로 바꿔 돌려준다.

    메모리가 모자라 멈추면 그 조각을 **반으로 나눠 다시** 해 본다. 모델을 올리는
    것과 달리 받아쓰는 중에 드는 양은 미리 재기 어려워서, 겪고 나서 줄이는 수밖에
    없다 (2026-10-20: 10분 조각이 generate_segments 에서 mkl_malloc 으로 멈췄다).
    되돌아간 결과만 담으므로 같은 말이 두 번 들어가지 않는다.
    """
    out: list[dict] = []
    try:
        return _run_piece(model, wav_path, a, b, cfg, label, out, base=base, span=span,
                          began=began, last_at=last_at, progress=progress, step=step)
    except Exception as exc:                          # noqa: BLE001 — 아래에서 가린다
        if depth >= 3 or not is_memory_error(exc) or (b - a) < 45:
            raise
        mid = (a + b) / 2
        if progress:
            print(f"  [자막] 메모리가 모자라 이 조각을 반으로 나눠 다시 합니다 "
                  f"({(b - a) / 60:.0f}분 → {(mid - a) / 60:.0f}분씩)", flush=True)
        half = _one_piece(model, wav_path, a, mid, cfg, label, base=base, span=span,
                          began=began, last_at=last_at, progress=progress, step=step,
                          depth=depth + 1)
        half += _one_piece(model, wav_path, mid, b, cfg, label, base=base + (mid - a),
                           span=span, began=began, last_at=last_at, progress=progress,
                           step=step, depth=depth + 1)
        return half


def _run_piece(model, wav_path: Path, a: float, b: float, cfg: Config, label: str,
               out: list[dict], *, base: float, span: float, began: float,
               last_at: list[float], progress: bool, step: float) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        clip = _slice_wav(wav_path, a, b, Path(tmp) / "clip.wav")
        segments, _info = model.transcribe(
            _read_wav16k(clip),
            language=cfg.language,
            beam_size=cfg.beam_size,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            initial_prompt=cfg.initial_prompt or None,
            condition_on_previous_text=False,
        )
        for seg in segments:
            done = min(base + float(seg.end), span)
            now = time.monotonic()
            if progress and now - last_at[0] >= step:
                last_at[0] = now
                spent = now - began
                speed = done / spent if spent > 0 else 0.0
                left = (span - done) / speed if speed > 0.01 else 0.0
                print(f"  [자막] {done / span * 100:3.0f}%  "
                      f"{_mmss(done)} / {_mmss(span)}   {speed:.1f}배속"
                      + (f"   남은 시간 약 {_mmss(left)}" if left > 1 else ""),
                      flush=True)
            text = fix_text((seg.text or "").strip(), cfg.work_dir)   # 오인식 바로잡기
            if not text:
                continue
            out.append({
                "start": round(a + float(seg.start), 3),
                "end": round(a + float(seg.end), 3),
                "text": text,
                "label": label,
            })
    return out


def save_transcript(path: Path, segments: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(segments, ensure_ascii=False, indent=1), encoding="utf-8")


def load_transcript(path: Path) -> list[dict]:
    """저장해 둔 자막을 읽는다. 읽을 때마다 오인식 사전을 다시 적용한다.

    사전에 말을 더하면(고칠말.txt) 예전에 만든 자막에도 바로 적용되게 하려는 것이다.
    고친 결과는 같은 자리에 다시 쓰지 않는다 — 원래 받아쓴 글자는 파일에 남는다.
    """
    p = Path(path)
    if not p.exists():
        return []
    segments = json.loads(p.read_text(encoding="utf-8"))
    try:
        fix_transcript(segments, p.parent.parent)      # cache/ 의 위가 data/
    except Exception:
        pass
    return segments
