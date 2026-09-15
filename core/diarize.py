"""pyannote.audio 화자 분리 + 결과 캐시.

pyannote 모델은 HuggingFace 토큰이 필요하다(무료).
  1) https://huggingface.co/pyannote/speaker-diarization-3.1 에서 라이선스 동의
  2) https://huggingface.co/settings/tokens 에서 read 토큰 발급
  3) 환경변수 HF_TOKEN 에 등록
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .util import ToolError


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    speaker: str

    @property
    def duration(self) -> float:
        return self.end - self.start



GATED_REPOS = [
    "https://huggingface.co/pyannote/segmentation-3.0",
    "https://huggingface.co/pyannote/speaker-diarization-3.1",
    "https://huggingface.co/pyannote/speaker-diarization-community-1",
]


def _wrap_hub_error(exc: Exception, cfg: Config) -> Exception:
    """HuggingFace 게이트/권한 에러를 사람이 읽을 수 있는 안내로 바꾼다."""
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    gated = any(k in lowered for k in ("gated", "403", "restricted", "authorized list", "401"))
    if not gated:
        return exc

    missing = ""
    for url in GATED_REPOS:
        name = url.split("huggingface.co/")[-1]
        if name in text:
            missing = f"\n지금 막힌 저장소: {url}\n"

    return ToolError(
        "HuggingFace 모델 접근이 거부됐습니다(gated repo).\n"
        + missing
        + "\npyannote 4.x 는 아래 세 저장소 모두에 라이선스 동의가 필요합니다.\n"
        + "".join(f"  - {u}\n" for u in GATED_REPOS)
        + "\n각 페이지에서 동의 버튼을 누르면 대개 즉시 승인됩니다.\n"
        "동의 후에도 계속 막히면 토큰을 다시 발급해 .hf_token 에 넣어보세요.\n"
        "\n동의를 더 늘리고 싶지 않다면 3.x 계열로 내려도 됩니다:\n"
        "  pip install \"pyannote.audio<4\""
    )





def _to_annotation(result):
    """pyannote 버전별 반환 타입을 Annotation 으로 통일한다.

    3.x 는 Annotation 을 바로 주지만, 4.x 는 DiarizeOutput 안에 담아서 준다.
    """
    if hasattr(result, "itertracks"):
        return result

    for attr in ("speaker_diarization", "diarization", "annotation", "output"):
        obj = getattr(result, attr, None)
        if obj is not None and hasattr(obj, "itertracks"):
            return obj

    # namedtuple / tuple 형태면 필드를 훑는다
    for obj in getattr(result, "_asdict", lambda: {})().values():
        if hasattr(obj, "itertracks"):
            return obj
    if isinstance(result, (tuple, list)):
        for obj in result:
            if hasattr(obj, "itertracks"):
                return obj

    fields = [a for a in dir(result) if not a.startswith("_")]
    raise ToolError(
        f"화자 분리 결과에서 Annotation 을 찾지 못했습니다.\n"
        f"  반환 타입: {type(result).__name__}\n"
        f"  가진 속성: {', '.join(fields[:20])}"
    )


def _run_pipeline(pipe, source):
    """가능하면 진행률 훅을 붙여 실행한다.

    화자 분리는 CPU에서 수십 분이 걸리는데 아무 출력이 없으면 멈춘 것처럼 보인다.
    pyannote 의 ProgressHook 이 있으면 단계별 진행률을 표시한다.
    """
    try:
        from pyannote.audio.pipelines.utils.hook import ProgressHook
    except Exception:
        return pipe(source)

    try:
        with ProgressHook() as hook:
            return pipe(source, hook=hook)
    except TypeError:
        return pipe(source)


def _load_waveform(wav_path: Path):
    """16kHz 모노 WAV를 직접 읽어 pyannote 에 텐서로 넘긴다.

    pyannote 4.x 는 파일 경로를 주면 torchcodec 으로 디코딩하는데, 이게 윈도우에서
    FFmpeg 공유 DLL(full-shared 빌드)을 요구한다. 우리는 이미 PCM 16bit WAV를
    만들어 두었으므로 표준 라이브러리로 읽어 넘기면 그 의존성을 통째로 건너뛴다.
    """
    import wave

    import numpy as np
    import torch

    with wave.open(str(wav_path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if width != 2:
        raise ValueError(f"지원하지 않는 샘플 폭: {width*8}bit (16bit PCM 이어야 함)")

    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).T
        data = data.mean(axis=0, keepdims=True)
    else:
        data = data.reshape(1, -1)

    return {"waveform": torch.from_numpy(np.ascontiguousarray(data)), "sample_rate": rate}


def load_cache(path: Path) -> list[Segment] | None:
    path = Path(path)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Segment(float(s["start"]), float(s["end"]), str(s["speaker"])) for s in raw]


def save_cache(path: Path, segments: list[Segment]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [{"start": s.start, "end": s.end, "speaker": s.speaker} for s in segments],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def diarize(wav_path: Path, cfg: Config, cache_path: Path | None = None,
            force: bool = False) -> list[Segment]:
    """화자 구간 리스트를 돌려준다. 캐시가 있으면 재사용한다(CPU에서 가장 오래 걸리는 단계)."""
    if cache_path and not force:
        cached = load_cache(cache_path)
        if cached:
            print(f"  캐시 사용 — 화자 구간 {len(cached)}개를 즉시 불러왔습니다.")
            return cached

    print(
        "  캐시 없음. 이 영상은 처음이라 화자 분리를 처음부터 돌립니다.\n"
        "  CPU 에서는 1시간 영상당 20~40분 걸립니다. 창을 닫지 말고 기다리세요.\n"
        "  한 번 끝나면 결과가 저장되어 다음부터는 몇 초면 끝납니다."
    )

    try:
        import torch
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise ToolError(
            "pyannote.audio / torch 가 설치되어 있지 않습니다.\n"
            "  pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu\n"
            "  pip install pyannote.audio"
        ) from exc

    if not cfg.hf_token:
        raise ToolError(
            "HuggingFace 토큰을 찾지 못했습니다. 아래 중 하나로 넣으세요.\n"
            "  1) 이 폴더에 .hf_token 파일을 만들고 토큰만 한 줄 저장  ← 가장 확실함\n"
            "  2) 지금 이 터미널에서만:  set HF_TOKEN=hf_xxxxxxxx\n"
            '  3) 영구 등록:  setx HF_TOKEN "hf_xxxxxxxx"  (새 터미널부터 적용됨)\n'
            "\n모델 라이선스 동의도 두 곳 모두 필요합니다:\n"
            "  https://huggingface.co/pyannote/segmentation-3.0\n"
            "  https://huggingface.co/pyannote/speaker-diarization-3.1"
        )

    # pyannote.audio 4.x 는 token=, 3.x 는 use_auth_token= 을 쓴다
    pipe = None
    for kwarg in ("token", "use_auth_token"):
        try:
            pipe = Pipeline.from_pretrained(cfg.diarization_model, **{kwarg: cfg.hf_token})
            break
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise _wrap_hub_error(exc, cfg)
            continue
        except Exception as exc:
            raise _wrap_hub_error(exc, cfg)

    if pipe is None:
        raise ToolError(
            "pyannote 파이프라인을 불러오지 못했습니다. 모델 페이지에서 라이선스 동의를 했는지 확인하세요."
        )
    if cfg.device != "cpu" and torch.cuda.is_available():
        pipe.to(torch.device(cfg.device))

    try:
        source = _load_waveform(wav_path)
    except Exception as exc:  # WAV가 아니거나 메모리 부족 — 파일 경로로 폴백
        print(f"  (직접 로딩 실패: {exc} — 파일 경로로 넘깁니다)")
        source = str(wav_path)

    try:
        annotation = _run_pipeline(pipe, source)
    except RuntimeError as exc:
        if "libtorchcodec" in str(exc):
            raise ToolError(
                "torchcodec 이 오디오를 디코딩하지 못했습니다.\n"
                "보통 FFmpeg 정적(essentials) 빌드만 설치했을 때 납니다. 둘 중 하나로 해결하세요.\n"
                "  1) FFmpeg 'full-shared' 빌드를 받아 bin 폴더를 PATH에 등록\n"
                "     https://www.gyan.dev/ffmpeg/builds/ → ffmpeg-release-full-shared.7z\n"
                "  2) 입력을 16bit PCM WAV로 맞추기 (이 도구가 만드는 wav는 원래 그 형식입니다)"
            ) from exc
        raise
    annotation = _to_annotation(annotation)
    segments = [
        Segment(float(turn.start), float(turn.end), str(spk))
        for turn, _, spk in annotation.itertracks(yield_label=True)
    ]
    segments.sort(key=lambda s: s.start)

    if cache_path:
        save_cache(cache_path, segments)
        print(f"  화자 분리 완료 — 구간 {len(segments)}개를 캐시에 저장했습니다.")
    return segments
