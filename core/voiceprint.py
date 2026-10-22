"""성문(voiceprint) 기반 설교 구간 검출 — CPU 전용.

화자 분리(pyannote)는 "이 영상에 몇 명이 있고 누가 누구인지"를 매번 처음부터
계산하므로 CPU 에서 30~90분이 걸린다. 목사님 성문을 미리 만들어 두면 런타임에는
"지금 3초가 목사님 목소리인가"만 판단하면 되고, 이는 ONNX CPU 로 실시간의 100배
이상 빠르다. torch 가 필요 없어 설치본에도 넣을 수 있다.

  등록: 이미 잘라 둔 설교 영상 여러 편 → 평균 임베딩 → voiceprint.npz
  검출: 전체 오디오를 3초 창으로 훑어 유사도 → 임계값 → 최장 블록
"""
from __future__ import annotations

import json
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .util import PROJECT_ROOT, ToolError, has_audio, hms, require, run

MODEL_DIR_CANDIDATES = [
    PROJECT_ROOT / "bin" / "models",
    PROJECT_ROOT / "models",
]
EMBED_MODEL = "campplus.onnx"
SAMPLE_RATE = 16000


# ------------------------------------------------------------------ 오디오
def find_model(name: str = EMBED_MODEL) -> Path:
    for d in MODEL_DIR_CANDIDATES:
        p = d / name
        if p.exists():
            return p
    raise ToolError(
        f"성문 모델을 찾지 못했습니다: {name}\n"
        f"  넣을 위치: {MODEL_DIR_CANDIDATES[0]}\n"
        "  받는 곳: https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models\n"
        "  (3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx 를 campplus.onnx 로 저장)"
    )


