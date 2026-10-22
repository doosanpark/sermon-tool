"""설교를 '덩어리'로 나누고, 덩어리마다 역할을 매긴다. 형식은 그 위에서 조립한다.

왜 문장이 아니라 덩어리인가 (2026-10-13 확인):
    문장 단위로 고르면 '문장 모음' 이 된다. 2026-09-06 설교로 만든 3분 요약은
    서로 떨어진 문장 28개(평균 6초)를 이어 붙여, 들어도 무슨 내용인지 알 수 없었다.
    DIAMOND 는 야구 하이라이트(서로 독립된 장면)를 고르는 구조라 그대로 옮기면
    이렇게 된다. 설교는 앞뒤가 이어진 논증이라 **20~60초 연속 구간** 이 단위여야 한다.

왜 역할인가:
    설교 형식은 매주 조금씩 다르다. 07-26 설교는 "첫 번째·두 번째…" 로 대목을
    선언하셨고 09-06 설교는 선언이 없었다. 번호 선언에 기대면 그런 주에 무너진다.
    대신 거의 모든 설교에 있는 흐름 — 문제 제기 → 핵심 → 적용 — 을 역할로 매긴다.
    역할까지만 이 파일이 정하고, **어떤 형식으로 이을지는 assemble() 이 정한다.**
    그래서 형식이 바뀌어도 이 바닥층(그리고 나중에 학습시킬 모델)은 그대로 쓴다.

세 단계 (DIAMOND 구조를 덩어리 단위로):
    Preparation  segment()      문단급 쉼에서 자른다 (pauses.py)
                 label_rules()  위치·낱말·문장 점수로 역할 밑그림
    Decision     label_with_ai() 로컬 AI 가 구획별로 읽고 역할·점수를 다시 매긴다.
                                NexusSum 식으로 **나눠서 읽고(구획) → 구획 요약을
                                모아 한 번 더 본다(위계).** 4B 모델은 36분을 한 번에
                                잘 못 읽지만 5분씩은 잘 읽는다.
    Reflection   편집기에서 칸마다 후보를 바꿔 가며 사람이 고른다.

AI 가 없거나 죽어도 규칙 밑그림이 그대로 남는다 — 켜서 나빠지는 일은 없어야 한다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

ROLE_NAMES = {
    "opening": "문제 제기",
    "core": "핵심",
    "story": "예화",
    "closing": "적용",
    "reading": "성경 봉독",
    "aside": "안내",
    "body": "설명",
}
_KO_ROLE = {
    "문제제기": "opening", "문제": "opening", "도입": "opening", "질문": "opening",
    "핵심": "core", "명제": "core", "주제": "core",
    "예화": "story", "이야기": "story", "예시": "story",
    "결단": "closing", "적용": "closing", "결론": "closing", "권면": "closing", "맺음": "closing",
    "봉독": "reading", "성경봉독": "reading", "낭독": "reading", "본문": "reading",
    "안내": "aside", "광고": "aside", "인사": "aside",
    "기타": "body", "설명": "body", "해설": "body",
}

# 형식 — 화면의 [형식] 상자와 같은 이름. 순서가 곧 화면 순서다.
FORMATS = {
    # 기본. 시나리오 작가 검토(2026-10-19): 릴스로 보는 사람은 '내가 명심하고 살아갈 말'
    # (적용)을 찾는다. 영상은 설교 전체를 듣고 싶게, 교회에 오고 싶게 하는 광고에 가깝다.
    # 그래서 핵심 → 적용만 남기고 길이를 1분 40초 안팎으로 줄였다.
    "two": "핵심요약 (핵심 → 적용, 1분 40초)",
    "three": "3분요약 (문제 → 핵심 → 적용, 2분 50초)",
}

# 화면에서는 위 두 가지만 고르게 한다 (2026-10-20). 아래 두 가지는 예전 방식이라
# 상자에서 뺐지만, 만드는 코드는 그대로 둔다 — 지난 작업을 불러올 때 쓰인다.
LEGACY_FORMATS = {
    "one": "한 대목 (1분 안팎)",
    "sentences": "문장 모음 (예전 방식)",
}


def format_name(key: str) -> str:
    """형식의 사람이 읽는 이름. 예전 방식으로 저장된 작업도 이름이 나오게 한다."""
    return FORMATS.get(key) or LEGACY_FORMATS.get(key, key)


def short_name(key: str) -> str:
    """괄호 설명을 뺀 짧은 이름. "핵심요약", "3분요약".

    화면에 띄우는 글과 파일 이름이 **모두 이 함수 하나**를 본다. 예전에는
    화면에는 "3분 요약" 이 박혀 있고 파일은 export 쪽에서 따로 지어서,
    핵심요약을 골랐는데 화면은 3분요약이라고 하는 일이 있었다 (2026-10-20).
    """
    return format_name(key or "two").split(" (")[0].strip()

# 듣는 사람에게 삶에 적용하자고 권하는 말
_APPEAL = re.compile(r"(않(?:겠)?습니까|아닙니까|합시다|바랍니다|원합니다|축원|되시기를|되길"
                     r"|살아갑시다|좋겠습니다|살아가시기|됩시다|하십시오|하세요)")
# 문제를 던지는 말
_PROBLEM = re.compile(r"(왜|어떻게|무엇이|문제|위기|고민|힘들|아프|흔들|무너|질문|까요\?|니까\?|\?)")
# 예화·경험담
_STORY = re.compile(r"(예를\s*들|이야기|어떤\s*분|한\s*분이|제가\s*.{0,12}(때|적)|옛날|어릴\s*때"
                    r"|있었습니다|했었|들었습니다|기억이\s*납니다|한번은)")
# 앞 문장 없이 들으면 뜻이 끊기는 시작
_LEANS = re.compile(r"^(그래서|그러니까|그러면|이것|그것|이게|그게|이렇게|그렇게"
                    r"|이런|그런|여기서|거기서)")
# 앞말에 살짝 기대지만 설교 입말에서는 혼자 들어도 대개 통하는 시작 — 조금만 깎는다.
# 09-06 설교 핵심 "근데 그 돈을 어떻게 쓰는지 하나님이 보고 계신다는 거예요" 가
# '근데' 하나 때문에 후보에서 밀려났다 (작가 검토에서 가장 좋다고 한 대목).
_LEANS_SOFT = re.compile(r"^(그런데|근데|그리고|또|그러나|하지만|그럼)")
# 설교가 끝나는 인사 — 요약에 넣으면 어색하다
_EXIT = re.compile(r"(자리에서\s*일어나|아멘\s*$|^아멘|기도하겠습니다\s*$|기도합시다\s*$)")
_CLOSING = re.compile(r"(말씀을\s*맺|정리하면|결론적으로|마무리하겠|맺으려고|사랑하는\s*.{0,8}성도\s*여러분)")


@dataclass
class Block:
    index: int
    first: int                 # 처음 문장(Mark.index)
    last: int                  # 마지막 문장(포함)
    start: float
    end: float
    text: str
    pos: float = 0.0           # 설교 안 위치 0~1
    role: str = "body"
    score: float = 0.0
    alone: float = 0.6         # 이 덩어리만 떼어 들어도 이해되는 정도 0~1
    why: str = ""
    by_ai: bool = False
    section: int = 0           # AI 가 읽은 구획 번호
    read_share: float = 0.0    # 봉독·안내가 차지하는 몫 0~1 (역할이 '설명' 이어도 섞여 있을 수 있다)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Cand:
    """칸 하나에 넣을 후보 — 연속된 덩어리 1~3개."""
    first: int                 # Block.index
    last: int
    start: float
    end: float
    score: float
    preview: str = ""

    def overlaps(self, other: "Cand") -> bool:
        return not (self.end <= other.start or other.end <= self.start)


@dataclass
class Slot:
    key: str                   # opening | core | closing
    name: str                  # 화면에 보일 이름
    cands: list[Cand] = field(default_factory=list)
    cur: int = 0

    @property
    def chosen(self) -> Cand | None:
        return self.cands[self.cur] if self.cands else None


# ------------------------------------------------------------------ 나누기
def mark_exits(marks) -> None:
    """'자리에서 일어나겠습니다', '아멘' 처럼 설교를 닫는 인사를 안내로 돌린다."""
    for m in marks:
        if m.kind in ("body", "close") and _EXIT.search(m.text.strip()):
            m.kind = "aside"


def _gap_at(marks, i: int, pauses) -> float:
    """문장 i 와 i+1 사이에 실제로 쉰 길이. 전사문에는 쉼이 없어 오디오에서 본다."""
    if not pauses or i + 1 >= len(marks):
        return 0.0
    lo, hi = marks[i].end - 1.0, marks[i + 1].start + 1.0
    best = 0.0
    for p in pauses:
        if p.end < lo:
            continue
        if p.start > hi:
            break
        best = max(best, p.end - p.start)
    return best


def segment(marks, pauses=None, min_len: float = 18.0, max_len: float = 55.0) -> list[Block]:
    """문단급 쉼에서 잘라 20~60초 덩어리로 묶는다.

    목사님이 숨을 길게 고르시는 자리(실측 2초 이상, 평균 25초에 한 번)가 곧
    말의 문단이다. 봉독이 시작되고 끝나는 자리도 경계로 삼는다 — 봉독과 해설이
    한 덩어리에 섞이면 그 덩어리를 쓸 수 없게 된다.
    """
    if not marks:
        return []
    pauses = sorted(pauses or [], key=lambda p: p.start)
    gaps = [_gap_at(marks, i, pauses) for i in range(len(marks))]

    def is_read(m) -> bool:
        return m.kind in ("reading", "aside")

    groups: list[tuple[int, int]] = []
    a = 0
    for i in range(len(marks)):
        dur = marks[i].end - marks[a].start
        nxt = marks[i + 1] if i + 1 < len(marks) else None
        if nxt is None:
            groups.append((a, i))
            break
        kind_change = is_read(marks[i]) != is_read(nxt)
        if nxt.start - marks[i].end > 3.0:
            # 자막이 3초 넘게 비었다 — 음악·침묵·못 알아들은 자리. 건너서 잇지 않는다
            groups.append((a, i))
            a = i + 1
            continue
        if kind_change and dur >= 6.0:
            groups.append((a, i))
            a = i + 1
            continue
        if dur >= min_len and gaps[i] >= 1.2:
            groups.append((a, i))
            a = i + 1
            continue
        if dur >= max_len:
            # 너무 길어졌다 — 지나온 자리 중 가장 길게 쉰 곳에서 자른다
            cut, best = i, -1.0
            for j in range(a, i + 1):
                if marks[j].end - marks[a].start < min_len * 0.6:
                    continue
                if gaps[j] > best:
                    cut, best = j, gaps[j]
            groups.append((a, cut))
            a = cut + 1
    # 끝에 남은 토막은 앞 덩어리에 붙인다
    if len(groups) >= 2:
        f, l = groups[-1]
        if marks[l].end - marks[f].start < 8.0 and not is_read(marks[f]):
            pf, _ = groups[-2]
            groups[-2:] = [(pf, l)]

    s0, s1 = marks[0].start, marks[-1].end
    span = max(s1 - s0, 1.0)
    out: list[Block] = []
    for f, l in groups:
        text = " ".join(m.text for m in marks[f:l + 1])
        out.append(Block(index=len(out), first=marks[f].index, last=marks[l].index,
                         start=marks[f].start, end=marks[l].end, text=text,
                         pos=(marks[f].start - s0) / span))
    return out


# ------------------------------------------------------------------ 역할 (규칙)
def label_rules(blocks: list[Block], marks) -> None:
    """위치·낱말·문장 점수로 역할 밑그림을 매긴다. AI 가 없어도 이것만으로 돈다."""
    by_idx = {m.index: m for m in marks}
    scores = []
    for b in blocks:
        ms = [by_idx[i] for i in range(b.first, b.last + 1) if i in by_idx]
        total = sum(m.duration for m in ms) or 1.0
        read = sum(m.duration for m in ms if m.kind == "reading") / total
        aside = sum(m.duration for m in ms if m.kind == "aside") / total
        talk = [m for m in ms if m.kind not in ("reading", "aside")]
        mean = (sum(m.score * m.duration for m in talk) / max(sum(m.duration for m in talk), 1.0)
                if talk else -5.0)
        text = b.text
        appeal = len(_APPEAL.findall(text))
        problem = len(_PROBLEM.findall(text))
        story = len(_STORY.findall(text))
        closing_cue = bool(_CLOSING.search(text)) or any(m.kind == "close" for m in ms)
        has_point = any(m.kind == "point" for m in ms)

        why: list[str] = []
        if read >= 0.5:
            role = "reading"
        elif aside >= 0.5:
            role = "aside"
        elif b.pos >= 0.85 or (b.pos >= 0.6 and (closing_cue or appeal >= 2)):
            role = "closing"
            why.append("설교 끝부분의 권면" if b.pos >= 0.85 else "삶에 적용하자고 권하는 말")
        elif b.pos <= 0.3 and problem >= 2:
            role = "opening"
            why.append("앞부분에서 질문·문제를 던짐")
        elif story >= 2:
            role = "story"
            why.append("예화·경험담")
        else:
            role = "body"
        if has_point:
            why.append("목사님이 번호로 선언하신 대목")

        first = by_idx.get(b.first)
        alone = 0.75
        if first is not None and _LEANS.search(first.text.strip()):
            alone -= 0.35
            why.append("앞 말에 기대어 시작")
        elif first is not None and _LEANS_SOFT.search(first.text.strip()):
            alone -= 0.1
        if first is not None and len(first.text) < 8:
            alone -= 0.1
        b.role, b.alone = role, max(0.0, min(1.0, alone))
        b.read_share = round(read + aside, 3)
        b.score = round(mean + (1.5 if has_point else 0.0), 3)
        b.why = " · ".join(why)
        if role not in ("reading", "aside"):
            scores.append(b.score)

    # 남은 '설명' 중 점수가 높은 것을 핵심으로 본다 (상위 3할)
    if scores:
        cut = sorted(scores, reverse=True)[max(0, int(len(scores) * 0.3) - 1)]
        for b in blocks:
            if b.role == "body" and b.score >= cut and 0.15 <= b.pos <= 0.9:
                b.role = "core"
                b.why = (b.why + " · " if b.why else "") + "되풀이해 강조한 생각"


# ------------------------------------------------------------------ 역할 (AI)
AI_SYSTEM = (
    "당신은 한국 교회 주일설교 영상을 짧은 영상으로 편집하는 편집자입니다. "
    "설교를 읽고 각 부분이 설교에서 어떤 역할인지 판단합니다. "
    "설교자는 '목사님'이라고 부릅니다. 반드시 JSON 하나로만 답합니다."
)

AI_SECTION = """아래는 설교의 한 구획(설교 전체 중 {lo}~{hi}% 지점)을 덩어리로 나눈 것입니다.

