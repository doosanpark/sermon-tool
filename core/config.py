"""파이프라인 설정값.

CPU 전용 환경을 기본으로 잡았다. GPU가 있으면 --device cuda 로 덮어쓴다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


from .util import IS_FROZEN, PROJECT_ROOT


def user_data_dir() -> Path:
    """설치본은 Program Files 아래에 쓸 수 없으므로 사용자 폴더를 쓴다."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "SermonTool"


def _default_work_dir() -> Path:
    env = os.environ.get("SERMON_TOOL_DIR")
    if env:
        return Path(env)
    return user_data_dir() / "data" if IS_FROZEN else PROJECT_ROOT / "data"


def read_hf_token() -> str | None:
    """HF_TOKEN 을 환경변수 → .env → .hf_token 순으로 찾는다.

    setx 는 새 터미널부터 적용되기 때문에 매번 걸리기 쉽다. 프로젝트 폴더에
    .hf_token 파일 하나만 만들어 두면 그 문제를 겪지 않는다.
    """
    env = os.environ.get("HF_TOKEN")
    if env and env.strip():
        return env.strip()

    for candidate in (user_data_dir() / ".hf_token", user_data_dir() / "hf_token.txt"):
        if candidate.exists():
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return value

    dotenv = PROJECT_ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("HF_TOKEN"):
                _, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if value:
                    return value

    plain = PROJECT_ROOT / ".hf_token"
    if plain.exists():
        value = plain.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