def to_wav(media: Path, out_wav: Path, start: float | None = None,
           end: float | None = None) -> Path:
    """어떤 미디어든 16kHz 모노 wav 로. 구간만 뽑을 수도 있다."""
    media = Path(media)
    if not media.exists():
        raise ToolError(f"파일이 없습니다: {media}")
    if not has_audio(media):
        raise ToolError(
            f"이 파일에는 소리가 없습니다: {media.name}\n"
            "영상 트랙만 있는 파일입니다. 유튜브에서 영상과 소리를 따로 받아\n"
            "합치는 도중 끊기면 이런 파일이 남습니다.\n\n"
            "해결: 이 파일을 지우고 [영상 가져오기] 로 다시 받으세요.\n"
            f"  {media}"
        )

    cmd = [require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += ["-i", str(media), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le", str(out_wav)]
    run(cmd)          # 실패하면 ffmpeg 의 stderr 를 그대로 담아 예외를 던진다
    return out_wav


def read_wav(path: Path) -> np.ndarray:
    """wav 를 통째로 메모리에 올린다. 짧은 파일에만 쓴다.

    긴 예배 영상에는 WavFrames 를 쓴다 — 아래 설명 참고.
    """
    with wave.open(str(path), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ToolError("16bit PCM wav 만 지원합니다.")
        channels = wf.getnchannels()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    audio = data.astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio


class WavFrames:
    """wav 를 통째로 올리지 않고, 쓸 조각만 그때그때 읽어 온다.

    왜 필요한가 (2026-10-20, 교회 PC):
        82분 예배는 16kHz 로 7,850만 개 값이다. float32 로 바꾸면 300MB 이고,
        바꾸기 전 16비트 원본 157MB 가 함께 떠 있어 한순간 460MB 가 필요하다.
        메모리가 넉넉하지 않은 PC 에서는 바로 여기서 멈춘다:
            numpy ... Unable to allocate 300. MiB for an array with shape (78544208,)
        설교를 찾는 일 자체는 3초짜리 창을 하나씩 보는 것이라, 애초에 전부
        올려 둘 이유가 없었다.

    어떻게:
        _windows() 가 쓰는 것은 len(audio) 와 audio[i:j] 둘뿐이다. 그 둘만
        흉내내고 속으로는 그 자리만 파일에서 읽는다. 한 조각은 3초 = 약 190KB 라
        아무리 긴 영상이라도 쓰는 메모리가 늘지 않는다.

    쓰는 법 (반드시 with 로 — Windows 는 파일이 열려 있으면 임시 폴더를 못 지운다):
        with WavFrames(wav) as audio:
            times, scores = scan(audio, vp, embedder)
    """

    def __init__(self, path: Path):
        self._wf = wave.open(str(path), "rb")
        if self._wf.getsampwidth() != 2:
            self._wf.close()
            raise ToolError("16bit PCM wav 만 지원합니다.")
        self._channels = self._wf.getnchannels()
        self._frames = self._wf.getnframes()

    def __len__(self) -> int:
        return self._frames

    def __getitem__(self, cut) -> np.ndarray:
        if not isinstance(cut, slice):
            raise TypeError("WavFrames 는 audio[i:j] 꼴로만 읽습니다")
        start, stop, _ = cut.indices(self._frames)
        count = max(stop - start, 0)
        if count == 0:
            return np.zeros(0, dtype=np.float32)
        self._wf.setpos(start)
        raw = np.frombuffer(self._wf.readframes(count), dtype="<i2")
        audio = raw.astype(np.float32) / 32768.0
        if self._channels > 1:
            audio = audio.reshape(-1, self._channels).mean(axis=1)
        return audio

    def close(self) -> None:
        self._wf.close()

    def __enter__(self) -> "WavFrames":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# ------------------------------------------------------------------ 임베딩
class Embedder:
    def __init__(self, model: Path | None = None, threads: int = 2):
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise ToolError("sherpa-onnx 가 필요합니다.  pip install sherpa-onnx") from exc
        self._model_path = Path(model) if model else find_model()
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(self._model_path), num_threads=threads, debug=False)
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.dim = self._ex.dim

    def embed(self, samples: np.ndarray) -> np.ndarray:
        stream = self._ex.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples.astype(np.float32))
        stream.input_finished()
        vec = np.asarray(self._ex.compute(stream), dtype=np.float32)
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec


# ------------------------------------------------------------------ 성문
@dataclass
class Voiceprint:
    vector: np.ndarray
    window: float = 3.0
    sources: list[str] = field(default_factory=list)
    sample_count: int = 0
    model: str = EMBED_MODEL

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, vector=self.vector, window=self.window,
                 meta=json.dumps({"sources": self.sources,
                                  "sample_count": self.sample_count,
                                  "model": self.model}, ensure_ascii=False))
        return path

    @classmethod
    def load(cls, path: Path) -> "Voiceprint":
        data = np.load(Path(path), allow_pickle=False)
        meta = json.loads(str(data["meta"]))
        return cls(vector=data["vector"], window=float(data["window"]),
                   sources=meta.get("sources", []),
                   sample_count=int(meta.get("sample_count", 0)),
                   model=meta.get("model", EMBED_MODEL))

    def score(self, vec: np.ndarray) -> float:
        return float(np.dot(self.vector, vec))


def _windows(audio: np.ndarray, window: float, hop: float, min_rms: float):
    """무음을 건너뛰고 창 단위로 잘라 낸다."""
    w = int(window * SAMPLE_RATE)
    h = int(hop * SAMPLE_RATE)
    for i in range(0, max(len(audio) - w, 0) + 1, h):
        chunk = audio[i:i + w]
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        yield i / SAMPLE_RATE, chunk, rms >= min_rms


