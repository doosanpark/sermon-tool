"""설교에서 3분 요약에 넣을 문장을 고른다.

편집 모형이 이 파일의 모양을 정한다. 사용자는 타임라인 막대를 끄는 게 아니라
**자막을 읽으며 문장을 켜고 끈다.** 그래서 여기서 다루는 단위도 '조각'이 아니라
전사문 문장 하나하나이고, 이어붙일 구간은 내보낼 때 연속 선택을 묶어서 만든다.

고르는 근거 (2026-07-26 설교에서 확인한 것):
  - 목사님은 대목을 **번호로 선언**하신다. "첫 번째에요", "두 번째입니다" …
    이 설교에서는 8:43 / 12:27 / 18:25 / 24:30 에 거의 균등하게 놓여 있었다.
    요약의 뼈대는 이 선언 문장이 잡는다.
  - 선언 직후 30초는 대부분 **성경 봉독**이다. 강해설교가 '선언 → 낭독 → 해설'
    순서로 가기 때문이다. 규칙만으로 연속해서 자르면 제목과 봉독만 남고
    목사님 해설이 하나도 안 들어간다 — 그래서 낭독은 빼고 해설을 고른다.

번호 선언이 없는 설교도 있다. 그때는 개요가 비고 점수만으로 고르게 되는데,
그러면 뼈대가 약해진다. **이 파일에는 아직 AI 선정이 없다.** 규칙으로만 고른다.

논문(arXiv:2506.02351 DIAMOND)이 말하는 3단 중 여기 들어온 것은 앞뒤 두 단이다:
    Preparation  수치·규칙으로 후보를 좁힌다 → classify / find_outline / pauses
    Decision     LLM 이 문맥으로 고른다      → **없음.** score_marks 가 대신 선다
    Reflection   사람이 확정한다             → 편집기 [3분 요약] 탭
논문에서 수치 지표(WPA)만 쓰면 F1 42.9% 였고 LLM 단계를 얹은 전체 구조가
84.8% 였다(KBO 경기 5편). 지금 여기 있는 것은 그 구조의 아래쪽 절반이다.
AI 를 붙일 자리는 auto_pick 뒤, fit_to_target 앞이다 — 문장 번호만 받아
Mark.chosen 을 갈아 끼우면 나머지는 그대로 돌아간다.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

# 구간 하나를 실제 쉼에 맞춰 주는 함수. core.pauses 가 만들어 넘긴다.
# 여기서 직접 부르지 않고 받아 쓰는 이유는, 화면에 뜨는 길이와 실제로 나오는
# 영상 길이가 반드시 같아야 하기 때문이다. 스냅은 구간을 앞뒤로 벌리므로
# (실측 177초 → 210초, ＋19%) 이걸 빼고 세면 화면이 거짓말을 한다.
Snap = Callable[[float, float], "tuple[float, float]"]

# ---------------------------------------------------------------- 문장 갈래

# 성경 본문 특유의 어미. 목사님 해설에는 거의 안 나온다.
_SCRIPTURE_TAIL = re.compile(
    r"(하나이다|하옵소서|하시옵소서|이니이다|나이다|하리라|하시니라|느니라"
    r"|이로다|사오니|하였나니|이시여|할지라|지어다|되었더라|하더라|이라\s*하시"
    # 2026-09-06 설교(느헤미야 5장)에서 놓친 개역개정 어미
    r"|하였으나|하였더니|하였느니라|하였으며|이르되|가로되|하니라|하였노라|하리이다|하였도다)")

# 본문이 어디인지 알리는 말. 내용이 아니라 안내다.
# 실측: "니에미아 1장 오늘 본문 5절부터 11절까지 말씀입니다" 가 되풀이되는
# 낱말 때문에 점수가 높게 나와 요약에 들어갔다.
_ANNOUNCE = re.compile(
    r"\d+\s*절.{0,14}(말씀입니다|말씀이고|말씀이에요|보겠습니다|읽겠습니다|입니다)\s*$"
    r"|본문.{0,8}(말씀입니다|입니다)\s*$"
    r"|오늘\s*(의\s*)?본문은")

# 봉독을 시키는 말. 이 뒤로 성경체 문장이 이어진다.
_READ_CUE = re.compile(
    r"(읽겠습니다|읽습니다|봉독|다\s*같이|한\s*목소리로|같이\s*보실까요"
    r"|제가\s*읽|구절은\s*다"
    # "제가 6절과 7절을 보겠습니다 같이 말씀을 봐주시기 바랍니다" — 이 뒤가 봉독이다
    r"|\d+\s*절.{0,12}(보겠습니다|봅시다|보시겠습니다)|말씀을\s*봐\s*주시)")

# 대목 선언. "첫 번째에요" 같은 말.
_POINT = re.compile(r"(첫|두|세|네|다섯|여섯)\s*번째")
_POINT_LAST = re.compile(r"마지막\s*(으로|.{0,3}번째)")
# 선언이 아닌 것들 — 시리즈 회차, 지난주 되짚기, 인물의 '첫 번째 반응'
_NOT_POINT = re.compile(r"(시간(입니다|이에요|이었)|지난|시리즈|반응|번째\s*시간)")

# 맺음말로 넘어가는 신호
_CLOSING = re.compile(r"(말씀을\s*맺|정리하면|결론적으로|마무리하겠|맺으려고)")

# 예배 진행·광고. 설교 내용이 아니다.
_ASIDE = re.compile(r"(광고|공지|알려드립니다|주보|헌금\s*위원|안내드립|생일|축하합니다)")

# 듣는 사람에게 건네는 말. 요약에 넣으면 설교처럼 들린다.
_APPLY = re.compile(r"(우리|오늘|여러분|성도님|이번\s*한\s*주|한\s*주간|저와\s*여러분)")

# 명제·대조 구조. 설교의 핵심 문장은 대개 이 모양이다.
_THESIS = re.compile(r"(가\s*아니라|이\s*아니라|아닙니다|보다\s*먼저|것입니다|합니다만"
                     r"|해야\s*합니다|하는\s*것이|라는\s*겁니다|라는\s*것입니다)")

_JOSA = re.compile(r"(은|는|이|가|을|를|의|에|에서|로|으로|과|와|도|만|께|께서|부터|까지)$")

# 자주 나오지만 뜻이 없는 말. 핵심어로 뽑히면 점수가 아무 문장에나 붙는다.
_FILLER = {
    "거예요", "겁니다", "입니다", "있습니다", "합니다", "습니다", "그런데", "그래서",
    "그리고", "그러니까", "우리가", "우리는", "그것을", "이것을", "그거는", "이렇게",
    "그렇게", "저렇게", "지금", "정말", "진짜", "너무", "다시", "먼저", "말씀을",
    "하나님", "하나님이", "여러분", "때문에", "위해서", "가지고", "하시는", "하는",
}


@dataclass
class Mark:
    """전사문 문장 하나. GUI 한 줄이 이 하나에 대응한다."""
    index: int
    start: float
    end: float
    text: str
    kind: str = "body"        # point | reading | body | close | aside
    score: float = 0.0
    chosen: bool = False
    why: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Outline:
    """설교 개요. 목사님이 번호로 선언하신 대목들."""
    points: list[Mark] = field(default_factory=list)
    closing: Mark | None = None

    @property
    def found(self) -> bool:
        return len(self.points) >= 2


# ---------------------------------------------------------------- 낱말

def _stem(word: str) -> str:
    return _JOSA.sub("", word.strip(" .,?!\"'"))


def keywords(marks: list[Mark], top: int = 14) -> list[str]:
    """설교에서 되풀이되는 말. 반복은 곧 강조다.

    형태소 분석기를 쓰지 않는다 — 설치본에 넣을 수 없다. 조사만 떼어낸
    낱말과 두 낱말 묶음의 빈도로도 핵심어는 충분히 드러난다.
    """
    uni: Counter[str] = Counter()
    bi: Counter[str] = Counter()
    for m in marks:
        if m.kind in ("reading", "aside"):
            continue
        words = [_stem(w) for w in m.text.split()]
        words = [w for w in words if len(w) >= 2]
        uni.update(words)
        bi.update(f"{a} {b}" for a, b in zip(words, words[1:]))

    def ok(phrase: str) -> bool:
        return all(part not in _FILLER for part in phrase.split())

    out = [w for w, c in bi.most_common(top * 3) if c >= 3 and ok(w)]
    joined = " ".join(out)
    out += [w for w, c in uni.most_common(top * 4)
            if c >= 5 and w not in _FILLER and w not in joined]
    return out[:top]


# ---------------------------------------------------------------- 갈래 나누기

def classify(marks: list[Mark]) -> None:
    """문장마다 갈래를 매긴다. 낭독·광고를 먼저 걷어내야 점수가 뜻을 갖는다."""
    reading_until = -1.0
    for m in marks:
        if _ASIDE.search(m.text) or _ANNOUNCE.search(m.text):
            m.kind = "aside"
            continue
        if _SCRIPTURE_TAIL.search(m.text):
            m.kind = "reading"
            reading_until = m.end + 8.0      # 낭독은 몇 문장씩 이어진다
            continue
        if _READ_CUE.search(m.text):
            m.kind = "reading"
            reading_until = m.end + 20.0     # 지시 직후는 낭독일 가능성이 높다
            continue
        if m.start <= reading_until and len(m.text) < 30:
            m.kind = "reading"
            continue
        if _CLOSING.search(m.text):
            m.kind = "close"


def find_outline(marks: list[Mark], sermon_start: float,
                 sermon_end: float) -> Outline:
    """번호로 선언된 대목을 순서대로 찾는다.

    같은 번호가 여러 번 나오기 때문에(＂시리즈 두 번째 시간＂ 같은 말) 걸러야 한다.
    거르는 방법은 두 가지다 — 회차·되짚기 표현을 빼고, **번호가 순서대로 커지는
    조합만** 받아들인다. 도입부(설교 시작 3분)와 너무 촘촘한 것(60초 이내)도 뺀다.
    """
    order = {"첫": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6}
    lead_in = sermon_start + 180.0
    picked: list[Mark] = []
    want = 1

    for m in marks:
        if m.kind in ("reading", "aside") or m.start < lead_in:
            continue
        if _NOT_POINT.search(m.text):
            continue
        hit = _POINT.search(m.text)
        num = order.get(hit.group(1)) if hit else None
        if num is None and _POINT_LAST.search(m.text):
            num = want
        if num != want:
            continue
        if picked and m.start - picked[-1].start < 60.0:
            continue
        m.kind = "point"
        m.why = f"{num}번째 대목 선언"
        picked.append(m)
        want += 1

    closing = None
    for m in marks:
        if m.kind == "close" and m.start > sermon_start + (sermon_end - sermon_start) * 0.6:
            closing = m
            break
    return Outline(points=picked, closing=closing)


# ---------------------------------------------------------------- 점수

def score_marks(marks: list[Mark], keys: list[str]) -> None:
    """문장마다 '요약에 넣을 만한 정도'를 매긴다.

    점수는 순위를 매기기 위한 것이지 정답이 아니다. 사람이 화면에서 고쳐
    쓰라고 만든 값이라, 애매하면 낮게 준다.
    """
    for m in marks:
        if m.kind == "point":
            m.score = 10.0
            continue
        if m.kind in ("reading", "aside"):
            m.score = -5.0
            continue

        s = 0.0
        why: list[str] = []
        text = m.text

        hits = [k for k in keys if k in text]
        if hits:
            s += 1.2 * min(len(hits), 3)
            why.append(f"되풀이되는 말({hits[0]})")
        if _THESIS.search(text):
            s += 1.4
            why.append("핵심을 말하는 문장 꼴")
        if _APPLY.search(text):
            s += 0.9
            why.append("듣는 사람에게 건네는 말")
        if m.kind == "close":
            s += 2.0
            why.append("맺음말")

        n = len(text)
        if n < 12:
            s -= 1.5                       # 토막말은 이어붙이면 튄다
        elif 20 <= n <= 70:
            s += 0.6                       # 한 호흡에 들어오는 길이
        if m.duration < 1.5:
            s -= 1.0

        m.score = round(s, 2)
        m.why = " · ".join(why)


# ---------------------------------------------------------------- 고르기

def build_marks(transcript: list[dict], sermon_start: float,
                sermon_end: float) -> list[Mark]:
    """전사문에서 설교 구간 문장만 뽑아 Mark 로 만든다."""
    marks: list[Mark] = []
    for seg in sorted(transcript, key=lambda s: s["start"]):
        if seg.get("label") == "benediction":
            continue
        if seg["end"] <= sermon_start or seg["start"] >= sermon_end:
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        marks.append(Mark(index=len(marks), start=float(seg["start"]),
                          end=float(seg["end"]), text=text))
    classify(marks)
    return marks


def _window_best(marks: list[Mark], lo: int, hi: int, want: float,
                 taken: set[int]) -> list[int] | None:
    """[lo, hi) 안에서 점수가 가장 높은 연속 묶음을 찾는다.

    문장 하나만 떼면 앞뒤가 잘려 무슨 말인지 알기 어렵다. 2~5문장을 붙여
    한 호흡으로 들리게 만든다.
    """
    best: tuple[float, list[int]] | None = None
    for a in range(lo, hi):
        if a in taken or marks[a].kind in ("reading", "aside"):
            continue
        total = 0.0
        dur = 0.0
        run: list[int] = []
        for b in range(a, min(hi, a + 6)):
            if b in taken or marks[b].kind in ("reading", "aside"):
                break
            run.append(b)
            total += marks[b].score
            dur += marks[b].duration
            if dur < 6.0:
                continue
            if dur > want + 6.0:
                break
            density = total / max(dur, 1.0)
            if best is None or density > best[0]:
                best = (density, list(run))
    return best[1] if best else None


def auto_pick(marks: list[Mark], outline: Outline, target: float = 180.0,
              snap: Snap | None = None) -> None:
    """프로그램이 처음 골라 주는 밑그림. 사용자가 이 위에서 고친다."""
    for m in marks:
        m.chosen = False
    if not marks:
        return

    taken: set[int] = set()

    def take(idxs: list[int] | None) -> None:
        for i in idxs or []:
            marks[i].chosen = True
            taken.add(i)

    bounds = [p.index for p in outline.points]

    # 1) 대목 선언은 무조건 넣는다. 요약의 뼈대다.
    #    "이제 마지막 네 번째입니다" 처럼 선언과 제목이 두 문장으로 갈리는 일이
    #    잦다. 뒤 문장까지 붙여야 "두려움보다 사명이 커지는 기도의 자리" 가 들린다.
    for p in outline.points:
        span = [p.index]
        dur = marks[p.index].duration
        j = p.index + 1
        while j < len(marks) and len(span) < 3 and dur < 9.0:
            if marks[j].kind not in ("body", "close"):
                break
            span.append(j)
            dur += marks[j].duration
            j += 1
        for i in span:
            marks[i].chosen = True
            taken.add(i)

    # 2) 설교를 구획으로 나누고 **몫을 똑같이 나눠** 담는다.
    #
    #    구획마다 알아서 고르게 두면 긴 대목에 시간이 몰린다. 마지막 대목은
    #    맺음말까지 품어 다른 대목의 두 배가 넘는 일이 흔하고, 실측에서도
    #    네 번째에 41초가 가고 나머지는 23~31초였다. 요약이 뒤로 쏠린다.
    #    그래서 구획 수로 나눈 몫을 각자 채우게 한다.
    #    구획은 서로 겹치지 않아야 한다. 마지막 대목의 구획을 설교 끝까지 뻗게
    #    두면 맺음말과 마지막 권면을 통째로 삼켜, 실측에서 그 둘이 0초가 됐다.
    first = bounds[0] if bounds else len(marks) // 4
    close_i = outline.closing.index if outline.closing else max(0, len(marks) - 22)
    tail_i = max(close_i, len(marks) - 14)

    zones: list[tuple[int, int]] = [(max(0, first - 26), first)]      # 명제
    for k, start_i in enumerate(bounds):
        stop = bounds[k + 1] if k + 1 < len(bounds) else close_i
        if stop - start_i > 1:
            zones.append((start_i + 1, min(stop, close_i)))
    if close_i < tail_i:
        zones.append((close_i, tail_i))                              # 맺음말
    zones.append((tail_i, len(marks)))                               # 마지막 권면

    spent = total_seconds(marks, snap)
    quota = max(8.0, (target - spent) / max(len(zones), 1))
    for lo, hi in zones:
        if hi - lo < 2:
            continue
        got = 0.0
        for _ in range(4):                       # 한 구획에서 최대 네 묶음
            if got >= quota:
                break
            run = _window_best(marks, lo, hi, min(quota - got, 16.0), taken)
            if not run:
                break
            got += sum(marks[i].duration for i in run)
            take(run)

    # 개요를 못 찾은 설교 — 점수만으로 고르게 된다
    if not outline.found:
        while total_seconds(marks, snap) < target * 0.8:
            got = _window_best(marks, 0, len(marks), 14.0, taken)
            if not got:
                break
            take(got)

    fit_to_target(marks, outline, target, snap=snap)
    if drop_slivers(marks, outline, snap):
        fit_to_target(marks, outline, target, snap=snap)


def drop_slivers(marks: list[Mark], outline: Outline, snap: Snap | None = None,
                 min_piece: float = 4.5) -> int:
    """너무 짧은 조각을 뺀다.

    `fit_to_target` 이 길이를 맞추느라 문장을 하나씩 빼는데, 그게 이어져 있던
    조각의 한가운데면 조각이 둘로 쪼개진다. 그렇게 2~4초짜리 토막이 생기면
    이어붙였을 때 뚝뚝 끊긴다. 실측: 조각 22개 중 4개가 5초 미만이었고
    가장 짧은 것이 2초였다.

    대목 선언이 든 조각은 짧아도 남긴다 — 요약의 뼈대다.
    """
    protected = {p.index for p in outline.points}
    removed = 0
    for start, end in runs(marks, snap):
        if end - start >= min_piece:
            continue
        inside = [m for m in marks
                  if m.chosen and start - 0.5 <= m.start and m.end <= end + 0.5]
        if not inside or any(m.index in protected for m in inside):
            continue
        for m in inside:
            m.chosen = False
            removed += 1
    return removed


def total_seconds(marks: list[Mark], snap: Snap | None = None) -> float:
    """실제로 나올 영상 길이. 스냅으로 벌어지는 몫까지 넣어 센다."""
    return sum(e - s for s, e in runs(marks, snap))


def fit_to_target(marks: list[Mark], outline: Outline, target: float = 180.0,
                  tol: float = 8.0, snap: Snap | None = None) -> None:
    """목표 길이에 맞춘다.

    1차 선정은 거의 늘 목표를 넘긴다 — 실측에서 180초 목표에 221.6초(＋23%)가
    나왔다. 넘친 만큼을 덜어내는 단계가 따로 있어야 하는 이유다.
    덜어낼 때 **대목 선언은 건드리지 않는다.** 뼈대가 무너지면 요약이 아니다.
    """
    protected = {p.index for p in outline.points}

    guard = 0
    while total_seconds(marks, snap) > target + tol and guard < 400:
        guard += 1
        cand = [m for m in marks if m.chosen and m.index not in protected]
        if not cand:
            break
        # 점수가 낮은 것부터, 같으면 긴 것부터 뺀다
        drop = min(cand, key=lambda m: (m.score, -m.duration))
        drop.chosen = False

    guard = 0
    while total_seconds(marks, snap) < target - tol and guard < 400:
        guard += 1
        cand = [m for m in marks
                if not m.chosen and m.kind not in ("reading", "aside")]
        if not cand:
            break
        # 이미 고른 문장 바로 옆을 먼저 붙인다 — 말이 이어져야 자연스럽다
        chosen_idx = {m.index for m in marks if m.chosen}

        def key(m: Mark) -> tuple[int, float]:
            near = 0 if (m.index - 1 in chosen_idx or m.index + 1 in chosen_idx) else 1
            return (near, -m.score)

        add = min(cand, key=key)
        # 이미 목표의 7할을 넘겼으면 점수가 0 이하인 문장은 넣지 않는다.
        # 길이를 맞추자고 군더더기를 넣으면 요약이 아니라 짜깁기가 된다.
        if add.score <= 0.0 and total_seconds(marks, snap) > target * 0.70:
            break
        add.chosen = True


def runs(marks: list[Mark], snap: Snap | None = None) -> list[tuple[float, float]]:
    """연속으로 고른 문장들을 이어붙일 구간으로 묶는다.

    snap 을 주면 그 구간을 실제 쉼에 맞춘 뒤, 맞추다 겹친 것을 다시 합친다.
    화면에 뜨는 길이와 내보낸 영상 길이가 같으려면 여기 한 곳만 거쳐야 한다.
    """
    out: list[tuple[float, float]] = []
    for m in marks:
        if not m.chosen:
            continue
        if out and m.start - out[-1][1] <= 0.35:
            out[-1] = (out[-1][0], m.end)
        else:
            out.append((m.start, m.end))

    if not snap:
        return out

    snapped = [snap(s, e) for s, e in out]
    merged: list[tuple[float, float]] = []
    for s, e in sorted(snapped):
        if merged and s <= merged[-1][1] + 0.15:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def summarize(marks: list[Mark], outline: Outline, target: float = 180.0,
              snap: Snap | None = None) -> str:
    """화면 위쪽에 띄울 한 줄."""
    total = total_seconds(marks, snap)
    n = sum(1 for m in marks if m.chosen)
    diff = total - target
    mm, ss = divmod(int(round(total)), 60)
    head = f"지금 {mm}분 {ss:02d}초 · 문장 {n}개"
    if abs(diff) <= 8:
        return head + " · 목표에 맞습니다"
    if diff > 0:
        return head + f" · 목표보다 {int(diff)}초 깁니다"
    return head + f" · 목표보다 {int(-diff)}초 짧습니다"


# ---------------------------------------------------------------- 임베딩·LLM

def apply_embedding(marks: list[Mark], vecs, weight: float = 1.6) -> int:
    """뜻으로 잰 대표성을 규칙 점수에 섞는다.

    규칙은 낱말만 본다. 그래서 "니에미아 1장 오늘 본문 5절부터 11절까지 말씀입니다"
    처럼 되풀이되는 낱말이 든 안내 문장이 높게 나오고, 정작 설교의 명제인
    "그의 기도를 보면 그의 삶의 구조가 보여요" 는 낮게 나왔다.
    설교 전체의 평균 방향에 가까운 문장일수록 그 설교를 대표한다.

    점수 규모가 서로 다르므로 표준화해서 섞는다. 몇 점을 더할지가 아니라
    '평균보다 얼마나 위인가' 를 더한다.
    """
    import numpy as np

    from .embed import importance

    if vecs is None or len(vecs) != len(marks) or not len(marks):
        return 0
    rep = np.asarray(importance(vecs), dtype=float)
    if not rep.any():
        return 0
    spread = float(rep.std())
    if spread < 1e-6:
        return 0
    z = (rep - float(rep.mean())) / spread

    touched = 0
    for m, dz in zip(marks, z):
        if m.kind in ("reading", "aside", "point"):
            continue          # 봉독은 빼고, 대목 선언은 이미 최고점이다
        m.score = round(m.score + weight * float(dz), 2)
        if dz > 0.8:
            m.why = (m.why + " · " if m.why else "") + "설교 전체를 대표하는 말"
        touched += 1
    return touched


def llm_candidates(marks: list[Mark], outline: Outline, limit: int) -> list[int]:
    """LLM 에게 넘길 후보를 고른다. 이게 DIAMOND 의 Preparation 단계다.

    551문장을 통째로 넘기면 1.5만 토큰이라 작은 모델에는 버겁고 느리다.
    점수 상위만 추려 3천 토큰으로 줄인다. 대목 선언은 점수와 무관하게 넣는다 —
    뼈대가 후보에 없으면 LLM 도 살릴 수 없다.
    """
    must = {p.index for p in outline.points}
    if outline.closing:
        must.add(outline.closing.index)
    rest = [m for m in marks
            if m.index not in must and m.kind not in ("reading", "aside")]
    rest.sort(key=lambda m: -m.score)
    keep = must | {m.index for m in rest[:max(0, limit - len(must))]}
    return sorted(keep)


def llm_revise(marks: list[Mark], outline: Outline, chooser, target: float,
               limit: int = 90, log=None) -> str:
    """좁혀진 후보를 LLM 에게 보여 주고 남길 것을 고르게 한다.

    실패하면 아무것도 바꾸지 않는다. 답이 비었든 터졌든 규칙이 고른 결과가
    그대로 남는다 — 켜서 나빠지는 일은 없어야 한다.
    """
    say = log or (lambda t: None)
    idxs = llm_candidates(marks, outline, limit)
    if len(idxs) < 8:
        return "후보가 너무 적어 AI 고르기를 건너뜁니다"

    items = [(i, marks[i].duration, marks[i].text) for i in idxs]
    try:
        picked = chooser(items, target)
    except Exception as exc:                       # 서버·형식·시간초과 무엇이든
        hint = ""
        if "timed out" in str(exc).lower() or "timeout" in type(exc).__name__.lower():
            hint = (" — 이 PC 에는 AI 가 버거운 듯합니다. [고르는 방법] 을 "
                    "'뜻까지 보기' 로 두시면 몇 초에 끝나고 결과도 나쁘지 않습니다")
        return f"AI 고르기를 건너뜁니다 ({type(exc).__name__}){hint}"
    if not picked:
        return "AI 가 고르지 못해 규칙이 고른 결과를 그대로 씁니다"

    chosen = set(picked)
    chosen |= {p.index for p in outline.points}    # 뼈대는 빠뜨리지 않는다
    for m in marks:
        m.chosen = m.index in chosen and m.kind not in ("reading", "aside")
    say(f"  AI 가 후보 {len(idxs)}개 중 {len(picked)}개를 골랐습니다")
    return f"AI 가 골랐습니다 (후보 {len(idxs)} → {len(picked)})"


def make_helpers(cfg, log=None):
    """설정을 보고 임베딩·LLM 도우미를 만든다. 없으면 None 을 돌려준다.

    onnxruntime 과 llama-server 를 여기서만 늦게 불러온다. 그래야
    highlight 자체는 그것들 없이도 import 되고, selftest 가 가볍게 돈다.
    """
    say = log or (lambda t: print(f"  {t}"))
    vectors_of = None
    chooser = None
    notes: list[str] = []

    if getattr(cfg, "use_embedding", False):
        try:
            from . import embed
            if not embed.is_ready(cfg):
                embed.download(cfg, progress=say)
            emb = embed.SentenceEmbedder(cfg)
            vectors_of = emb.encode
            notes.append("임베딩 켜짐")
        except Exception as exc:
            notes.append(f"임베딩 없이 진행합니다 ({exc})")

    if getattr(cfg, "use_llm", False):
        try:
            from . import llm as llm_mod
            if not llm_mod.model_path(cfg).exists():
                llm_mod.download(cfg, progress=say)
            server = llm_mod.start(cfg, progress=say)

            def ask_llm(items, target, _s=server, _m=llm_mod):
                return _m.choose(_s, items, target)

            ask_llm.close = server.close      # 부르는 쪽이 끝나면 닫는다

            def ask_raw(system, user, max_tokens=400, _s=server, _m=llm_mod):
                return _m.ask(_s, system, user, max_tokens=max_tokens)

            ask_llm.ask = ask_raw             # 덩어리 역할 매기기(blocks.label_with_ai)가 쓴다
            chooser = ask_llm
            notes.append("AI 고르기 켜짐")
        except Exception as exc:
            notes.append(f"AI 고르기 없이 진행합니다 ({exc})")

    return vectors_of, chooser, notes


def plan(transcript: list[dict], sermon_start: float, sermon_end: float,
         target: float = 180.0, snap: Snap | None = None,
         vectors_of=None, chooser=None, weight: float = 1.6,
         limit: int = 90, log=None) -> tuple[list[Mark], Outline]:
    """문장 만들기 → 갈래 → 개요 → 규칙 점수 → (임베딩) → 밑그림 → (AI) → 길이 맞추기.

    임베딩과 AI 는 있으면 쓰고 없으면 건너뛴다. 어느 쪽이 빠져도 결과가 나온다.
    DIAMOND 로 치면 앞의 셋이 Preparation, AI 가 Decision, 편집기가 Reflection 이다.
    """
    say = log or (lambda t: None)
    marks = build_marks(transcript, sermon_start, sermon_end)
    if not marks:
        return marks, Outline()
    outline = find_outline(marks, sermon_start, sermon_end)
    score_marks(marks, keywords(marks))

    if vectors_of is not None:
        try:
            vecs = vectors_of([m.text for m in marks])
            touched = apply_embedding(marks, vecs, weight)
            say(f"  뜻으로 다시 매긴 문장 {touched}개")
        except Exception as exc:
            say(f"  임베딩을 건너뜁니다 ({type(exc).__name__}: {exc})")

    auto_pick(marks, outline, target, snap=snap)

    if chooser is not None:
        say("  " + llm_revise(marks, outline, chooser, target, limit, log=say))
        fit_to_target(marks, outline, target, snap=snap)
    return marks, outline
