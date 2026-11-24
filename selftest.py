#!/usr/bin/env python3
"""무거운 의존성(torch/whisper/yt-dlp) 없이 검출·보정·자막 로직만 검증한다.

    python selftest.py
"""
from __future__ import annotations

from core import highlight as hl
from core import pauses as pz
from core.config import Config
from core.detect import detect
from core.diarize import Segment
from core.export import build_srt
from core.marker import Marker
from core.refine import refine_benediction, refine_sermon


def fake_service() -> tuple[list[Segment], float]:
    """전형적인 예배 1시간 20분짜리를 합성한다."""
    segs: list[Segment] = []

    def add(start_min: float, end_min: float, spk: str, step: float = 0.5) -> None:
        t = start_min * 60
        while t < end_min * 60:
            segs.append(Segment(t, min(t + step * 60, end_min * 60), spk))
            t += step * 60

    add(0, 10, "SPEAKER_00")        # 찬양 인도
    add(12, 16, "SPEAKER_01")       # 대표기도
    add(32.3, 69.7, "SPEAKER_02")   # 봉독 + 설교 (37.4분)
    add(75.3, 76.8, "SPEAKER_02")   # 축도 (1.5분)
    add(70.5, 74.0, "SPEAKER_00")   # 헌금 찬양
    segs.sort(key=lambda s: s.start)
    return segs, 78 * 60


def main() -> int:
    cfg = Config()
    segs, duration = fake_service()
    det = detect(segs, duration, cfg)

    assert det.pastor == "SPEAKER_02", det.pastor
    assert abs(det.sermon.start - 32.3 * 60) < 60, det.sermon
    assert abs(det.sermon.end - 69.7 * 60) < 60, det.sermon
    assert det.benediction is not None
    assert abs(det.benediction.start - 75.3 * 60) < 60, det.benediction
    assert det.confidence >= 0.9, det.confidence
    assert det.bene_confidence >= 0.9, det.bene_confidence
    print(f"검출 OK  설교 {det.sermon.duration/60:.1f}분 / 축도 {det.benediction.duration/60:.1f}분"
          f"  신뢰도 {det.confidence} / {det.bene_confidence}")

    # 경계 보정
    transcript = [
        {"start": 1935.0, "end": 1939.0, "text": "오늘 본문은 요한복음 3장 16절입니다", "label": "sermon"},
        {"start": 2000.0, "end": 2004.0, "text": "하나님이 세상을 이처럼 사랑하사", "label": "sermon"},
        {"start": 4180.0, "end": 4184.0, "text": "함께 기도하겠습니다", "label": "sermon"},
        {"start": 4520.0, "end": 4526.0, "text": "주 예수 그리스도의 은혜와 하나님의 사랑과", "label": "benediction"},
        {"start": 4600.0, "end": 4603.0, "text": "아멘", "label": "benediction"},
    ]
    s, e, notes = refine_sermon(transcript, 1938.0, 4182.0, cfg)
    assert s < 1938.0 and e > 4182.0, (s, e)
    assert notes, notes
    bs, be, bnotes = refine_benediction(transcript, 4518.0, 4590.0, cfg)
    assert bs <= 4520.0 and be >= 4600.0, (bs, be)
    print(f"보정 OK  설교 {s:.1f}~{e:.1f}s / 축도 {bs:.1f}~{be:.1f}s")

    # 마커 우선순위: human_* 이 ai_* 를 이긴다
    m = Marker(video_id="test", duration=duration,
               sermon_ai_start=s, sermon_ai_end=e,
               bene_ai_start=bs, bene_ai_end=be)
    assert len(m.ranges()) == 2
    m.sermon_human_start = 1900.0
    assert m.sermon_range()[0] == 1900.0
    assert [r[2] for r in m.ranges()] == ["sermon", "benediction"]
    print("마커 OK  human_* 우선 적용, 시간순 정렬")

    # 자막 타임라인 재계산: 두 번째 클립 자막이 첫 클립 길이만큼 밀려야 한다
    ranges = [(1900.0, 4190.0, "sermon"), (4515.0, 4610.0, "benediction")]
    srt = build_srt(ranges, [2290.0, 95.0], transcript)
    lines = srt.splitlines()
    assert lines[1].startswith("00:00:35"), lines[1]          # 1935 - 1900
    assert "00:38:15" in srt, srt[:400]                        # 2290 + (4520-4515)
    assert srt.count("-->") == 5
    print("자막 OK  이어붙인 타임라인 기준으로 재계산됨")

    # 이웃 문장의 꼬리가 딸려 들어와 깜빡이면 안 된다.
    # 컷을 쉼에 맞추느라 구간을 앞뒤로 벌리는데, 전사문 문장은 빈틈없이 이어져
    # 있어서 조금이라도 겹치면 넣던 예전 방식은 0.2초짜리 자막을 만들었다.
    tight = [(1938.0, 1941.0, "sermon")]      # 다음 문장은 머리 0.5초만 걸친다
    seg = [{"start": 1937.0, "end": 1940.5, "text": "제대로 든 문장", "label": "sermon"},
           {"start": 1940.5, "end": 1948.0, "text": "꼬리만 걸친 문장", "label": "sermon"}]
    out = build_srt(tight, [3.0], seg)
    assert "꼬리만 걸친 문장" not in out, "0.5초짜리 꼬리 자막이 살아남았다"
    assert "제대로 든 문장" in out, "제대로 든 문장까지 버렸다"
    print("자막 깜빡임 OK  0.5초짜리 꼬리는 버리고 제대로 든 문장은 남김")

    check_summary()
    check_stages()
    check_fixwords()
    check_blocks()
    check_training_match()
    check_ducking()
    check_visual()
    check_reels()

    print("\n전체 통과")
    return 0


