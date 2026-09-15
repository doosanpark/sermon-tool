"""설교 + 축도 구간 검출.

전제(명세 2장):
  - 목사님 = 전체에서 가장 오래 발화한 화자
  - 설교 = 목사님이 가장 오래 연속 발화하는 단일 블록 (봉독 포함)
  - 축도 = 설교 이후, 앞에 긴 공백(찬양/헌금)을 두고 영상 끝쪽에 있는 1~2분짜리 목사님 블록

v2 명세는 축도를 '설교로 오인되면 안 되는 것'으로 배제했지만, 여기서는 축도도
추출 대상이므로 별도 규칙으로 함께 찾아낸다.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .config import Config
from .diarize import Segment
from .util import ToolError, hms


@dataclass
class Block:
    start: float
    end: float
    speaker: str

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Detection:
    pastor: str
    pastor_ratio: float
    sermon: Block
    benediction: Block | None
    confidence: float
    bene_confidence: float
    reasons: list[str]
    blocks: list[Block]


def speaker_totals(segments: list[Segment]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for s in segments:
        totals[s.speaker] += s.duration
    return dict(totals)


def merge_blocks(segments: list[Segment], speaker: str, gap: float) -> list[Block]:
    """같은 화자의 구간을 gap 이하 간격이면 하나로 잇는다."""
    picked = sorted((s for s in segments if s.speaker == speaker), key=lambda s: s.start)
    blocks: list[Block] = []
    for s in picked:
        if blocks and s.start - blocks[-1].end <= gap:
            blocks[-1].end = max(blocks[-1].end, s.end)
        else:
            blocks.append(Block(s.start, s.end, speaker))
    return blocks


def _sermon_confidence(blocks: list[Block], sermon: Block, pastor_ratio: float,
                       cfg: Config) -> tuple[float, list[str]]:
    others = [b for b in blocks if b is not sermon]
    second = max((b.duration for b in others), default=1.0)
    ratio = sermon.duration / max(second, 1.0)

    score = 0.0
    score += 0.4 if ratio > 5 else 0.4 * (ratio / 5)
    in_range = cfg.sermon_min <= sermon.duration <= cfg.sermon_max
    score += 0.4 if in_range else 0.0
    score += 0.2 * min(pastor_ratio / 0.5, 1.0)

    reasons = [
        f"최장블록/2위 = {ratio:.1f}배",
        f"길이 {sermon.duration/60:.1f}분 "
        + ("정상범위" if in_range else f"정상범위({cfg.sermon_min/60:.0f}~{cfg.sermon_max/60:.0f}분) 벗어남"),
        f"목사님 발화 점유율 {pastor_ratio*100:.0f}%",
    ]
    return round(min(score, 1.0), 2), reasons


def _find_benediction(blocks: list[Block], sermon: Block, duration: float,
                      cfg: Config) -> tuple[Block | None, float, list[str]]:
    """설교 이후 목사님 블록 중 축도를 고른다."""
    tail = [b for b in blocks if b.start >= sermon.end]
    if not tail:
        return None, 0.0, ["설교 이후 목사님 발화 블록이 없음 — 축도 검출 실패"]

    tail.sort(key=lambda b: b.start)
    cutoff = duration * cfg.bene_tail_ratio if duration > 0 else 0.0

    def lead_gap(b: Block) -> float:
        prev_end = sermon.end
        for other in tail:
            if other.end <= b.start:
                prev_end = max(prev_end, other.end)
        return b.start - prev_end

    scored: list[tuple[float, Block, list[str]]] = []
    for b in tail:
        gap = lead_gap(b)
        reasons: list[str] = []
        score = 0.0

        if cfg.bene_min <= b.duration <= cfg.bene_max:
            score += 0.4
            reasons.append(f"길이 {b.duration/60:.1f}분 축도 범위")
        else:
            reasons.append(f"길이 {b.duration/60:.1f}분 축도 범위 벗어남")

        if duration > 0 and b.start >= cutoff:
            score += 0.3
            reasons.append(f"영상 뒤쪽 {b.start/duration*100:.0f}% 지점")
        else:
            reasons.append("영상 뒤쪽이 아님")

        if gap >= cfg.bene_lead_gap:
            score += 0.3
            reasons.append(f"앞 공백 {gap/60:.1f}분")
        else:
            score += 0.3 * (gap / cfg.bene_lead_gap)
            reasons.append(f"앞 공백 {gap/60:.1f}분 (짧음)")

        scored.append((round(min(score, 1.0), 2), b, reasons))

    # 점수가 같으면 더 뒤에 있는 블록을 택한다 (축도는 예배 마지막 순서다)
    scored.sort(key=lambda x: (x[0], x[1].start))
    best_score, best_block, best_reasons = scored[-1]
    return best_block, best_score, best_reasons


def detect(segments: list[Segment], duration: float, cfg: Config) -> Detection:
    if not segments:
        raise ToolError("화자 분리 결과가 비어 있습니다.")

    totals = speaker_totals(segments)
    pastor = max(totals, key=lambda k: totals[k])
    speech_total = sum(totals.values()) or 1.0
    pastor_ratio = totals[pastor] / speech_total

    blocks = merge_blocks(segments, pastor, cfg.gap_threshold)
    if not blocks:
        raise ToolError("목사님 화자의 발화 블록을 만들지 못했습니다.")

    sermon = max(blocks, key=lambda b: b.duration)
    conf, reasons = _sermon_confidence(blocks, sermon, pastor_ratio, cfg)

    bene, bene_conf, bene_reasons = _find_benediction(blocks, sermon, duration, cfg)
    reasons.append(f"목사님 화자 = {pastor} (총 {totals[pastor]/60:.1f}분)")
    reasons.extend(f"[축도] {r}" for r in bene_reasons)

    return Detection(
        pastor=pastor,
        pastor_ratio=pastor_ratio,
        sermon=sermon,
        benediction=bene,
        confidence=conf,
        bene_confidence=bene_conf,
        reasons=reasons,
        blocks=sorted(blocks, key=lambda b: b.start),
    )


def format_detection(det: Detection) -> str:
    lines = [
        f"목사님 화자   : {det.pastor} (점유율 {det.pastor_ratio*100:.0f}%)",
        f"설교        : {hms(det.sermon.start)} ~ {hms(det.sermon.end)}"
        f"  ({det.sermon.duration/60:.1f}분)  신뢰도 {det.confidence}",
    ]
    if det.benediction:
        b = det.benediction
        lines.append(
            f"축도        : {hms(b.start)} ~ {hms(b.end)}"
            f"  ({b.duration/60:.1f}분)  신뢰도 {det.bene_confidence}"
        )
    else:
        lines.append("축도        : 검출 실패")
    lines.append("근거        : " + " / ".join(det.reasons))
    return "\n".join(lines)


def apply_service_result(marker, result, cfg: Config) -> None:
    """성문 검출 결과(ServiceResult)를 마커에 채워 넣는다.

    설교와 축도를 각각 구간으로 넣으므로, 사이의 찬양·헌금은 자동으로 빠진다.
    """
    marker.sermon_ai_start, marker.sermon_ai_end = (result.sermon or (None, None))
    if cfg.include_benediction and result.benediction:
        marker.bene_ai_start, marker.bene_ai_end = result.benediction
    else:
        marker.bene_ai_start = marker.bene_ai_end = None
    marker.confidence = result.confidence
    marker.bene_confidence = result.confidence if result.benediction else 0.0
    marker.reasons = list(result.reasons)
    marker.pastor_speaker = "voiceprint"
    marker.clips = []
    marker.sync_clips_from_detection()
