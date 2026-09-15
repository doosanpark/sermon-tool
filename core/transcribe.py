"""faster-whisper 래퍼.

전체가 아니라 '확정된 구간에만' 돌린다. 2시간 영상에서 설교 37분 + 축도 2분만
처리하므로 전체 대비 약 1/3 시간이면 끝난다.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from .config import Config
from .util import ToolError, _mmss, require, run

_MODEL_CACHE: dict[tuple[str, str, str], object] = {}


def _get_model(cfg: Config):
    key = (cfg.whisper_model, cfg.device, cfg.compute_type)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ToolError("faster-whisper 가 설치되어 있지 않습니다.  pip install faster-whisper") from exc
    model = WhisperModel(cfg.whisper_model, device=cfg.device, compute_type=cfg.compute_type)
    _MODEL_CACHE[key] = model
    return model


def _slice_wav(wav_path: Path, start: float, end: float, out_path: Path) -> Path:
    run([
        require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", str(wav_path),
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(out_path),
    ])
    return out_path


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
    with tempfile.TemporaryDirectory() as tmp:
        clip = _slice_wav(wav_path, start, end, Path(tmp) / "clip.wav")
        segments, _info = model.transcribe(
            str(clip),
            language=cfg.language,
            beam_size=cfg.beam_size,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            initial_prompt=cfg.initial_prompt or None,
            condition_on_previous_text=False,
        )
        out = []
        began = time.monotonic()
        last_at = 0.0
        if progress:
            print(f"  [자막] {label or '구간'} {span / 60:.1f}분 받아쓰는 중 — "
                  "이 줄이 움직이면 잘 돌고 있는 겁니다", flush=True)
        for seg in segments:
            done = min(float(seg.end), span)
            now = time.monotonic()
            if progress and now - last_at >= step:
                last_at = now
                spent = now - began
                speed = done / spent if spent > 0 else 0.0
                left = (span - done) / speed if speed > 0.01 else 0.0
                print(f"  [자막] {done / span * 100:3.0f}%  "
                      f"{_mmss(done)} / {_mmss(span)}   {speed:.1f}배속"
                      + (f"   남은 시간 약 {_mmss(left)}" if left > 1 else ""),
                      flush=True)
            text = (seg.text or "").strip()
            if not text:
                continue
            out.append({
                "start": round(start + float(seg.start), 3),
                "end": round(start + float(seg.end), 3),
                "text": text,
                "label": label,
            })
        if progress:
            spent = time.monotonic() - began
            print(f"  [자막] 끝 — {_mmss(spent)} 걸려 {len(out)}문장", flush=True)
    return out


def save_transcript(path: Path, segments: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(segments, ensure_ascii=False, indent=1), encoding="utf-8")


def load_transcript(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))
