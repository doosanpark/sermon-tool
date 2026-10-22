#!/usr/bin/env python3
"""학습 자료 만들기 — 모은 설교를 덩어리로 나누고, 정답(역할·점수)을 붙여 학습 문제로 바꾼다.

    python tools/train_data.py sheets            설교마다 '채점지'(덩어리 목록)를 만든다
    python tools/train_data.py check             정답 파일이 채점지와 맞는지 검사
    python tools/train_data.py sft               정답을 AI 학습 문제(jsonl)로 바꾼다
    python tools/train_data.py stats             모은 것 · 정답 붙인 것 · 학습 문제 수

덩어리는 **설교 편집기와 똑같은 코드로** 나눈다 (ui_summary 의 [요약 골라줘] 와 같은 순서).
학습한 AI 가 실제로 받게 될 글과 학습 때 본 글이 같아야 하기 때문이다. 묻는 글도
core.blocks.section_prompt / whole_prompt 를 그대로 쓴다.

정답 파일 (data/train/teacher/영상.json):
    {
      "video_id": "...",
      "theme": "설교 중심 메시지 한 문장",
      "sections": ["구획 1 요약 한 문장", "구획 2 요약", ...],      # 구획 수와 같아야 한다
      "whole": {"문제제기": 1, "핵심": 7, "적용": 11},            # 구획 번호 (1부터)
      "blocks": [[0, "안내", 2, 1], [1, "문제제기", 4, 3], ...],   # [번호, 역할, 혼자, 인상]
      "picks": {"opening": [12, 13], "core": [53, 54], "closing": [86, 87]},  # 덩어리 번호 [처음, 끝]
      "notes": "채점하며 남길 말 (없어도 됨)"
    }
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TRAIN = ROOT / "data" / "train"
TEACHER = TRAIN / "teacher"
SHEETS = TRAIN / "sheets"
SFT = TRAIN / "sft"

ROLES = ("문제제기", "핵심", "예화", "적용", "봉독", "안내", "기타")
ROLE_KEY = {"문제제기": "opening", "핵심": "core", "예화": "story", "적용": "closing",
            "봉독": "reading", "안내": "aside", "기타": "body"}
KEY_ROLE = {v: k for k, v in ROLE_KEY.items()}
# 반드시 검증용으로 남기는 설교 — 시나리오 작가가 직접 보고 의견을 준 2026-09-06 설교
ALWAYS_VALID = {"Fn2PhIgXtHI"}


def hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


# ------------------------------------------------------------------ 덩어리
def collected() -> list[str]:
    """자막과 쉼이 모두 있는 설교 (날짜순)."""
    out = []
    for meta in sorted((TRAIN / "meta").glob("*.json")):
        info = json.loads(meta.read_text(encoding="utf-8"))
        vid = info["id"]
        if (ROOT / "data" / "cache" / f"{vid}.transcript.json").exists() and \
                (TRAIN / "pauses" / f"{vid}.json").exists():
            out.append((info.get("title_date") or info.get("upload_date") or "", vid))
    return [v for _, v in sorted(out)]


def meta_of(vid: str) -> dict:
    return json.loads((TRAIN / "meta" / f"{vid}.json").read_text(encoding="utf-8"))


_EMB = None


def embedder():
    """임베딩은 있으면 쓴다 — 설교 편집기의 '뜻까지 보기' 와 같다 (규칙 역할에만 영향)."""
    global _EMB
    if _EMB is None:
        try:
            from core.config import Config
            from core import embed
            cfg = Config()
            _EMB = embed.SentenceEmbedder(cfg) if embed.is_ready(cfg) else False
        except Exception:
            _EMB = False
    return _EMB or None


def build(vid: str):
    """설교 편집기와 같은 순서로 문장 → 덩어리 → 구획을 만든다."""
    from core import blocks as blk, highlight as hl, transcribe
    from core.pauses import Pause

    info = meta_of(vid)
    lo, hi = info["sermon"]
    transcript = transcribe.load_transcript(ROOT / "data" / "cache" / f"{vid}.transcript.json")
    pauses = [Pause(a, b) for a, b in
              json.loads((TRAIN / "pauses" / f"{vid}.json").read_text(encoding="utf-8"))]
    marks = hl.build_marks(transcript, lo, hi)
    hl.score_marks(marks, hl.keywords(marks))
    emb = embedder()
    if emb is not None:
        try:
            hl.apply_embedding(marks, emb.encode([m.text for m in marks]), 1.6)
        except Exception:
            pass
    blk.mark_exits(marks)
    blocks = blk.segment(marks, pauses)
    blk.label_rules(blocks, marks)
    secs = blk.sections(blocks)
    return info, marks, blocks, secs


# ------------------------------------------------------------------ 채점지
def sheet_text(info: dict, blocks, secs) -> str:
    out = [f"# 설교: {info.get('title', '')}  ({info.get('title_date') or info.get('upload_date')})",
           f"# 영상 ID: {info['id']}   덩어리 {len(blocks)}개 · 구획 {len(secs)}개 · "
           f"설교 {(info['sermon'][1] - info['sermon'][0]) / 60:.0f}분",
           ""]
    for k, sec in enumerate(secs):
        lo = int(sec[0].pos * 100)
        out.append(f"## 구획 {k + 1}  (설교 {lo}% 지점부터, 덩어리 {sec[0].index}~{sec[-1].index})")
        for b in sec:
            hint = ""
            if b.role in ("reading", "aside"):
                hint = f"  ※규칙이 본 역할: {KEY_ROLE[b.role]}"
            out.append(f"[{b.index}] ({hms(b.start)}~{hms(b.end)}, {b.duration:.0f}초){hint}")
            out.append(b.text)
        out.append("")
    return "\n".join(out)


def cmd_sheets(args) -> int:
    SHEETS.mkdir(parents=True, exist_ok=True)
    vids = collected()
    made = 0
    for vid in vids:
        path = SHEETS / f"{vid}.txt"
        if path.exists() and not args.force:
            continue
        info, _marks, blocks, secs = build(vid)
        path.write_text(sheet_text(info, blocks, secs), encoding="utf-8")
        (SHEETS / f"{vid}.blocks.json").write_text(json.dumps({
            "video_id": vid, "sections": [[b.index for b in s] for s in secs],
            "blocks": [{"i": b.index, "start": round(b.start, 2), "end": round(b.end, 2),
                        "rule": b.role} for b in blocks]}, ensure_ascii=False), encoding="utf-8")
        made += 1
        print(f"  채점지 {vid}: 덩어리 {len(blocks)} · 구획 {len(secs)}")
    print(f"채점지 {made}개 새로 만듦 (모은 설교 {len(vids)}편)")
    return 0


# ------------------------------------------------------------------ 정답 검사
def validate(vid: str, lab: dict, blocks=None, secs=None) -> list[str]:
    """정답 파일이 덩어리·구획과 맞는지. 문제 목록을 돌려준다 (빈 목록이면 통과)."""
    if blocks is None:
        _info, _m, blocks, secs = build(vid)
    errs: list[str] = []
    idx = {b.index for b in blocks}
    got = {}
    for row in lab.get("blocks", []):
        if not (isinstance(row, list) and len(row) == 4):
            errs.append(f"덩어리 줄 모양이 틀림: {row}")
            continue
        i, role, alone, impact = row
        if i not in idx:
            errs.append(f"없는 덩어리 번호 {i}")
        if role not in ROLES:
            errs.append(f"[{i}] 역할 '{role}' 은 목록에 없음")
        for name, v in (("혼자", alone), ("인상", impact)):
            if not (isinstance(v, int) and 1 <= v <= 5):
                errs.append(f"[{i}] {name} 값 {v} (1~5 정수여야 함)")
        got[i] = row
    missing = sorted(idx - set(got))
    if missing:
        errs.append(f"정답이 빠진 덩어리 {len(missing)}개: {missing[:10]}")
    if len(lab.get("sections", [])) != len(secs):
        errs.append(f"구획 요약 {len(lab.get('sections', []))}개 — 구획은 {len(secs)}개")
    for key in ("문제제기", "핵심", "적용"):
        v = (lab.get("whole") or {}).get(key)
        if not (isinstance(v, int) and 1 <= v <= len(secs)):
            errs.append(f"whole.{key} = {v} (1~{len(secs)} 구획 번호여야 함)")
    by = {b.index: b for b in blocks}
    for key, rng in (lab.get("picks") or {}).items():
        if key not in ("opening", "core", "closing"):
            errs.append(f"picks 의 '{key}' 는 모르는 칸")
            continue
        if not (isinstance(rng, list) and len(rng) == 2 and rng[0] in by and rng[1] in by
                and rng[0] <= rng[1] and rng[1] - rng[0] <= 3):
            errs.append(f"picks.{key} = {rng} (연속된 덩어리 1~4개 [처음, 끝])")
            continue
        dur = by[rng[1]].end - by[rng[0]].start
        if not (10 <= dur <= 150):
            errs.append(f"picks.{key} 길이 {dur:.0f}초 — 10~150초여야 함")
    if not str(lab.get("theme", "")).strip():
        errs.append("theme(주제) 이 비어 있음")
    return errs


def cmd_check(args) -> int:
    bad = 0
    folder = Path(args.dir) if getattr(args, "dir", "") else TEACHER
    files = sorted(folder.glob("*.json"))
    for f in files:
        vid = f.stem
        try:
            lab = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"  {vid}: JSON 이 깨짐 ({exc})")
            bad += 1
            continue
        errs = validate(vid, lab)
        if errs:
            bad += 1
            print(f"  {vid}: 문제 {len(errs)}개 — " + " / ".join(errs[:4]))
    print(f"정답 {len(files)}개 중 문제 있는 것 {bad}개")
    return 1 if bad else 0


# ------------------------------------------------------------------ 학습 문제
def examples_for(vid: str, lab: dict) -> list[dict]:
    """정답 하나를 학습 문제 여러 개로 — 구획마다 하나 + 설교 전체 하나."""
    from core import blocks as blk
    _info, _m, blocks, secs = build(vid)
    errs = validate(vid, lab, blocks, secs)
    if errs:
        raise ValueError(f"{vid}: " + " / ".join(errs[:3]))
    rows = {r[0]: r for r in lab["blocks"]}
    out = []
    for k, sec in enumerate(secs):
        user, _budget = blk.section_prompt(secs, k)
        answer = {"요약": lab["sections"][k].strip(),
                  "덩어리": [{"번호": b.index, "역할": rows[b.index][1],
                            "혼자": rows[b.index][2], "인상": rows[b.index][3]} for b in sec]}
        out.append({"video_id": vid, "kind": "section", "k": k,
                    "system": blk.AI_SYSTEM, "user": user + "\n\n/no_think",
                    "assistant": json.dumps(answer, ensure_ascii=False)})
    whole = {"주제": lab["theme"].strip(), "문제제기": lab["whole"]["문제제기"],
             "핵심": lab["whole"]["핵심"], "적용": lab["whole"]["적용"]}
    out.append({"video_id": vid, "kind": "whole",
                "system": blk.AI_SYSTEM,
                "user": blk.whole_prompt([s.strip() for s in lab["sections"]]) + "\n\n/no_think",
                "assistant": json.dumps(whole, ensure_ascii=False)})
    return out


def split(vids: list[str], share: float = 0.12, seed: int = 7) -> tuple[list[str], list[str]]:
    """설교 단위로 나눈다 (같은 설교의 구획이 학습·검증에 섞이면 점수가 부풀려진다)."""
    rest = [v for v in vids if v not in ALWAYS_VALID]
    rnd = random.Random(seed)
    rnd.shuffle(rest)
    n_valid = max(1, round(len(vids) * share)) - len([v for v in vids if v in ALWAYS_VALID])
    valid = sorted([v for v in vids if v in ALWAYS_VALID] + rest[:max(n_valid, 0)])
    train = sorted(v for v in vids if v not in valid)
    return train, valid


def cmd_sft(_args) -> int:
    SFT.mkdir(parents=True, exist_ok=True)
    vids = sorted(f.stem for f in TEACHER.glob("*.json"))
    if not vids:
        print("정답 파일이 없습니다 (data/train/teacher)")
        return 1
    train, valid = split(vids)
    counts = {}
    for name, part in (("train", train), ("valid", valid)):
        n = 0
        with (SFT / f"{name}.jsonl").open("w", encoding="utf-8") as fh:
            for vid in part:
                lab = json.loads((TEACHER / f"{vid}.json").read_text(encoding="utf-8"))
                for ex in examples_for(vid, lab):
                    fh.write(json.dumps(ex, ensure_ascii=False) + "\n")
                    n += 1
        counts[name] = n
    (SFT / "split.json").write_text(json.dumps({"train": train, "valid": valid}, ensure_ascii=False,
                                               indent=1), encoding="utf-8")
    print(f"학습 문제: 학습용 {counts['train']}개 (설교 {len(train)}편) · "
          f"검증용 {counts['valid']}개 (설교 {len(valid)}편)")
    return 0


def cmd_stats(_args) -> int:
    vids = collected()
    labs = sorted(f.stem for f in TEACHER.glob("*.json"))
    print(f"모은 설교 {len(vids)}편 · 정답 붙인 설교 {len(labs)}편")
    for name in ("train", "valid"):
        p = SFT / f"{name}.jsonl"
        if p.exists():
            print(f"  {name}: {sum(1 for _ in p.open(encoding='utf-8'))}문제")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheets")
    s.add_argument("--force", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("--dir", default="", help="정답 폴더 (기본 data/train/teacher)")
    sub.add_parser("sft")
    sub.add_parser("stats")
    args = ap.parse_args()
    return {"sheets": cmd_sheets, "check": cmd_check, "sft": cmd_sft, "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
