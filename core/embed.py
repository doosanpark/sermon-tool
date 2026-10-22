"""문장 임베딩 — 어느 문장이 설교 전체를 대표하는가.

규칙 점수(highlight.score_marks)는 낱말만 본다. 되풀이되는 말이 들어 있으면
점수를 준다. 그래서 실측에서 "니에미아 1장 오늘 본문 5절부터 11절까지 말씀입니다"
같은 안내 문장이 높게 나오고, 정작 설교의 명제인 "그의 기도를 보면 그의 삶의
구조가 보여요" 는 낮게 나왔다.

임베딩은 뜻을 본다. 설교 전체의 평균 방향에 가까운 문장이 곧 그 설교를
대표하는 문장이다. 성문 검출이 무거운 화자 분리 대신 작은 ONNX 임베딩과
코사인 유사도로 60배 빨라진 것과 **같은 수법**을, 소리가 아니라 글에 쓴다.

  - torch 를 쓰지 않는다. onnxruntime 과 tokenizers 는 이미 설치본에 들어 있다.
  - 모델(약 120MB)은 faster-whisper 와 같이 첫 사용 때 내려받는다.
  - 없으면 조용히 건너뛴다. 규칙 점수만으로도 프로그램은 그대로 돈다.
"""
from __future__ import annotations

import gc
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .util import PROJECT_ROOT, ToolError, model_search_dirs

# Transformers.js 용으로 ONNX 로 내보내 둔 다국어 임베딩. 한국어가 들어 있고
# 양자화본이 120MB 라 설치본 옆에 두기에 부담이 없다.
REPO = "Xenova/multilingual-e5-small"
ONNX_FILE = "onnx/model_quantized.onnx"
TOKENIZER_FILE = "tokenizer.json"
MAX_TOKENS = 192          # 설교 한 문장은 길어야 이 안에 들어온다
# e5 계열은 문장 앞에 무엇으로 쓰는지 붙여 줘야 제 성능이 난다.
PREFIX = "query: "


def model_dirs(cfg) -> list[Path]:
    """모델을 찾아볼 곳들. 맨 앞이 받아서 넣는 곳이다."""
    dirs = [PROJECT_ROOT / "bin" / "models" / "text-embed"]
    dirs += model_search_dirs(cfg.work_dir, "text-embed")
    return dirs


def model_dir(cfg) -> Path:
    """이 모델이 있는 곳. 아무 데도 없으면 받아서 넣을 자리.

    소스 폴더에 넣어 둔 모델도 찾는다. 빌드본의 작업 폴더는 사용자 폴더라서,
    한 곳만 보면 모델이 PC 안에 있는데도 "아직 없음" 이 떴다 (2026-10-20).
    """
    for d in model_dirs(cfg):
        if (d / "model.onnx").exists() and (d / "tokenizer.json").exists():
            return d
    return Path(cfg.work_dir) / "models" / "text-embed"


def is_ready(cfg) -> bool:
    d = model_dir(cfg)
    return (d / "model.onnx").exists() and (d / "tokenizer.json").exists()


def download(cfg, progress=None) -> Path:
    """첫 사용 때 모델을 받아 둔다. faster-whisper 와 같은 방식이다."""
    d = model_dir(cfg)
    if is_ready(cfg):
        return d
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise ToolError(
            "모델을 내려받으려면 huggingface_hub 가 필요합니다.\n"
            "  pip install huggingface_hub") from exc

    d.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda t: print(f"  {t}"))
    say("문장 임베딩 모델을 처음 한 번만 내려받습니다 (약 120MB)")
    try:
        for remote, local in ((ONNX_FILE, "model.onnx"),
                              (TOKENIZER_FILE, "tokenizer.json")):
            say(f"  {remote} 받는 중...")
            got = hf_hub_download(repo_id=REPO, filename=remote)
            target = d / local
            target.write_bytes(Path(got).read_bytes())
    except Exception as exc:                      # 네트워크·권한 등 무엇이든
        raise ToolError(
            "문장 임베딩 모델을 내려받지 못했습니다.\n"
            f"  {exc}\n\n"
            "인터넷이 막혀 있으면 다른 PC에서 아래 두 파일을 받아\n"
            f"  {d}\n"
            "에 넣어 주세요. 넣지 않아도 3분 요약은 규칙만으로 돌아갑니다.\n"
            f"  https://huggingface.co/{REPO}/blob/main/{ONNX_FILE}  → model.onnx\n"
            f"  https://huggingface.co/{REPO}/blob/main/{TOKENIZER_FILE}  → tokenizer.json"
        ) from exc
    say("  받았습니다")
    return d


@dataclass
class _Loaded:
    session: object
    tokenizer: object
    inputs: list[str]