def enroll(sermon_wavs: list[Path], embedder: Embedder, window: float = 3.0,
           hop: float = 3.0, min_rms: float = 0.01, max_per_file: int = 400,
           progress=print) -> Voiceprint:
    """이미 잘라 둔 설교 오디오에서 평균 성문을 만든다."""
    vectors: list[np.ndarray] = []
    sources: list[str] = []
    for path in sermon_wavs:
        taken = 0
        with WavFrames(Path(path)) as audio:       # 여기도 조각만 읽는다
            for _, chunk, voiced in _windows(audio, window, hop, min_rms):
                if not voiced:
                    continue
                vectors.append(embedder.embed(chunk))
                taken += 1
                if taken >= max_per_file:
                    break
        sources.append(Path(path).stem)
        progress(f"  {Path(path).stem}: 표본 {taken}개")

    if not vectors:
        raise ToolError("성문을 만들 음성 표본이 없습니다.")

    stack = np.vstack(vectors)
    mean = stack.mean(axis=0)
    mean /= np.linalg.norm(mean)

    # 평균에서 크게 벗어난 표본(다른 화자·잡음)을 한 번 걸러 다시 평균낸다
    sims = stack @ mean
    keep = stack[sims >= np.percentile(sims, 20)]
    refined = keep.mean(axis=0)
    refined /= np.linalg.norm(refined)
    progress(f"  표본 {len(stack)}개 → 정제 후 {len(keep)}개 사용")

    return Voiceprint(vector=refined.astype(np.float32), window=window,
                      sources=sources, sample_count=int(len(keep)))