def fake_sermon() -> list[dict]:
    """번호로 대목을 선언하는 전형적인 강해설교를 흉내 낸다."""
    rows = [
        (0, "오늘 우리가 함께 볼 말씀은 느헤미야 1장 5절부터 11절입니다"),
        (6, "느헤미야 말씀 시리즈 두 번째 시간입니다"),           # 선언이 아님 (회차)
        (12, "지난 첫 번째 시간 때 우리가 무엇을 나눴습니까"),      # 선언이 아님 (되짚기)
        (200, "그의 기도를 보면 그의 삶의 구조가 보입니다"),
        (206, "기도는 상황을 바꾸는 도구가 아니라 나를 맞추는 과정입니다"),
        (300, "첫 번째에요 문제보다 하나님이 먼저 보이는 기도의 자리였습니다"),
        (308, "제가 읽습니다 5절 말씀 같이 보실까요"),              # 봉독 지시
        (314, "이르되 하늘의 하나님 크고 두려우신 하나님이여 간구하나이다"),  # 성경체
        (330, "무너진 성벽 이야기가 먼저 나온 게 아닙니다"),
        (336, "하나님에 대한 고백이 먼저 자리를 가득 채웁니다"),
        (600, "두 번째입니다 밖에서 안으로 책임의 방향이 바뀌는 기도의 자리였어요"),
        (612, "우리가 범죄했습니다 나와 내 아버지 집이 범죄했습니다"),
        (900, "세 번째는 감정보다 말씀이 기준이 되는 기도의 자리입니다"),
        (912, "위기 앞에서 그가 붙잡은 것은 하나님이 하신 말씀이에요"),
        (1200, "이제 마지막 네 번째입니다"),
        (1206, "두려움보다 사명이 커지는 기도의 자리였는데요"),
        (1212, "환경은 그대로였습니다 아무것도 변한 게 없습니다"),
        (1400, "말씀을 맺으려고 합니다"),
        (1406, "기도로 삶의 틀을 새롭게 세우고 싶다 해보세요"),
        (1412, "이번 한 주간 느헤미야의 네 걸음을 따라가 보세요"),
    ]
    base = 1000.0
    return [{"start": base + t, "end": base + t + 5.5, "text": txt, "label": "sermon"}
            for t, txt in rows]


def check_stages() -> None:
    """규칙 → 임베딩 → AI 세 단계가 서로를 망가뜨리지 않는지.

    가짜 임베딩·가짜 AI 로 껍데기만 본다. 모델 품질이 아니라 **어느 하나가
    죽어도 앞 단계 결과가 그대로 남는가** 를 확인하는 검사다.
    """
    import random

    tr = fake_sermon()
    lo, hi = 1000.0, 1000.0 + 1420.0
    base, outline = hl.plan(tr, lo, hi, target=60.0)
    base_pick = {m.index for m in base if m.chosen}

    # 임베딩이 터져도 규칙 결과가 남아야 한다
    def boom(_texts):
        raise RuntimeError("모델 없음")

    m2, _ = hl.plan(tr, lo, hi, target=60.0, vectors_of=boom)
    assert {m.index for m in m2 if m.chosen} == base_pick, "임베딩이 죽자 결과가 바뀌었다"

    # AI 가 터져도 앞 단계 결과가 남아야 한다
    def boom2(_items, _target):
        raise ConnectionError("서버 없음")

    m3, _ = hl.plan(tr, lo, hi, target=60.0, chooser=boom2)
    assert {m.index for m in m3 if m.chosen} == base_pick, "AI 가 죽자 결과가 바뀌었다"

    # AI 가 엉뚱한 번호를 줘도 봉독이 들어가거나 대목이 빠지면 안 된다
    def mischief(items, _target):
        return [i for i, _, _ in items][:4] + [10 ** 6]

    m4, ol4 = hl.plan(tr, lo, hi, target=60.0, chooser=mischief)
    assert all(m4[p.index].chosen for p in ol4.points), "AI 답 때문에 대목이 빠졌다"
    assert not any(m.chosen for m in m4 if m.kind in ("reading", "aside")), "봉독이 들어갔다"

    # 임베딩 점수 섞기 — 대목 선언과 봉독은 건드리지 않는다
    marks = hl.build_marks(tr, lo, hi)
    ol = hl.find_outline(marks, lo, hi)
    hl.score_marks(marks, hl.keywords(marks))
    before = {m.index: m.score for m in marks}
    rnd = random.Random(0)
    vecs = [[rnd.random() for _ in range(8)] for _ in marks]
    import numpy as np
    hl.apply_embedding(marks, np.array(vecs, dtype=float), weight=1.6)
    for m in marks:
        if m.kind in ("reading", "aside", "point"):
            assert m.score == before[m.index], f"건드리면 안 되는 문장이 바뀜: {m.text[:20]}"

    # 후보 좁히기 — 대목은 점수와 무관하게 들어가야 한다
    cand = hl.llm_candidates(marks, ol, limit=8)
    assert all(p.index in cand for p in ol.points), "후보에서 대목이 빠졌다"
    assert not any(marks[i].kind in ("reading", "aside") for i in cand), "후보에 봉독이 있다"
    print(f"단계 OK      임베딩·AI 가 죽어도 앞 단계 결과 유지 · 후보 {len(cand)}개에 대목 전부 포함")