덩어리마다 다음을 매겨 주세요.
- 역할: 문제제기 / 핵심 / 예화 / 적용 / 봉독 / 안내 / 기타 중 하나
  · 문제제기: 왜 이 이야기를 하는지, 듣는 사람의 고민·질문을 꺼내는 부분
  · 핵심: 이 설교가 말하려는 중심 생각을 분명하게 말하는 부분
  · 예화: 이야기·경험·예시로 풀어 주는 부분
  · 적용: 말씀을 삶에 옮기자고 권하고 청하는 부분
  · 봉독: 성경을 소리 내어 읽는 부분 / 안내: 광고·인사·진행
- 혼자: 이 덩어리만 떼어 들어도 무슨 말인지 이해되는 정도 (1~5)
- 인상: 짧은 영상에 넣었을 때 마음에 남을 만한 정도 (1~5)
그리고 이 구획 전체를 한 문장으로 요약해 주세요.

{lines}

답 모양 (이 모양 그대로, 다른 말 없이):
{{"요약": "한 문장", "덩어리": [{{"번호": {first}, "역할": "핵심", "혼자": 4, "인상": 5}}]}}"""

AI_WHOLE = """아래는 설교를 구획별로 한 문장씩 요약한 것입니다.

{lines}

1) 이 설교의 중심 메시지를 한 문장으로 써 주세요 (목사님 말투가 아니라 요약문으로).
2) 문제 제기·핵심·적용이 가장 분명한 구획 번호를 하나씩 골라 주세요.