@dataclass
class Config:
    # --- 경로 ---
    work_dir: Path = field(default_factory=_default_work_dir)

    # --- 화자 분리 ---
    diarization_model: str = "pyannote/speaker-diarization-3.1"
    hf_token: str | None = field(default_factory=read_hf_token)
    device: str = "cpu"

    # --- 블록 병합 / 검출 ---
    gap_threshold: float = 90.0      # 목사님 발화 블록 병합 기준(초)
    sermon_min: float = 20 * 60      # 설교 정상 길이 하한
    sermon_max: float = 55 * 60      # 설교 정상 길이 상한
    bene_min: float = 25.0           # 축도 길이 하한
    bene_max: float = 300.0          # 축도 길이 상한
    bene_lead_gap: float = 90.0      # 축도 앞 공백(찬양/헌금) 최소 길이
    bene_tail_ratio: float = 0.80    # 축도는 영상 뒤쪽 20% 안에 있어야 함

    # --- 성문(voiceprint) 검출 ---
    use_voiceprint: bool = True        # 성문이 있으면 화자분리 대신 이걸 쓴다
    vp_threshold: float = 0.6          # 목사님으로 볼 유사도 기준
    vp_hop: float = 3.0                # 몇 초마다 판정할지
    include_benediction: bool = True   # 축도도 이어붙일지

    # --- STT ---
    whisper_model: str = "medium"    # CPU 기본. GPU면 large-v3 권장
    compute_type: str = "int8"       # CPU 기본. GPU면 float16
    language: str = "ko"
    beam_size: int = 5
    initial_prompt: str = "한국어 예배 설교입니다. 성경 본문과 기도, 축도가 포함됩니다."

    # --- 경계 보정 ---
    refine_window: float = 150.0     # 경계 앞뒤로 몇 초까지 뒤질지
    refine_pad: float = 1.0          # 찾은 문장 시작보다 살짝 앞에서 자르기

    # --- 음량 맞추기 ---
    # 교회 라이브 방송은 마이크 레벨이 낮게 잡혀 평균 -40dB 로 송출되는 경우가 있다.
    # 방송·유튜브 표준(-16 LUFS)에 맞춰 올려 준다. 영상은 그대로 두고 소리만 다시
    # 인코딩하므로 몇 십 초면 끝난다.
    normalize_audio: bool = True
    # 잡음 제거: "" (끔) / "light" / "strong"
    # 예배 방송의 웅웅거리는 정상 잡음(마이크 험·에어컨)에는 FFT 방식이 잘 듣는다.
    denoise: str = "light"
    target_lufs: float = -16.0
    target_peak_db: float = -1.5

    # --- 자막 넣기 ---
    # "file"  : .srt 파일만 따로 (유튜브에 올릴 때는 이게 낫다)
    # "embed" : 영상 안에 자막 트랙으로. 재생기에서 끄고 켤 수 있다. 빠르다.
    # "burn"  : 화면에 태워 넣는다. 어디서나 보이지만 끌 수 없고 재인코딩이 필요하다.
    subtitle_mode: str = "embed"   # embed | burn | file(CLI 전용)
    mp3_bitrate: str = "96k"       # 말소리는 모노 96k 로 충분하다
    mp3_mono: bool = True
    intro_path: str = ""           # 설교 앞에 붙일 시작 영상 (교회 로고 등)
    subtitle_font: str = "Malgun Gothic"
    subtitle_size: int = 22

    # --- 다시 인코딩할 때의 화질 (정확히 자르기 · 자막 태우기) ---
    # crf 는 낮을수록 좋고 파일이 커진다. preset 은 느릴수록 같은 crf 에서 조금 더 좋다.
    # 실측(720p 60초, 원본 대비 SSIM): crf20 veryfast 0.9948 → crf18 medium 0.9976.
    # 손실은 crf 20→18 에서 거의 다 줄고, 프리셋은 medium 까지가 남은 몫을 채운다.
    # 40분 설교 기준 12스레드 PC 에서 약 3~4분. 느린 PC 에서 너무 오래 걸리면
    # preset 을 "fast" 로 낮춘다 (SSIM 0.9974, 시간은 3/4).
    video_crf: int = 18
    video_preset: str = "medium"

    # --- 3분 요약 (세로 숏폼) ---
    # 목표 길이. 3분이 기본이고, 너무 잘게 끊긴다 싶으면 늘리면 된다.
    summary_target: float = 180.0
    # 세로 화면에서 목사님을 어디로 잡을지. 원본 가로/세로의 비율(0~1)이라
    # 720p 로 받히든 1080p 로 받히든 같은 구도가 나온다.
    # 고정 카메라라 한 번 맞춰 두면 계속 쓴다 — [고급 설정] 에서 미리보기로 맞춘다.
    shorts_center_x: float = 0.44   # 목사님 가로 위치 (실측: 화면의 40~45% 지점)
    shorts_center_y: float = 0.61   # 세로 위치. 머리 위 여백이 남게 잡은 값
    # 0.515 는 목사님이 너무 크게 잡혀 720p 원본에서 351x370 을 1140 으로 3.1배 키웠다.
    # 1.2배 넓힌 0.618 은 422x445 를 2.6배만 키우므로 덜 뭉개진다 (2026-09-15).
    shorts_zoom: float = 0.800      # 원본 높이의 몇 배를 담을지. 작을수록 크게
    shorts_sub_size: int = 62       # 세로 화면 자막 크기 (1080x1920 픽셀 그대로)
    shorts_crf: int = 18
    shorts_preset: str = "medium"    # 3분짜리라 medium 이어도 몇 분이면 끝난다
    # 확대 화질. 720p 원본에서 350x370 쯤을 잘라 1080x1140 으로 약 3배 키우는데,
    # 그냥 키우면 압축 블록까지 3배로 커진다. 작을 때 블록을 살짝 뭉개고(hqdn3d)
    # → lanczos 로 키우고 → 키운 뒤 윤곽만 되살린다(cas). 비우거나 0 이면 그 단계를 건너뛴다.
    shorts_denoise: str = "hqdn3d=2:1.5:4:3"
    shorts_scaler: str = "lanczos"
    shorts_sharpen: float = 0.5       # cas 세기 0~1. 0.7 넘으면 링잉이 보이기 시작한다

    # --- 요약 문장 고르기: 임베딩 + 로컬 LLM ---
    # 둘 다 없어도 3분 요약은 규칙만으로 돌아간다. 있으면 더 잘 고른다.
    #
    #   규칙        낱말만 본다. 되풀이되는 말이 있으면 점수를 준다.
    #   임베딩      뜻을 본다. 설교 전체를 대표하는 문장을 잡는다. 120MB, 몇 초.
    #   로컬 LLM    문맥을 본다. 좁혀진 후보 안에서 고른다. 2GB, 1~3분.
    #
    # 이 순서가 DIAMOND(arXiv:2506.02351) 의 Preparation → Decision 구조다.
    use_embedding: bool = True       # 모델이 없으면 알아서 건너뛴다
    embed_weight: float = 1.6        # 규칙 점수에 대표성을 얼마나 섞을지

    use_llm: bool = False            # 2GB 를 받아야 하므로 기본은 꺼 둔다
    # 다른 모델을 쓰려면 이 두 줄만 바꾸면 된다. 한국어가 되는 4B 안팎이면 된다.
    llm_model_repo: str = "Qwen/Qwen3-4B-GGUF"
    llm_model_file: str = "Qwen3-4B-Q4_K_M.gguf"
    llm_context: int = 8192          # 후보만 넘기므로 이만하면 넉넉하다
    llm_threads: int = 4
    llm_url: str = ""                # 비워 두면 bin/llama-server 를 직접 띄운다
    llm_candidates: int = 90         # LLM 에게 넘길 후보 문장 수 (입력을 1/6 로 줄인다)

    # --- 내보내기 ---
    head_pad: float = 1.5            # 각 구간 시작 여유
    tail_pad: float = 1.5            # 각 구간 끝 여유
    make_srt: bool = True

    # ---------- 경로 헬퍼 ----------
    def ensure_dirs(self) -> None:
        for d in (self.audio_dir, self.cache_dir, self.marker_dir,
                  self.export_dir, self.preview_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def audio_dir(self) -> Path:
        return self.work_dir / "audio"

    @property
    def cache_dir(self) -> Path:
        return self.work_dir / "cache"

    @property
    def marker_dir(self) -> Path:
        return self.work_dir / "markers"

    @property
    def export_dir(self) -> Path:
        return self.work_dir / "export"

    @property
    def preview_dir(self) -> Path:
        return self.work_dir / "preview"

    def preview_path(self, video_id: str) -> Path:
        return self.preview_dir / f"{video_id}.mp4"

    def audio_path(self, video_id: str) -> Path:
        return self.audio_dir / f"{video_id}.wav"

    def diar_path(self, video_id: str) -> Path:
        return self.cache_dir / f"{video_id}.diar.json"

    def transcript_path(self, video_id: str) -> Path:
        return self.cache_dir / f"{video_id}.transcript.json"

    @property
    def voiceprint_path(self) -> Path:
        """사용자가 만든 성문이 우선, 없으면 설치본에 함께 묶인 기본 성문."""
        mine = self.work_dir / "voiceprint.npz"
        if mine.exists():
            return mine
        bundled = PROJECT_ROOT / "voiceprint.npz"
        return bundled if bundled.exists() else mine

    def marker_path(self, video_id: str) -> Path:
        return self.marker_dir / f"{video_id}.json"