def check_summary() -> None:
    tr = fake_sermon()
    lo, hi = 1000.0, 1000.0 + 1420.0
    marks = hl.build_marks(tr, lo, hi)
    assert len(marks) == 20, len(marks)

    # 봉독은 걸러져야 한다
    def kind_of(needle: str) -> str:
        return next(m.kind for m in marks if needle in m.text)

    assert kind_of("제가 읽습니다") == "reading", kind_of("제가 읽습니다")
    assert kind_of("이르되 하늘의") == "reading", kind_of("이르되 하늘의")
    assert kind_of("고백이 먼저") == "body", kind_of("고백이 먼저")

    # 회차·되짚기를 대목 선언으로 잘못 잡으면 안 된다
    outline = hl.find_outline(marks, lo, hi)
    assert len(outline.points) == 4, [p.text for p in outline.points]
    assert outline.points[0].text.startswith("첫 번째에요"), outline.points[0].text
    assert outline.points[3].text.startswith("이제 마지막"), outline.points[3].text
    assert outline.closing is not None
    print(f"요약 개요 OK  대목 {len(outline.points)}곳 + 맺음말, 회차·되짚기는 걸러짐")

    hl.score_marks(marks, hl.keywords(marks))
    hl.auto_pick(marks, outline, target=60.0)
    for p in outline.points:
        assert marks[p.index].chosen, f"대목 선언이 빠졌다: {p.text}"
    assert not any(m.chosen for m in marks if m.kind == "reading"), "봉독이 들어갔다"
    print(f"요약 고르기 OK  {hl.summarize(marks, outline, 60.0)}  대목 선언 모두 포함")

    # 구획마다 고르게 담겼는가. 마지막 대목의 구획을 설교 끝까지 뻗게 두면
    # 맺음말과 마지막 권면을 삼켜 그 둘이 0초가 된다 (실측에서 그랬다).
    assert outline.closing is not None
    tail = [m for m in marks if m.chosen and m.index >= outline.closing.index]
    assert tail, "맺음말 뒤에서 아무것도 고르지 않았다 — 구획이 겹쳤다"
    head = [m for m in marks if m.chosen and m.index < outline.points[0].index]
    assert head, "첫 대목 앞(명제)에서 아무것도 고르지 않았다"
    # 본문 안내는 내용이 아니다
    assert not any("본문" in m.text and "말씀입니다" in m.text
                   for m in marks if m.chosen), "본문 안내가 요약에 들어갔다"
    print(f"요약 균형 OK  명제 {len(head)}문장 · 맺음 뒤 {len(tail)}문장 · 본문 안내 없음")

    # 화면에 뜨는 길이와 실제로 나올 길이가 같아야 한다
    fake_pauses = [pz.Pause(t - 0.6, t + 0.2) for t in range(1000, 2500, 6)]
    snap = pz.make_snap(fake_pauses)
    assert abs(hl.total_seconds(marks, snap)
               - sum(e - s for s, e in hl.runs(marks, snap))) < 1e-6
    assert hl.total_seconds(marks, snap) >= hl.total_seconds(marks), "스냅은 구간을 벌린다"
    print("요약 길이 OK  화면 표시와 내보낼 길이가 같은 함수로 계산됨")

    # 요약 선택이 clips 를 오염시키면 [설교 영상 만들기] 가 망가진다
    m = Marker(video_id="s", sermon_ai_start=lo, sermon_ai_end=hi)
    m.set_summary_picks([x.start for x in marks if x.chosen])
    assert len(m.ranges()) == 1 and m.ranges()[0][2] == "sermon", m.ranges()
    got = m.summary_chosen([x.start for x in marks])
    assert got == [x.chosen for x in marks], "저장·복원이 어긋난다"
    print("요약 저장 OK  clips 와 섞이지 않고, 저장한 대로 되살아남")