답 모양 (이 모양 그대로, 다른 말 없이):
{{"주제": "한 문장", "문제제기": 1, "핵심": 3, "적용": 6}}"""

_ITEM = re.compile(r'"번호"\s*:\s*(\d+)[^{}]*?"역할"\s*:\s*"([^"]+)"'
                   r'(?:[^{}]*?"혼자"\s*:\s*(\d))?(?:[^{}]*?"인상"\s*:\s*(\d))?')


def _json_obj(text: str) -> dict:
    hit = re.search(r"\{.*\}", text, re.S)
    if not hit:
        return {}
    try:
        got = json.loads(hit.group(0))
        return got if isinstance(got, dict) else {}
    except ValueError:
        return {}


def _role_of(word: str) -> str | None:
    w = re.sub(r"\s+", "", word or "")
    if w in _KO_ROLE:
        return _KO_ROLE[w]
    for k, v in _KO_ROLE.items():
        if k in w:
            return v
    return None


def sections(blocks: list[Block], max_blocks: int = 8, max_chars: int = 1800) -> list[list[Block]]:
    """AI 에게 한 번에 보여 줄 구획으로 나눈다. 4B 모델이 잘 읽는 크기(약 5분)."""
    out: list[list[Block]] = []
    cur: list[Block] = []
    size = 0
    for b in blocks:
        if cur and (len(cur) >= max_blocks or size + len(b.text) > max_chars):
            out.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += len(b.text)
    if cur:
        out.append(cur)
    for k, sec in enumerate(out):
        for b in sec:
            b.section = k
    return out


def section_prompt(secs: list[list[Block]], k: int) -> tuple[str, int]:
    """구획 k 를 AI 에게 보일 글과 답 길이 한도. 학습 자료(tools/train_data.py)도 이 함수를 쓴다.

    학습할 때 본 글과 실제로 묻는 글이 한 글자라도 다르면 학습한 보람이 줄어든다.
    그래서 묻는 글은 반드시 여기서만 만든다.
    """
    sec = secs[k]
    lo = int(sec[0].pos * 100)
    hi = int(secs[k + 1][0].pos * 100) if k + 1 < len(secs) else 100
    hi = max(hi, lo + 1)
    lines = "\n".join(f"[{b.index}] {b.text}" for b in sec)
    return (AI_SECTION.format(lo=lo, hi=hi, lines=lines, first=sec[0].index),
            120 + 40 * len(sec))


def whole_prompt(summaries: list[str]) -> str:
    """구획 요약을 모아 설교 전체를 묻는 글. 빈 요약(실패한 구획)은 번호째로 건너뛴다."""
    lines = "\n".join(f"{k + 1}. {s}" for k, s in enumerate(summaries) if s)
    return AI_WHOLE.format(lines=lines)


def label_with_ai(blocks: list[Block], ask, log=None) -> tuple[int, str]:
    """로컬 AI 로 역할을 다시 매긴다. 고친 덩어리 수와 설교 주제 한 문장을 돌려준다.

    ask(system, user, max_tokens) -> str. 실패한 구획은 규칙 밑그림을 그대로 둔다.
    봉독·안내는 규칙이 더 정확하므로(성경 어미로 잡는다) AI 가 바꾸지 못하게 한다.
    """
    say = log or (lambda t: None)
    secs = sections(blocks)
    summaries: list[str] = []
    changed = 0
    by_index = {b.index: b for b in blocks}
    for k, sec in enumerate(secs):
        user, budget = section_prompt(secs, k)
        say(f"[AI] 구획 {k + 1}/{len(secs)} 읽는 중 (덩어리 {len(sec)}개)")
        try:
            reply = ask(AI_SYSTEM, user, budget)
        except Exception as exc:                          # 서버·시간초과 무엇이든
            say(f"[AI] 구획 {k + 1} 을 건너뜁니다 ({type(exc).__name__})")
            summaries.append("")
            continue
        data = _json_obj(reply)
        summaries.append(str(data.get("요약", "")).strip()[:120])
        items = data.get("덩어리") if isinstance(data.get("덩어리"), list) else None
        parsed: list[tuple[int, str, int | None, int | None]] = []
        if items:
            for it in items:
                if not isinstance(it, dict):
                    continue
                try:
                    n = int(it.get("번호"))
                except (TypeError, ValueError):
                    continue
                def _num(v):
                    try:
                        return max(1, min(5, int(v)))
                    except (TypeError, ValueError):
                        return None
                parsed.append((n, str(it.get("역할", "")), _num(it.get("혼자")), _num(it.get("인상"))))
        else:                                             # JSON 이 깨졌으면 글자에서 건진다
            for m in _ITEM.finditer(reply):
                parsed.append((int(m.group(1)), m.group(2),
                               int(m.group(3)) if m.group(3) else None,
                               int(m.group(4)) if m.group(4) else None))
        valid = {b.index for b in sec}
        for n, role_word, alone, impact in parsed:
            if n not in valid:
                continue                                  # 범위 밖 번호는 버린다
            b = by_index[n]
            role = _role_of(role_word)
            if role and b.role not in ("reading", "aside") and role not in ("reading", "aside"):
                if role != b.role:
                    changed += 1
                b.role = role
                b.by_ai = True
            if alone is not None:
                b.alone = round(0.5 * b.alone + 0.5 * (alone - 1) / 4, 3)
            if impact is not None:
                b.score = round(b.score + 0.8 * (impact - 3), 3)
                b.by_ai = True

    # 구획 요약을 모아 한 번 더 본다 (NexusSum 의 위계 단계)
    theme = ""
    if len([s for s in summaries if s]) >= 2:
        say("[AI] 구획 요약을 모아 설교 전체를 봅니다")
        try:
            data = _json_obj(ask(AI_SYSTEM, whole_prompt(summaries), 200))
        except Exception as exc:
            say(f"[AI] 전체 보기를 건너뜁니다 ({type(exc).__name__})")
            data = {}
        theme = str(data.get("주제", "")).strip()[:120]
        for key, role in (("문제제기", "opening"), ("핵심", "core"), ("적용", "closing")):
            try:
                k = int(data.get(key)) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= k < len(secs):
                for b in secs[k]:
                    if b.role == role:
                        b.score = round(b.score + 1.0, 3)   # 그 구획의 같은 역할에 힘을 싣는다
    return changed, theme


# ------------------------------------------------------------------ 조립
_SLOT_SPEC = {
    # 칸 이름, 이 칸에 맞는 역할, 목표 길이 비율, 설교 안 위치(바라는 범위)
    "two": [
        ("core", "핵심", ("core", "body"), 0.55, (0.25, 0.9)),
        ("closing", "적용", ("closing",), 0.45, (0.7, 1.0)),
    ],
    "three": [
        ("opening", "문제 제기", ("opening",), 0.25, (0.0, 0.35)),
        ("core", "핵심", ("core", "body"), 0.45, (0.25, 0.85)),
        ("closing", "적용", ("closing",), 0.30, (0.7, 1.0)),
    ],
    "one": [
        ("core", "한 대목", ("core", "closing", "story"), 1.0, (0.1, 1.0)),
    ],
}


def _cands(blocks: list[Block], roles: tuple[str, ...], want: float,
           where: tuple[float, float], key: str) -> list[Cand]:
    """칸에 넣을 후보. 맞는 역할이 모자라면 설교 안 위치로 채운다.

    AI 가 역할을 한쪽으로 몰아 매기면(예: 전부 '핵심') 문제 제기·적용 칸이
    통째로 비어 버린다. 칸이 사라지면 안 되므로 그때는 위치를 보고 고른다.
    """
    got = _cands_by(blocks, roles, want, where, key, need_role=True)
    for relax in ({"need_role": False}, {"need_role": False, "min_ratio": 0.0}):
        if len(got) >= 2:
            break
        # 맞는 역할이 없으면 위치로, 그래도 없으면(덩어리가 짧게만 나온 설교) 짧은 것도
        extra = _cands_by(blocks, roles, want, where, key, **relax)
        got += [c for c in extra if not any(c.overlaps(k) and abs(c.start - k.start) < 20
                                            for k in got)]
    return got[:4]


def _cands_by(blocks: list[Block], roles: tuple[str, ...], want: float,
              where: tuple[float, float], key: str, need_role: bool,
              min_ratio: float = 0.5) -> list[Cand]:
    out: list[Cand] = []
    n = len(blocks)
    for a in range(n):
        if blocks[a].role in ("reading", "aside"):
            continue
        for b in range(a, min(n, a + 3)):
            if blocks[b].role in ("reading", "aside"):
                break
            if b > a and blocks[b].start - blocks[b - 1].end > 3.0:
                break                         # 비어 있는 자리를 건너 잇지 않는다
            dur = blocks[b].end - blocks[a].start
            if dur > want * 1.6 + 5:
                break
            if dur < want * min_ratio:
                continue
            seg = blocks[a:b + 1]
            # 칸의 첫 역할(핵심 칸이면 '핵심')은 온전히, 나머지(설명 등)는 반만 쳐 준다.
            # 둘을 똑같이 치면 본문 안내가 섞인 '설명' 덩어리가 핵심 칸 1번이 됐다
            # (09-06 설교, 04:52 "제가 1절부터 5절까지 먼저 보겠습니다").
            match = sum(1.0 if x.role == roles[0] else 0.5 if x.role in roles else 0.0
                        for x in seg) / len(seg)
            read = sum(x.read_share * x.duration for x in seg) / max(sum(x.duration for x in seg), 1.0)
            if match == 0 and key != "core" and need_role:
                continue
            mean = sum(x.score * x.duration for x in seg) / max(sum(x.duration for x in seg), 1.0)
            mid = (seg[0].pos + seg[-1].pos) / 2
            lo, hi = where
            place = 0.0 if lo <= mid <= hi else -1.5 * min(abs(mid - lo), abs(mid - hi)) * 4
            fit = -abs(dur - want) / max(want, 1.0) * 2.0
            s = 3.0 * match + mean + 2.0 * seg[0].alone + place + fit - 4.0 * read
            preview = seg[0].text[:60]
            out.append(Cand(first=seg[0].index, last=seg[-1].index, start=seg[0].start,
                            end=seg[-1].end, score=round(s, 3), preview=preview))
    out.sort(key=lambda c: -c.score)
    # 서로 거의 같은 후보는 하나만 남긴다
    kept: list[Cand] = []
    for c in out:
        if any(c.overlaps(k) and abs(c.start - k.start) < 20 for k in kept):
            continue
        kept.append(c)
        if len(kept) >= 4:
            break
    return kept


def assemble(fmt: str, blocks: list[Block], target: float = 180.0) -> list[Slot]:
    """형식에 맞춰 칸을 만들고 칸마다 후보를 고른다. 칸끼리는 겹치지 않게."""
    spec = _SLOT_SPEC.get(fmt)
    if not spec or not blocks:
        return []
    slots: list[Slot] = []
    for key, name, roles, share, where in spec:
        want = target * share if fmt != "one" else min(target, 75.0)
        slots.append(Slot(key=key, name=name,
                          cands=_cands(blocks, roles, max(want, 15.0), where, key)))
    # 겹치지 않게 앞 칸부터 차례로 정한다
    for i, slot in enumerate(slots):
        others = [s.chosen for s in slots[:i] if s.chosen]
        for j, c in enumerate(slot.cands):
            if not any(c.overlaps(o) for o in others):
                slot.cur = j
                break
    # 시간 순서가 뒤집힌 칸(적용이 핵심보다 앞)은 그대로 둔다 — 영상은 시간순으로 이어진다
    return slots


def next_cand(slots: list[Slot], k: int) -> bool:
    """칸 k 를 다음 후보로 바꾼다. 다른 칸과 겹치는 후보는 건너뛴다."""
    slot = slots[k]
    if len(slot.cands) < 2:
        return False
    others = [s.chosen for i, s in enumerate(slots) if i != k and s.chosen]
    for step in range(1, len(slot.cands)):
        j = (slot.cur + step) % len(slot.cands)
        if not any(slot.cands[j].overlaps(o) for o in others):
            slot.cur = j
            return True
    return False


def apply_slots(slots: list[Slot], blocks: list[Block], marks) -> None:
    """칸에서 고른 덩어리의 문장을 요약에 넣는다. 봉독·안내 문장은 빼고."""
    for m in marks:
        m.chosen = False
    by_block = {b.index: b for b in blocks}
    for s in slots:
        c = s.chosen
        if not c:
            continue
        lo = by_block[c.first].first
        hi = by_block[c.last].last
        for m in marks:
            if lo <= m.index <= hi and m.kind not in ("reading", "aside"):
                m.chosen = True


def describe(blocks: list[Block]) -> str:
    from collections import Counter
    c = Counter(b.role for b in blocks)
    parts = [f"{ROLE_NAMES[r]} {c[r]}" for r in ("opening", "core", "story", "closing", "body",
                                                   "reading", "aside") if c.get(r)]
    return f"덩어리 {len(blocks)}개 — " + " · ".join(parts)


# ------------------------------------------------------------------ 학습 자료
def save_label(work_dir, marker, blocks: list[Block], marks, fmt: str, target: float,
               initial: list[bool] | None, slots: list[Slot] | None, theme: str = ""):
    """사람이 확정한 요약을 학습·채점 자료로 남긴다 (data/labels/영상.json).

    나중에 로컬 AI 를 이 교회 설교에 맞게 학습시킬 때 정답지가 된다.
    형식이 바뀌어도 쓸 수 있도록 **덩어리 역할**과 **최종 문장 선택**을 따로 적는다.
    프로그램이 처음 고른 것(initial)도 함께 적어, 사람이 무엇을 고쳤는지 남긴다.
    """
    from datetime import datetime
    from pathlib import Path

    folder = Path(work_dir) / "labels"
    folder.mkdir(parents=True, exist_ok=True)
    chosen_blocks = set()
    final = [m for m in marks if m.chosen]
    for b in blocks:
        if any(b.first <= m.index <= b.last for m in final):
            chosen_blocks.add(b.index)
    data = {
        "video_id": marker.video_id,
        "title": marker.sermon_title or marker.title,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "format": fmt,
        "target": target,
        "theme": theme,
        "blocks": [{
            "index": b.index, "start": round(b.start, 2), "end": round(b.end, 2),
            "role": b.role, "by_ai": b.by_ai, "score": b.score, "alone": b.alone,
            "chosen": b.index in chosen_blocks, "text": b.text,
        } for b in blocks],
        "sentences": [{
            "start": round(m.start, 2), "end": round(m.end, 2), "kind": m.kind,
            "auto": bool(initial[i]) if initial and i < len(initial) else None,
            "final": m.chosen, "text": m.text,
        } for i, m in enumerate(marks)],
        "slots": [{
            "key": s.key, "name": s.name, "cur": s.cur,
            "chosen": [s.chosen.start, s.chosen.end] if s.chosen else None,
        } for s in (slots or [])],
    }
    path = folder / f"{marker.video_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path
