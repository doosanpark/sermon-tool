#!/usr/bin/env python3
"""0단계 — 설계 전제 검증용 스크립트.

코드를 더 쓰기 전에 반드시 이것부터 돌린다. 확인할 것은 두 가지다.
  1) 목사님이 최다 화자로 잡히는가
  2) 최장 블록이 실제 설교 시각과 일치하고, 그 뒤 짧은 블록이 축도인가

사용법:
    python check_speakers.py <wav 파일 또는 유튜브 URL>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import fetch
from core.config import Config
from core.detect import detect, format_detection, merge_blocks, speaker_totals
from core.diarize import diarize
from core.util import ToolError, ffprobe_duration, hms


def main() -> int:
    ap = argparse.ArgumentParser(description="화자 분리 전제 검증")
    ap.add_argument("source", help="로컬 wav 경로 또는 유튜브 URL")
    ap.add_argument("--gap", type=float, default=90.0, help="블록 병합 간격(초). 기본 90")
    ap.add_argument("--top", type=int, default=5, help="출력할 최장 블록 개수")
    ap.add_argument("--force", action="store_true", help="화자 분리 캐시 무시")
    args = ap.parse_args()

    cfg = Config(gap_threshold=args.gap)
    cfg.ensure_dirs()

    src = args.source
    if src.startswith("http"):
        meta = fetch.info(src)
        vid = meta["id"]
        wav = cfg.audio_path(vid)
        print(f"[1/3] 오디오 다운로드: {meta['title']}")
        fetch.download_audio(src, wav)
        duration = meta["duration"] or ffprobe_duration(wav)
        cache = cfg.diar_path(vid)
    else:
        wav = Path(src)
        if not wav.exists():
            print(f"파일이 없습니다: {wav}", file=sys.stderr)
            return 1
        duration = ffprobe_duration(wav)
        cache = cfg.cache_dir / f"{wav.stem}.diar.json"

    print("[2/3] 화자 분리 (CPU에서는 1시간 영상당 20~40분 걸릴 수 있습니다)")
    segments = diarize(wav, cfg, cache_path=cache, force=args.force)

    totals = speaker_totals(segments)
    print("\n=== 화자별 총 발화시간 ===")
    for spk, sec in sorted(totals.items(), key=lambda x: -x[1]):
        print(f"{spk}: {sec/60:.1f}분")

    top_spk = max(totals, key=lambda k: totals[k])
    blocks = merge_blocks(segments, top_spk, args.gap)
    blocks_by_len = sorted(blocks, key=lambda b: -b.duration)
    print(f"\n=== {top_spk} 최장 블록 TOP {args.top} ===")
    for b in blocks_by_len[: args.top]:
        print(f"{hms(b.start)} ~ {hms(b.end)}  ({b.duration/60:.1f}분)")

    print("\n[3/3] 자동 판정 결과")
    det = detect(segments, duration, cfg)
    print(format_detection(det))
    print(
        "\n위 설교/축도 시각이 실제 영상과 맞으면 전제가 검증된 것입니다.\n"
        "맞지 않으면 --gap 값을 조정하거나(기본 90초) 명세 12장의 보조신호를 검토하세요."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ToolError as exc:
        print(f"\n[오류] {exc}", file=sys.stderr)
        raise SystemExit(2)