def check_fixwords() -> None:
    """자막 오인식 사전. 실제로 틀렸던 말을 고치고, 맞는 말은 건드리지 않고, 두 번 돌려도 같아야 한다."""
    from core.fixwords import fix_text
    cases = {
        "니에미아가 분노했습니다": "느헤미야가 분노했습니다",
        "뉘미안을 보면": "느헤미야를 보면",
        "하나님을 경유하면서 살지 않을 겁니까?": "하나님을 경외하면서 살지 않을 겁니까?",
        "가난인의 사랑과 성경의 교통하심": "하나님의 사랑과 성령의 교통하심",
        "사랑하는 창의직교의 성도 여러분": "사랑하는 창의진교회의 성도 여러분",
        "산발라스는 경계하면서": "산발랏은 경계하면서",
        "요안복음 3장 16절": "요한복음 3장 16절",
    }
    for wrong, right in cases.items():
        got = fix_text(wrong)
        assert got == right, (wrong, got)
        assert fix_text(got) == got, got                    # 두 번 돌려도 같다
    for keep in ("경의를 표합니다", "경유지를 거쳐 갑니다", "이미 아는 이야기입니다"):
        assert fix_text(keep) == keep, keep                  # 맞는 말은 그대로
    print(f"자막 사전 OK  오인식 {len(cases)}종 고침 · 맞는 말 그대로 · 다시 돌려도 같음")


def check_blocks() -> None:
    """덩어리 · 역할 · 형식. AI 가 엉뚱하게 답해도 칸이 비지 않고 봉독은 들어가지 않아야 한다."""
    from core import blocks as blk
    from core import highlight as hl

    transcript = fake_sermon()
    lo, hi = transcript[0]["start"], transcript[-1]["end"]
    marks = hl.build_marks(transcript, lo, hi)
    hl.score_marks(marks, hl.keywords(marks))
    blk.mark_exits(marks)
    blocks = blk.segment(marks, [])
    blk.label_rules(blocks, marks)
    assert len(blocks) >= 4, len(blocks)
    for b in blocks:
        assert 0 < b.duration <= 70, (b.index, b.duration)

    def check(slots, fmt):
        assert slots and all(s.chosen for s in slots), fmt
        chosen = [s.chosen for s in slots]
        for i, a in enumerate(chosen):
            for b in chosen[i + 1:]:
                assert not a.overlaps(b), (fmt, a, b)          # 칸끼리 겹치지 않는다
        blk.apply_slots(slots, blocks, marks)
        assert not any(m.chosen and m.kind == "reading" for m in marks), fmt   # 봉독은 안 넣는다
        return len(slots)

    n2 = check(blk.assemble("two", blocks, 100), "two")
    assert n2 == 2, n2
    n3 = check(blk.assemble("three", blocks, 180), "three")
    n1 = check(blk.assemble("one", blocks, 180), "one")
    assert (n3, n1) == (3, 1), (n3, n1)

    # AI 가 전부 '핵심' 이라고 우겨도 문제 제기 · 적용 칸이 사라지지 않는다
    def bad_ai(system, user, max_tokens):
        import re
        nums = [int(x) for x in re.findall(r"^\[(\d+)\]", user, re.M)]
        return '{"덩어리": [' + ",".join(f'{{"번호": {n}, "역할": "핵심"}}' for n in nums) + "]}"
    reading_before = {b.index for b in blocks if b.role == "reading"}
    blk.label_with_ai(blocks, bad_ai)
    assert {b.index for b in blocks if b.role == "reading"} == reading_before   # 봉독은 AI 가 못 바꾼다
    check(blk.assemble("three", blocks, 180), "three-after-bad-ai")

    def dead_ai(system, user, max_tokens):
        raise TimeoutError("timed out")
    blk.label_with_ai(blocks, dead_ai)                       # 죽어도 예외 없이 지나간다
    print(f"덩어리 OK  {blk.describe(blocks)} · 세 덩어리/한 대목 칸 채움 · AI 가 엉뚱해도 칸 유지")