# ------------------------------------------------------------------ 검출
def scan(audio: np.ndarray, vp: Voiceprint, embedder: Embedder,
         hop: float = 1.5, min_rms: float = 0.008,
         progress=None, report_every: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    """전체 오디오를 훑어 (시각, 유사도) 를 돌려준다. 무음은 -1.

    수십 분짜리 오디오는 훑는 데 1~2분이 걸리므로, 아무 출력이 없으면 멈춘 것처럼
    보인다. progress 를 주면 진행률과 남은 시간을 알려준다.
    """
    step = int(hop * SAMPLE_RATE)
    win = int(vp.window * SAMPLE_RATE)
    total = max((len(audio) - win) // step + 1, 1)

    times, scores = [], []
    started = time.monotonic()
    next_mark = report_every

    for i, (t, chunk, voiced) in enumerate(_windows(audio, vp.window, hop, min_rms)):
        times.append(t)
        scores.append(vp.score(embedder.embed(chunk)) if voiced else -1.0)

        if progress is None:
            continue
        frac = (i + 1) / total
        if frac >= next_mark:
            elapsed = time.monotonic() - started
            eta = elapsed / max(frac, 1e-6) - elapsed
            hot = sum(1 for s in scores[-60:] if s >= 0.6)
            progress(f"    {frac*100:3.0f}%   {hms(t)} 지점   "
                     f"남은 시간 약 {eta:.0f}초   최근 목사님 발화 {hot*100//60}%")
            next_mark += report_every

    if progress is not None:
        progress(f"    훑기 완료 — {time.monotonic()-started:.0f}초")
    return np.array(times), np.array(scores, dtype=np.float32)


def smooth(scores: np.ndarray, k: int = 7) -> np.ndarray:
    """중앙값 필터. 한두 창의 튐을 없앤다."""
    if k <= 1 or len(scores) < k:
        return scores
    pad = k // 2
    padded = np.pad(scores, (pad, pad), mode="edge")
    return np.array([np.median(padded[i:i + k]) for i in range(len(scores))],
                    dtype=np.float32)


def detect(times: np.ndarray, scores: np.ndarray, threshold: float = 0.5,
           gap: float = 90.0, min_len: float = 600.0) -> list[tuple[float, float]]:
    """임계값을 넘는 구간을 gap 이하 간격이면 이어 붙이고, 긴 것부터 돌려준다."""
    hot = scores >= threshold
    blocks: list[list[float]] = []
    step = float(times[1] - times[0]) if len(times) > 1 else 1.5
    for t, is_hot in zip(times, hot):
        if not is_hot:
            continue
        if blocks and t - blocks[-1][1] <= gap:
            blocks[-1][1] = t + step
        else:
            blocks.append([t, t + step])
    found = [(s, e) for s, e in blocks if e - s >= min_len]
    return sorted(found, key=lambda b: -(b[1] - b[0]))


# ------------------------------------------------------------------ 예배 구조 판정
@dataclass
class ServiceResult:
    sermon: tuple[float, float] | None
    benediction: tuple[float, float] | None
    confidence: float
    reasons: list[str]
    inside_mean: float = 0.0
    outside_mean: float = 0.0


def find_service(times: np.ndarray, scores: np.ndarray, *,
                 threshold: float = 0.6, smooth_k: int = 9,
                 join_gap: float = 45.0, min_block: float = 20.0,
                 sermon_min: float = 15 * 60, sermon_max: float = 60 * 60,
                 bene_min: float = 60.0, bene_max: float = 480.0,
                 bene_lead_gap: float = 60.0) -> ServiceResult:
    """설교와 축도를 함께 찾는다.

    실제 예배 3편에서 확인된 구조:
        설교(33~39분) → 찬양·헌금 약 4분(목사님 아님) → 축도 3.4~3.6분 → 끝
    따라서 축도는 "설교 뒤, 1분 이상 공백을 두고 오는 1~8분짜리 목사님 발화" 이고,
    여러 개면 가장 뒤엣것을 택한다.
    """
    sm = smooth(scores, smooth_k)
    blocks = sorted(detect(times, sm, threshold=threshold,
                           gap=join_gap, min_len=min_block), key=lambda b: b[0])
    if not blocks:
        return ServiceResult(None, None, 0.0, ["목사님 발화 구간을 찾지 못했습니다"])

    sermon = max(blocks, key=lambda b: b[1] - b[0])
    length = sermon[1] - sermon[0]

    after = [b for b in blocks if b[0] >= sermon[1]]
    candidates = []
    prev_end = sermon[1]
    for b in after:
        lead = b[0] - prev_end
        dur = b[1] - b[0]
        if bene_min <= dur <= bene_max and lead >= bene_lead_gap:
            candidates.append((b, lead, dur))
        prev_end = max(prev_end, b[1])
    bene = candidates[-1][0] if candidates else None

    # 신뢰도 — 성문 대비가 얼마나 뚜렷한가, 길이가 정상인가, 최장 블록이 압도적인가
    voiced = scores > -1
    in_mask = voiced & (times >= sermon[0]) & (times <= sermon[1])
    out_mask = voiced & ~((times >= sermon[0]) & (times <= sermon[1]))
    inside = float(scores[in_mask].mean()) if in_mask.any() else 0.0
    outside = float(scores[out_mask].mean()) if out_mask.any() else 0.0
    contrast = inside - outside

    others = [b for b in blocks if b is not sermon]
    second = max((b[1] - b[0] for b in others), default=1.0)
    ratio = length / max(second, 1.0)

    score = 0.0
    score += 0.4 * min(contrast / 0.35, 1.0)
    in_range = sermon_min <= length <= sermon_max
    score += 0.4 if in_range else 0.0
    score += 0.2 * min(ratio / 4.0, 1.0)

    reasons = [
        f"성문 대비 {contrast:.2f} (안 {inside:.2f} / 밖 {outside:.2f})",
        f"설교 길이 {length/60:.1f}분 " + ("정상" if in_range else "비정상"),
        f"최장/2위 {ratio:.1f}배",
    ]
    if bene:
        lead = next(c[1] for c in candidates if c[0] is bene)
        reasons.append(f"축도 {(bene[1]-bene[0])/60:.1f}분, 앞 공백 {lead/60:.1f}분")
    else:
        reasons.append("축도 후보 없음")

    return ServiceResult(sermon, bene, round(min(score, 1.0), 2), reasons, inside, outside)


def analyze_media(media: Path, vp: Voiceprint, embedder: Embedder, *,
                  hop: float = 3.0, progress=print, **kwargs) -> ServiceResult:
    """영상/오디오 한 편을 통째로 분석한다."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        if Path(media).suffix.lower() == ".wav":
            wav = Path(media)
        else:
            progress("  오디오 추출 중")
            to_wav(Path(media), wav)
        # 통째로 올리지 않고 쓸 조각만 읽는다 — 메모리가 적은 PC 에서 멈추지 않게.
        with WavFrames(wav) as audio:
            minutes = len(audio) / SAMPLE_RATE / 60
            progress(f"  {minutes:.0f}분 훑는 중 (성문 대조)")
            times, scores = scan(audio, vp, embedder, hop=hop, progress=progress)
    return find_service(times, scores, **kwargs)
