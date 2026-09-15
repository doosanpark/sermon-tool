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


if __name__ == "__main__":
    raise SystemExit(main())