def check_training_match() -> None:
    """학습한 AI 가 받는 글 = 학습 때 본 글. 그리고 학습한 모델이 있으면 그걸 쓴다.

    tools/train_data.py 는 section_prompt / whole_prompt 로 학습 문제를 만든다. 설교 편집기
    (label_with_ai) 가 묻는 글이 그것과 한 글자라도 다르면 학습한 보람이 줄어든다.
    """
    import tempfile
    from pathlib import Path
    from core import blocks as blk, highlight as hl, llm as llm_mod
    from core.config import Config

    transcript = fake_sermon()
    lo, hi = transcript[0]["start"], transcript[-1]["end"]
    marks = hl.build_marks(transcript, lo, hi)
    hl.score_marks(marks, hl.keywords(marks))
    blocks = blk.segment(marks, [])
    blk.label_rules(blocks, marks)
    secs = blk.sections(blocks)
    asked: list[str] = []

    def record(system, user, max_tokens):
        asked.append(user)
        return '{"요약": "시험 요약"}'
    blk.label_with_ai(blocks, record)
    want = [blk.section_prompt(secs, k)[0] for k in range(len(secs))]
    assert asked[:len(secs)] == want, "구획 묻는 글이 학습 자료와 다릅니다"
    if len(secs) >= 2:
        assert asked[len(secs)] == blk.whole_prompt(["시험 요약"] * len(secs)), "전체 묻는 글이 다릅니다"

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.work_dir = Path(tmp)
        base = llm_mod.model_dir(cfg) / cfg.llm_model_file
        assert llm_mod.model_path(cfg) == base and not llm_mod.is_tuned(cfg)
        tuned = llm_mod.tuned_path(cfg)
        tuned.parent.mkdir(parents=True)
        tuned.write_bytes(b"gguf")
        assert llm_mod.model_path(cfg) == tuned and llm_mod.is_tuned(cfg)
    print(f"학습 맞춤 OK  묻는 글 {len(asked)}개가 학습 자료와 같음 · 학습한 모델이 있으면 그걸 씀")


def check_ducking() -> None:
    """FilmCraft 의 ducking.rs 시험을 그대로 옮겼다 — 같은 값이 나와야 같은 방법이다."""
    import tempfile
    import wave
    from pathlib import Path

    import numpy as np

    from core import ducking as d

    sr = 1000.0
    x = np.array([0.0 if i < 1000 else (0.1 if i % 2 == 0 else -0.1) for i in range(2000)])
    e = d.envelope_db(np.stack([x, x], 1), sr, 0.01, 0.02)
    assert len(e) == 200 and e[10] == d.FLOOR_DB, e[:12]
    assert abs(e[150] + 20.0) < 0.01, e[150]
    assert -30.0 < e[100] < -20.0, e[100]

    env = np.full(300, -80.0)
    for a, b in [(50, 100), (110, 150), (200, 203)]:
        env[a:b] = -20.0
    r = d.activity(env, 0.01, -40.0, 0.1, 0.2)
    assert len(r) == 1 and abs(r[0][0] - 0.5) < 1e-6 and abs(r[0][1] - 1.5) < 1e-6, r
    assert len(d.activity(env, 0.01, -40.0, 0.1, 0.0)) == 2
    env2 = np.full(10, -80.0)
    env2[5:] = 0.0
    assert d.activity(env2, 0.1, -40.0, 0.0, 0.0) == [(0.5, 1.0)]
    assert [d.sensitivity_threshold_db(s) for s in (5, 0, 10, 99)] == [-40, -20, -60, -60]

    k = d.duck_keyframes([(2.0, 4.0)], 0.0, 10.0, 0.0, 15.0, 0.5)
    assert k == [(1.5, 0.0), (2.0, -15.0), (4.0, -15.0), (4.5, 0.0)], k
    k = d.duck_keyframes([(2.0, 4.0), (4.8, 6.0)], 0.0, 10.0, -3.0, 12.0, 0.5)
    assert k == [(1.5, -3.0), (2.0, -15.0), (6.0, -15.0), (6.5, -3.0)], k
    assert len(d.duck_keyframes([(5.0, 6.0), (1.0, 2.0)], 0.0, 10.0, 0.0, 10.0, 0.25)) == 8
    assert d.duck_keyframes([(-1.0, 2.0)], 0.0, 10.0, 0.0, 10.0, 0.5) == \
        [(0.0, -10.0), (2.0, -10.0), (2.5, 0.0)]
    assert d.duck_keyframes([(8.0, 9.75)], 0.0, 10.0, 0.0, 10.0, 0.5)[-1] == (10.0, -5.0)
    assert d.duck_keyframes([(11.0, 12.0)], 0.0, 10.0, 0.0, 10.0, 0.5) == []

    # 파일을 조금씩 읽어 만든 곡선이 한꺼번에 만든 것과 똑같아야 한다
    rng = np.random.default_rng(1)
    rate = 48000
    n = rate * 3 + 777
    sig = np.zeros(n)
    sig[int(0.3 * rate):int(1.9 * rate)] = rng.normal(0, 0.1, int(1.9 * rate) - int(0.3 * rate))
    pcm = np.clip(np.round(sig * 32768), -32768, 32767).astype("<i2")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.wav"
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(pcm.tobytes())
        ref = d.envelope_db(pcm.astype(np.float64) / 32768.0, rate)
        got = d.envelope_from_wav(path, block_s=0.37)
    assert len(ref) == len(got) and float(np.max(np.abs(ref - got))) < 1e-3
    g = d.gain_curve([(1.0, -10.0), (2.0, -30.0)], 3.0, -10.0)
    assert abs(g[0] - 10 ** (-0.5)) < 1e-6 and abs(g[-1] - 10 ** (-1.5)) < 1e-6
    print("덕킹 OK  FilmCraft ducking.rs 시험과 같은 값 · 나눠 읽어도 같은 곡선")