class SentenceEmbedder:
    """문장 여러 개를 한 번에 벡터로 바꾼다. CPU 전용."""

    _cache: dict[str, _Loaded] = {}

    def __init__(self, cfg, threads: int = 2):
        self.dir = model_dir(cfg)
        key = str(self.dir)
        if key in self._cache:
            self._loaded = self._cache[key]
            return
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise ToolError(
                "문장 임베딩에는 onnxruntime 과 tokenizers 가 필요합니다.\n"
                "  pip install onnxruntime tokenizers") from exc

        if not is_ready(cfg):
            raise ToolError(f"임베딩 모델이 없습니다: {self.dir}")

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.log_severity_level = 3
        sess = ort.InferenceSession(str(self.dir / "model.onnx"), opts,
                                    providers=["CPUExecutionProvider"])
        tok = Tokenizer.from_file(str(self.dir / "tokenizer.json"))
        tok.enable_truncation(MAX_TOKENS)
        tok.enable_padding(length=None)
        self._loaded = _Loaded(sess, tok, [i.name for i in sess.get_inputs()])
        self._cache[key] = self._loaded

    def encode(self, texts: list[str], batch: int = 16) -> np.ndarray:
        """(문장 수, 차원) 배열. 길이 1 로 정규화해 돌려준다."""
        if not texts:
            return np.zeros((0, 384), dtype=np.float32)
        sess = self._loaded.session
        tok = self._loaded.tokenizer
        want = set(self._loaded.inputs)
        out: list[np.ndarray] = []

        for i in range(0, len(texts), batch):
            chunk = [PREFIX + (t or "").strip() for t in texts[i:i + batch]]
            enc = tok.encode_batch(chunk)
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in want:
                feed["token_type_ids"] = np.zeros_like(ids)
            feed = {k: v for k, v in feed.items() if k in want}

            hidden = sess.run(None, feed)[0]              # (b, 길이, 차원)
            m = mask[..., None].astype(np.float32)
            pooled = (hidden * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
            norm = np.linalg.norm(pooled, axis=1, keepdims=True)
            out.append(pooled / np.maximum(norm, 1e-9))
        return np.vstack(out).astype(np.float32)


# ------------------------------------------------------------------ 쓰임새
def importance(vecs: np.ndarray, drop_top: int = 1) -> np.ndarray:
    """그 설교에서 얼마나 중심에 있는 말인가.

    **평균 방향과의 유사도를 그냥 쓰면 안 된다.** 문장 벡터는 한 방향으로 크게
    쏠려 있어서(anisotropy) 코사인 값이 전부 0.85~0.94 사이에 몰린다. 그 상태로
    평균과 견주면 "중요한가" 가 아니라 "흔한가" 를 재게 된다.

    실측(2026-07-26 설교 551문장):
        평균 방향 유사도 상위  →  "그 말씀을 붙잡습니다" · "이 표현을 가지고 기도합니다"
                                 (짧은 상투구만 올라온다)
        쏠림 제거 후 상위      →  "기도가 어떻게 우리의 삶의 구조를 다시 세우는지"
                                 "기도가 삶의 제일 중요한 틀이에요"
                                 (설교의 명제가 올라온다)

    그래서 두 단계를 거친다.
      1) 평균을 빼고 제1주성분(가장 쏠린 방향)을 걷어낸다 — all-but-the-top
      2) 남은 방향에서 **다른 문장들과 얼마나 통하는지** 를 잰다 (degree centrality)
    되풀이해 말한 생각일수록 여러 문장과 통하므로 위로 올라온다.
    """
    n = len(vecs)
    if n < 4:
        return np.zeros(n, dtype=np.float32)

    X = np.asarray(vecs, dtype=np.float64)
    X = X - X.mean(axis=0)
    k = max(0, min(drop_top, min(X.shape) - 1))
    if k:
        # 상위 k 개 방향을 뺀다. 문장 수가 수백이라 SVD 가 순식간이다.
        _u, _s, vt = np.linalg.svd(X, full_matrices=False)
        for row in vt[:k]:
            X = X - np.outer(X @ row, row)
    norm = np.linalg.norm(X, axis=1, keepdims=True)
    X = X / np.maximum(norm, 1e-9)

    sim = X @ X.T
    np.fill_diagonal(sim, 0.0)
    return np.clip(sim, 0.0, None).mean(axis=1).astype(np.float32)


def release_model() -> None:
    """올려 둔 문장 임베딩 모델을 내려놓는다.

    onnxruntime 세션 하나가 300MB 안팎을 잡고 있다. 요약이 끝나면 다음
    요약까지 쓸 일이 없으니 놓아 준다. 다시 필요하면 저절로 다시 올라온다.
    """
    cache = SentenceEmbedder._cache
    if not cache:
        return
    cache.clear()
    gc.collect()
