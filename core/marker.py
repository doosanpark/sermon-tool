"""마커 데이터 모델 + JSON 입출력.

AI 예측(ai_*)과 사람 수정본(human_*)을 함께 남긴다. 나중에 학습 데이터로 쓰기
위한 것이므로 필드는 처음부터 고정해 둔다. 내보내기는 human_*이 있으면 그 값을,
없으면 ai_*를 쓴다.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 3  # v1: 설교만 / v2: 설교 + 축도 / v3: 3분 요약 문장 선택


@dataclass
class Marker:
    video_id: str
    url: str = ""
    title: str = ""
    upload_date: str = ""
    duration: float = 0.0

    # AI 예측
    sermon_ai_start: float | None = None
    sermon_ai_end: float | None = None
    bene_ai_start: float | None = None
    bene_ai_end: float | None = None
    confidence: float = 0.0
    bene_confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)

    # 사람 수정본
    sermon_human_start: float | None = None
    sermon_human_end: float | None = None
    bene_human_start: float | None = None
    bene_human_end: float | None = None
    corrected: bool = False

    # 설교 제목 — 사람이 적는다. 내보낼 파일 이름에 쓰인다.
    sermon_title: str = ""

    # 편집 구간 — 사람이 GUI에서 추가/삭제한 구간 목록.
    # 비어 있지 않으면 sermon/bene 대신 이 목록을 내보낸다.
    clips: list[dict] = field(default_factory=list)

    # 3분 요약에 넣기로 고른 문장들 — 그 문장의 시작 시각만 적는다.
    #
    # clips 와 절대 섞지 않는다. ranges() 는 clips 가 비어 있지 않으면 그걸
    # 그대로 돌려주고 export 가 그걸로 설교 영상을 만들기 때문에, 요약 조각을
    # clips 에 넣으면 [설교 영상 만들기] 가 조각 열댓 개를 이어붙인 영상을 뱉는다.
    #
    # 인덱스가 아니라 시각으로 적는 이유: 자막을 다시 만들면 문장이 나뉘는
    # 자리가 달라져 인덱스가 어긋난다. 시각은 그래도 거의 그대로다.
    summary_picks: list[float] = field(default_factory=list)
    summary_target: float = 180.0
    # 요약을 어떤 형식으로 골랐는지 (three | one | sentences). 비어 있으면 예전 파일.
    summary_format: str = ""

    # 메타
    pastor_speaker: str = ""
    pastor_ratio: float = 0.0
    refined: bool = False
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    # ---------- 확정 구간 ----------
    def sermon_range(self) -> tuple[float, float] | None:
        s = self.sermon_human_start if self.sermon_human_start is not None else self.sermon_ai_start
        e = self.sermon_human_end if self.sermon_human_end is not None else self.sermon_ai_end
        if s is None or e is None or e <= s:
            return None
        return (float(s), float(e))

    def bene_range(self) -> tuple[float, float] | None:
        s = self.bene_human_start if self.bene_human_start is not None else self.bene_ai_start
        e = self.bene_human_end if self.bene_human_end is not None else self.bene_ai_end
        if s is None or e is None or e <= s:
            return None
        return (float(s), float(e))

    def ranges(self) -> list[tuple[float, float, str]]:
        """내보낼 구간 목록.

        clips 가 있으면 그 순서 그대로(사람이 정렬한 순서를 존중), 없으면
        설교·축도를 시간 순으로 돌려준다.
        """
        if self.clips:
            out = []
            for c in self.clips:
                start, end = float(c["start"]), float(c["end"])
                if end > start:
                    out.append((start, end, str(c.get("label", "clip"))))
            return out

        out: list[tuple[float, float, str]] = []
        r = self.sermon_range()
        if r:
            out.append((r[0], r[1], "sermon"))
        r = self.bene_range()
        if r:
            out.append((r[0], r[1], "benediction"))
        out.sort(key=lambda x: x[0])
        return out

    # ---------- 직렬화 ----------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ---------- 3분 요약 ----------
    def summary_chosen(self, starts: list[float], tol: float = 0.3) -> list[bool]:
        """전사문 문장 시작 시각 목록을 받아, 각 문장이 골라졌는지 돌려준다."""
        picks = sorted(self.summary_picks)
        out: list[bool] = []
        for t in starts:
            out.append(any(abs(t - p) <= tol for p in picks))
        return out

    def set_summary_picks(self, starts: list[float]) -> None:
        self.summary_picks = [round(float(t), 3) for t in sorted(starts)]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Marker":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path) -> Path:
        # 옛 파일을 읽으면 그 안에 적힌 판 번호가 그대로 남는다. 저장하는 순간
        # 형식은 지금 판이므로 여기서 다시 찍는다 — 안 그러면 요약이 든 파일이
        # "판 2" 라고 우기게 된다.
        self.schema_version = SCHEMA_VERSION
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def sync_clips_from_detection(self) -> None:
        """clips 가 비어 있으면 설교·축도로 초기 목록을 만든다."""
        if self.clips:
            return
        self.clips = [
            {"label": label, "start": start, "end": end}
            for start, end, label in self.ranges()
        ]

    @classmethod
    def load(cls, path: Path) -> "Marker":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