def check_reels() -> None:
    """[숏폼 편집] — 자막 짧게 끊기·자막 차례·틀, 그리고 모양을 안 주면 예전과 같은 그래프."""
    from pathlib import Path

    from core import reels as r
    from core import shorts as sh

    cues = [(0.0, 4.0, "힘이 폭력이 되는 거죠 돈도 폭력이 되고요 지위도 폭력이 되고"),
            (4.0, 4.5, "아멘"), (5.0, 9.0, "")]
    ch = r.chunk_captions(cues, 12)
    assert all(len(t.replace(" ", "")) <= 12 for _a, _b, t in ch), ch
    assert abs(ch[0][0]) < 1e-9 and abs(sum(b - a for a, b, _ in ch[:-1]) - 4.0) < 1e-6
    assert " ".join(t for _a, _b, t in ch[:-1]) == cues[0][2], "글자가 빠지거나 바뀌면 안 된다"
    assert ch[-1][2] == "아멘" and len(ch) == len(ch[:-1]) + 1
    assert r.caption_at(ch, 4.2) == "아멘" and r.caption_at(ch, 4.7) == ""
    track = r.caption_track([(1.0, 2.0, "a.png"), (3.0, 5.0, "b.png")], 2.5, 4.0)
    assert track[0] == (None, 0.5) and track[1][0] == "b.png" and abs(track[1][1] - 1.0) < 1e-9
    assert abs(sum(d for _n, d in r.caption_track([(1.0, 2.0, "a")], 0.0, 10.0)) - 10.0) < 1e-9
    srt = "1\n00:00:01,000 --> 00:00:02,500\n첫 줄\n\n2\n00:00:03,000 --> 00:00:04,000\n둘째\n"
    assert r.parse_srt(srt) == [(1.0, 2.5, "첫 줄"), (3.0, 4.0, "둘째")]
    lk = r.apply_preset(r.Look(logo="logo.png", cx=0.3), "yellow_box")
    assert lk.layout == "full" and lk.logo == "logo.png" and lk.cx == 0.3, "틀은 로고·자리를 그대로 둔다"
    assert r.Look.from_dict(lk.to_dict()) == lk and lk.area() == (0, 1920)
    assert r.clean_hooks("1. 우리는 무엇을 믿고 도전하나\n- \"하나님은 왜 기다리실까?\"\n[1,2]\n") == \
        ["우리는 무엇을 믿고 도전하나", "하나님은 왜 기다리실까?"]
    # 모양을 주지 않으면 예전 그래프·크롭과 글자 하나 다르지 않다
    runs = [(1.0, 3.0), (5.0, 9.0)]
    assert sh._compose_graph(runs, 300, 320, 10, 20, 29.97, "anull") == \
        sh._compose_graph(runs, 300, 320, 10, 20, 29.97, "anull", area_y=sh.HEAD_H,
                          area_h=sh.VIDEO_H)
    assert sh.crop_rect(1280, 720, 0.44, 0.61, 0.8) == (546, 576, 290, 144)
    assert sh.crop_rect(1280, 720, 0.44, 0.61, 0.8, 1920)[0] == 324
    # 세로 영상 · 예전 요약 영상 — 정한 부분 안에서만 자른다
    mid = r.Look(source="middle", layout="bands")
    reg = mid.region(1080, 1920)
    assert reg == (0, 330, 1080, 1140)
    w, h, x, y = mid.crop(1080, 1920)
    assert (w, h, x, y) == (1080, 1140, 0, 330), (w, h, x, y)
    full = r.Look(source="middle", layout="full")
    w, h, x, y = full.crop(1080, 1920)
    assert h == 1140 and abs(w / h - 1080 / 1920) < 0.01 and 330 <= y and x + w <= 1080
    assert r.Look(source="whole", layout="full").crop(1080, 1920) == (1080, 1920, 0, 0)
    assert "region" in full.build_args(1080, 1920) and "region" not in r.Look().build_args(1280, 720)
    assert r.Look().crop(1280, 720) == sh.crop_rect(1280, 720, 0.44, 0.61, 0.8)
    with __import__("tempfile").TemporaryDirectory() as tmp:
        png = sh._blank_png(Path(tmp) / "b.png", 8, 4)
        assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        lst = r.ffconcat([("a.png", 1.5), ("blank.png", 0.0)], "blank.png", Path(tmp) / "c.txt")
        assert lst.read_text(encoding="utf-8").splitlines()[-1] == "file 'a.png'"
    print(f"숏폼 모양 OK  자막 {len(ch)}덩어리로 끊기 · 차례 · 틀 {len(r.PRESETS)}가지 · "
          "모양 없으면 예전 그래프 그대로")


