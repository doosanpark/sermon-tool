#!/usr/bin/env python3
"""3분 요약을 더 잘 고르게 하는 두 가지를 받고, 정말 나아지는지 보여 준다.

    python setup_ai.py              둘 다 (임베딩 120MB + AI 2GB)
    python setup_ai.py --embed      임베딩만 (120MB, 빠름)
    python setup_ai.py --check      이미 받은 것으로 확인만

받지 않아도 3분 요약은 규칙만으로 돌아간다. 이건 '더 잘 고르게' 하는 것이다.

  규칙      낱말만 본다. 되풀이되는 말이 있으면 점수를 준다.
  임베딩    뜻을 본다. 설교 전체를 대표하는 문장을 잡는다.  120MB · 몇 초
  AI        문맥을 본다. 좁혀진 후보 안에서 고른다.        2GB · 1~3분
"""
from __future__ import annotations

import argparse

from core import highlight as hl
from core import pauses as pause_mod
from core import transcribe
from core.config import Config
from core.marker import Marker
from core.util import ToolError


def newest_sermon(cfg: Config):
    """자막까지 만들어 둔 영상 중 가장 최근 것."""
    best = None
    for path in sorted(cfg.marker_dir.glob("*.json")):
        if not cfg.transcript_path(path.stem).exists():
            continue
        marker = Marker.load(path)
        if marker.sermon_range():
            best = marker
    return best


def show(title: str, marks, picked: set[int], limit: int = 6) -> None:
    print(f"\n  [{title}] 고른 문장 {len(picked)}개 중 앞 {limit}개")
    for i in sorted(picked)[:limit]:
        print(f"     {marks[i].text[:62]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed", action="store_true", help="임베딩만 받는다")
    ap.add_argument("--check", action="store_true", help="받지 않고 확인만 한다")
    args = ap.parse_args()

    cfg = Config()
    cfg.ensure_dirs()

    # ---------- 내려받기 ----------
    if not args.check:
        from core import embed
        try:
            embed.download(cfg)
        except ToolError as exc:
            print(f"\n{exc}\n")
            return 1

        if not args.embed:
            from core import llm
            missing = llm.why_not(cfg)
            if "llama-server" in missing:
                print("\nAI 고르기에는 실행파일이 하나 더 필요합니다.")
                print(missing)
                print("\n임베딩만으로도 규칙보다 낫습니다. 실행파일을 넣은 뒤 다시 돌리셔도 됩니다.\n")
            else:
                try:
                    llm.download(cfg)
                except ToolError as exc:
                    print(f"\n{exc}\n")

    # ---------- 정말 나아지는지 보여 주기 ----------
    marker = newest_sermon(cfg)
    if not marker:
        print("\n자막까지 만들어 둔 영상이 없어 비교는 건너뜁니다.")
        print("영상을 하나 처리한 뒤 python setup_ai.py --check 로 확인해 보세요.")
        return 0

    vid = marker.video_id
    tr = transcribe.load_transcript(cfg.transcript_path(vid))
    lo, hi = marker.sermon_range()
    wav = cfg.audio_path(vid)
    found = pause_mod.find_pauses(wav, lo, hi) if wav.exists() else []
    snap = pause_mod.make_snap(found) if found else None
    target = cfg.summary_target

    print(f"\n{'=' * 60}")
    print(f"  {vid} · 설교 {(hi - lo) / 60:.1f}분 으로 견줘 봅니다")
    print("=" * 60)

    marks_a, ol = hl.plan(tr, lo, hi, target, snap=snap)
    pick_a = {m.index for m in marks_a if m.chosen}
    print(f"\n규칙만        문장 {len(pick_a)}개 · "
          f"{hl.total_seconds(marks_a, snap):.0f}초")
    show("규칙만", marks_a, pick_a)

    vectors_of, chooser, notes = hl.make_helpers(cfg, log=lambda t: print(f"  {t}"))
    for note in notes:
        print(f"  {note}")

    try:
        if vectors_of:
            marks_b, _ = hl.plan(tr, lo, hi, target, snap=snap,
                                 vectors_of=vectors_of, weight=cfg.embed_weight)
            pick_b = {m.index for m in marks_b if m.chosen}
            print(f"\n＋임베딩      문장 {len(pick_b)}개 · "
                  f"{hl.total_seconds(marks_b, snap):.0f}초 · "
                  f"규칙과 다른 문장 {len(pick_a ^ pick_b)}개")
            show("＋임베딩", marks_b, pick_b)
            newly = sorted(pick_b - pick_a)[:5]
            if newly:
                print("\n  임베딩이 새로 넣은 문장 (뜻으로 골라낸 것):")
                for i in newly:
                    print(f"     {marks_b[i].text[:62]}")

        if chooser:
            marks_c, _ = hl.plan(tr, lo, hi, target, snap=snap,
                                 vectors_of=vectors_of, weight=cfg.embed_weight,
                                 chooser=chooser, limit=cfg.llm_candidates,
                                 log=lambda t: print(f"  {t}"))
            pick_c = {m.index for m in marks_c if m.chosen}
            print(f"\n＋AI          문장 {len(pick_c)}개 · "
                  f"{hl.total_seconds(marks_c, snap):.0f}초")
            show("＋AI", marks_c, pick_c)
    finally:
        closer = getattr(chooser, "close", None)
        if closer:
            closer()

    print(f"\n{'=' * 60}")
    print("  마음에 드는 쪽으로 core/config.py 에서 켜고 끄면 됩니다.")
    print("     use_embedding = True / False")
    print("     use_llm       = True / False")
    print("  편집기 [3분 요약] 탭에서 [요약 골라줘] 를 누르면 이 설정대로 고릅니다.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