def check_visual() -> None:
    """[음성 설교 영상 만들기] 의 칸 나누기·글자 배치·EXIF·ffmpeg 명령 모양."""
    import struct
    import tempfile
    from pathlib import Path

    from core import visual as v

    # 칸 나누기: 이어붙이면 소리 길이와 장면 수가 정확히 같아야 한다
    photo = v.Media(Path("a.jpg"), "photo")
    short = v.Media(Path("s.mp4"), "video", seconds=4.0, width=1920, height=1080)
    long_ = v.Media(Path("l.mp4"), "video", seconds=300.0, width=1080, height=1920)
    for total in (40 * 60 * 30 + 7, 95 * 30, 13 * 30):
        for media in ([photo], [short, photo, long_], [short]):
            slots = v.plan(media, total, 20, False, "x")
            got = sum(v.segment_frames(slots, i) for i in range(len(slots)))
            assert got == total, (total, got, [s.frames for s in slots])
            starts = v.slot_starts(slots)
            assert starts[0] == 0 and all(b > a for a, b in zip(starts, starts[1:]))
            if total > 30 * 30:
                assert slots[0].frames >= (v.CORNER_IN + v.CORNER_FADE) * v.FPS + v.XFADE
                assert all(s.frames >= 2 * v.XFADE for s in slots)
    slots = v.plan([short, photo, long_], 40 * 60 * 30, 20, False, "x")
    assert slots[0].media is photo, "첫 칸이 짧은 영상이면 사진을 앞으로"
    assert max(s.frames for s in slots if s.media is long_) <= v.VIDEO_MAX_SECONDS * v.FPS
    a = [s.media.path.name for s in v.plan([photo, short, long_], 9000, 20, True, "id1")]
    b = [s.media.path.name for s in v.plan([photo, short, long_], 9000, 20, True, "id1")]
    assert a == b, "섞어도 같은 설교는 같은 순서"
    calm = v.plan([photo], 9000, 20, False, "x", titled=False, still=True)
    assert all(s.motion == v.STILL for s in calm), "배경 없이 만들 때는 움직이지 않는다"

    # 글자: 정한 폭과 줄 수를 넘지 않는다. 두 줄이면 길이가 비슷하게 나눈다
    lines, size = v.fit("부르심을 따라 떠나는 믿음", 1720, (150, 136, 124), 2)
    assert lines == ["부르심을 따라", "떠나는 믿음"], lines
    lines, size = v.fit("하나님이 세상을 이처럼 사랑하사 독생자를 주셨으니 이는 그를 믿는 자마다 "
                        "멸망하지 않고 영생을 얻게 하려 하심이라", 1640, (104, 96, 88, 80, 72), 2)
    assert len(lines) == 2 and lines[-1].endswith("…")
    assert all(v.text_px(t, size) <= 1640 for t in lines)
    assert v.fit("", 100, (10,), 1) == ([], 10)

    # EXIF 방향 (휴대폰 사진)
    with tempfile.TemporaryDirectory() as tmp:
        ifd = struct.pack("<H", 1) + struct.pack("<HHII", 0x0112, 3, 1, 6) + struct.pack("<I", 0)
        app1 = b"Exif\x00\x00" + b"II" + struct.pack("<H", 42) + struct.pack("<I", 8) + ifd
        jpg = Path(tmp) / "p.jpg"
        jpg.write_bytes(b"\xff\xd8\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
                        + b"\xff\xd9")
        assert v.exif_orientation(jpg) == 6
        (Path(tmp) / "q.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        assert v.exif_orientation(Path(tmp) / "q.jpg") == 1
        assert v.exif_orientation(Path(tmp) / "none.jpg") == 1
    assert v._fills_frame(4000, 3000) and v._fills_frame(1920, 1080)
    assert not v._fills_frame(3000, 4000) and not v._fills_frame(1000, 1000)

    # ffmpeg 명령 모양 — 넘김은 둘째 조각부터, 제목 카드는 첫 조각에만, 끝은 검게
    real_require = v.require
    v.require = lambda name: name
    try:
        photo.prepared = Path("a_prep.jpg")
        slots = v.plan([photo, long_], 70 * 30, 20, False, "x")
        cmds = [v.segment_command(slots, i, Path(f"s{i}.mp4"), Path("card.png"),
                                  Path("corner.png"), "veryfast", 20, 2)
                for i in range(len(slots))]
    finally:
        v.require = real_require
    joined = [" ".join(c) for c in cmds]
    assert "xfade" not in joined[0] and all("xfade" in j for j in joined[1:])
    assert "card.png" in joined[0] and not any("card.png" in j for j in joined[1:])
    assert all("corner.png" in j for j in joined)
    assert "fade=t=in:st=0" in joined[0] and "fade=t=out" in joined[-1]
    assert all(c[c.index("-frames:v") + 1] == str(v.segment_frames(slots, i))
               for i, c in enumerate(cmds))
    text = v.description("부르심을 따라 떠나는 믿음", "창세기 12:1-4", "20261101")
    assert "부르심을 따라 떠나는 믿음 | 창세기 12:1-4" in text and "2026년 11월 1일" in text
    # 편집기 타임라인 → 칸: 길이가 정확하고, 넘김·되풀이·자르기가 뜻대로
    def it(name, dur, trans="dissolve", ts=1.0, kind="photo", motion="auto"):
        return {"path": name, "kind": kind, "len": 30.0 if kind == "video" else 0.0,
                "w": 1920, "h": 1080, "dur": dur, "motion": motion, "trans": trans,
                "trans_s": ts, "in": 0.0}
    tl = [it("a.jpg", 5), it("b.mp4", 8, "push_left", kind="video"), it("c.jpg", 7, "cut"),
          it("d.jpg", 6, "black", 1.5, motion="still")]
    for total in (40 * 60 * 30 + 7, 95 * 30, 13 * 30, 23 * 30):
        for fill in ("repeat", "hold"):
            slots = v.slots_from_items(tl, total, fill)
            got = sum(v.segment_frames(slots, i) for i in range(len(slots)))
            assert got == total, (total, fill, got)
            assert slots[0].xfade == 0 and all(s.frames > s.xfade for s in slots)
            if fill == "hold":
                assert not any(s.ghost for s in slots) and len(slots) <= len(tl)
    slots = v.slots_from_items(tl, 40 * 60 * 30, "repeat")
    assert [s.item for s in slots[:6]] == [0, 1, 2, 3, 0, 1] and slots[4].ghost
    assert slots[1].transition == "slideleft" and slots[1].xfade == 30
    assert slots[2].xfade == 0, "바로 넘기기는 겹치지 않는다"
    assert slots[3].transition == "fadeblack" and slots[3].xfade == 45
    assert slots[3].motion == v.STILL
    lay = v.item_layout(tl)
    assert [x[0] for x in lay] == v.slot_starts(slots)[:4]
    cut = v.slots_from_items(tl, 10 * 30, "repeat")
    assert len(cut) == 2 and sum(v.segment_frames(cut, i) for i in range(2)) == 300
    # 첫 장면이 짧으면 제목 카드가 둘째 조각에도 이어진다 (그 조각 몫만 잘라 쓴다)
    medias = [v.Media(Path(x["path"]), x["kind"], seconds=x["len"], width=1920, height=1080,
                      prepared=Path(x["path"])) for x in tl]
    slots = v.slots_from_items(tl, 60 * 30, "repeat", medias=medias)
    real_require = v.require
    v.require = lambda name: name
    try:
        c1 = " ".join(v.segment_command(slots, 1, Path("s1.mp4"), Path("card.png"),
                                        Path("corner.png"), "veryfast", 20, 1, []))
        c3 = " ".join(v.segment_command(slots, 3, Path("s3.mp4"), Path("card.png"),
                                        Path("corner.png"), "veryfast", 20, 1, []))
    finally:
        v.require = real_require
    assert "card.png" in c1 and "trim=start=4.0000" in c1 and "xfade=transition=slideleft" in c1
    assert "card.png" not in c3 and "xfade=transition=fadeblack:duration=1.500" in c3
    x0, y0, size = v.motion_rect(0, 600, 0)
    assert abs(size - 1.0) < 1e-9 and v.motion_rect(0, 600, 599)[2] < 0.9

    # 음성 설교 알아보기 — 제목만으로 되는 것과, 없는 파일은 아니라고 하는 것
    assert v.looks_audio_only("없는파일.mp4", "창의진교회 이른 아침예배 음성 LIVE 방송")[0]
    assert v.looks_audio_only("없는파일.mp4", "주일예배") == (False, "")
    print(f"사진·영상 입히기 OK  40분 → 칸 {len(v.plan([photo], 40 * 60 * 30, 20))}개, "
          "장면 수 정확 · 두 줄 고르게 · EXIF 6 · 첫 조각만 제목 카드 · 음성 설교 알아보기 · "
          "편집기 타임라인(넘김·되풀이·자르기)")


if __name__ == "__main__":
    raise SystemExit(main())
