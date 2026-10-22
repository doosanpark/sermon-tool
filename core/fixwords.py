"""받아쓰기(자막)가 잘못 알아들은 말을 바로잡는다.

논문 검토(NexusSum Preprocessor)에서 1순위로 권한 일이다. "요약 전에 입력을
정돈하면 뒤 단계가 다 좋아진다." 여기서는 요약보다 먼저 **자막 그 자체**의
문제다 — 3분 요약 영상에 자막을 태우면 오인식이 화면에 박혀 되돌릴 수 없다.

실측(2026-09-06 설교 545문장)에서 나온 것들:
    니에미아 / 느에미아 / 니에미야      → 느헤미야   (14번)
    경유하는 / 경애하는 / 경의하는       → 경외하는   (7번)
    창의직교                            → 창의진교회
    부르짓임 · 포도온 · 헤어려 · 추원   → 부르짖음 · 포도원 · 헤아려 · 축원
    산발라스 · 도비아                   → 산발랏 · 도비야
2026-07-26 설교(논문 검토 문서)에서:
    가난인의 사랑 → 하나님의 사랑,  성경의 교통하심 → 성령의 교통하심

LLM 없이 표 하나로 고친다. 고치는 말은 **틀릴 수 없는 것만** 넣는다 — 애매한 것을
억지로 고치면 맞게 받아쓴 말까지 망가뜨린다. 교회마다·설교마다 새로 나오는
오인식은 `data/고칠말.txt` 에 한 줄씩 적으면 프로그램을 고치지 않고 더할 수 있다.

몇 번을 다시 돌려도 결과가 같아야 한다(바로잡은 말이 다시 걸리지 않는다).
자막을 불러올 때마다 돌리기 때문이다.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .util import EXE_DIR, PROJECT_ROOT

# (찾을 꼴, 바꿀 말). 위에서부터 차례로 적용한다.
_BUILTIN: list[tuple[str, str]] = [
    # 성경 인물·책 — 느헤미야서 설교에서 실제로 나온 것
    (r"[뉘니느네][에이]?미안[은는]", "느헤미야는"),   # '느헤미야는' 을 '니에미안은' 으로 들음
    (r"[뉘니느네][에이]?미안을", "느헤미야를"),
    (r"[뉘니느네][에이]?미안이", "느헤미야가"),
    (r"[뉘니느네][에이]미[아야양]", "느헤미야"),     # 니에미아 · 뉘에미양 · 네이미아 …
    (r"뉘미[아야]", "느헤미야"),
    (r"느임이야", "느헤미야"),
    (r"산발라스[은는]", "산발랏은"),             # 받침이 생기므로 조사도 바꾼다
    (r"산발라스[이가]", "산발랏이"),
    (r"산발라스[을를]", "산발랏을"),
    (r"산발라스", "산발랏"),
    (r"삼발락", "산발랏"),
    (r"도비아(?=[는은의를가이와과도]|\s|$)", "도비야"),
    # 경외 — 설교에 자주 나오는데 받아쓰기가 거의 매번 틀린다
    (r"경(?:유|애|의)하(?=는|면서|며|고|지|기|여|던|실|시|십)", "경외하"),
    (r"경(?:유|애|의)함", "경외함"),
    # 축도 정형구 (고린도후서 13:13)
    (r"가난인의\s*사랑", "하나님의 사랑"),
    (r"성경의\s*교통하심", "성령의 교통하심"),
    # 자주 틀리는 낱말
    (r"부르짓임", "부르짖음"),
    (r"부르짓", "부르짖"),
    (r"포도온", "포도원"),
    (r"헤어려", "헤아려"),
    (r"추원합니다", "축원합니다"),
    (r"탐역", "탐욕"),
    # 교회 이름
    (r"창의직\s*교(?:회)?", "창의진교회"),
]

USER_FILE_NAME = "고칠말.txt"

_USER_HEADER = """\
# 자막에서 자꾸 틀리게 받아쓰는 말을 여기에 적으면, 자막을 만들거나 불러올 때
# 프로그램이 알아서 고칩니다. 한 줄에 하나씩,  틀린말 => 바른말  모양으로 적습니다.
# '#' 으로 시작하는 줄은 설명이라 무시합니다. 메모장으로 열어 고치면 됩니다.
#
# 예)
# 니에미아 => 느헤미야
# 창의직교 => 창의진교회
"""

_cache: dict[str, tuple[float, list[tuple[re.Pattern, str]]]] = {}


def _compiled_builtin() -> list[tuple[re.Pattern, str]]:
    return [(re.compile(p), r) for p, r in _BUILTIN]


_BUILT = _compiled_builtin()


def user_file(work_dir: Path) -> Path:
    return Path(work_dir) / USER_FILE_NAME


def bundled_file() -> Path | None:
    """프로그램과 함께 보내진 사전. 없으면 None.

    교회에 건넬 때 지금까지 모아 둔 오인식 사전을 같이 보낸다
    (build_exe.copy_models 가 exe 옆에 넣는다). 받는 분이 "니에미아 →
    느헤미야" 같은 것을 처음부터 다시 모으지 않아도 되게 하려는 것이다.
    """
    for base in (EXE_DIR, PROJECT_ROOT):
        for cand in (Path(base) / USER_FILE_NAME, Path(base) / "data" / USER_FILE_NAME):
            try:
                if cand.exists():
                    return cand
            except OSError:
                continue
    return None


def ensure_user_file(work_dir: Path) -> Path:
    """설명이 적힌 파일을 만들어 둔다. 사람이 찾아서 열 수 있게.

    함께 보내진 사전이 있으면 그 내용으로 시작한다. 덮어쓰지는 않는다 —
    쓰시는 분이 고쳐 둔 것을 지우면 안 된다.
    """
    path = user_file(work_dir)
    if path.exists():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        gift = bundled_file()
        if gift is not None and gift.resolve() != path.resolve():
            shutil.copyfile(gift, path)
        else:
            path.write_text(_USER_HEADER, encoding="utf-8-sig")
    except OSError:
        pass
    return path


def _read_user_file(path: Path) -> str:
    for enc in ("utf-8-sig", "cp949"):              # 메모장에서 ANSI 로 저장했을 수도 있다
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    return ""


def add_user_rule(work_dir: Path, wrong: str, right: str) -> Path:
    """편집기에서 고친 낱말을 사전 파일에 한 줄 더한다. 이미 있으면 그대로 둔다."""
    path = ensure_user_file(work_dir)
    text = _read_user_file(path) if path.exists() else _USER_HEADER
    line = f"{wrong} => {right}"
    if any(l.strip() == line for l in text.splitlines()):
        return path
    if text and not text.endswith("\n"):
        text += "\n"
    text += line + "\n"
    path.write_text(text, encoding="utf-8-sig")     # 섞이지 않게 통째로 다시 쓴다
    _cache.pop(str(path), None)
    return path


def _user_rules(work_dir: Path | None) -> list[tuple[re.Pattern, str]]:
    if work_dir is None:
        return []
    path = user_file(work_dir)
    if not path.exists():
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    key = str(path)
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    rules: list[tuple[re.Pattern, str]] = []
    lines = _read_user_file(path).splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        wrong, right = (part.strip() for part in line.split("=>", 1))
        if wrong and wrong != right:
            # 사람이 적은 것은 글자 그대로 찾는다 (정규식 기호를 몰라도 되게)
            rules.append((re.compile(re.escape(wrong)), right))
    _cache[key] = (mtime, rules)
    return rules


_BOOK_REF = re.compile(r"([가-힣]{1,6})(\s*\d+\s*장)")


def _fix_books(text: str) -> str:
    """'니에미아 5장' 처럼 장 앞에 오는 말만 성경책 이름으로 맞춘다.

    아무 낱말에나 대 보면 엉뚱한 말이 책 이름으로 바뀐다. 숫자+장 앞에 올 때만
    책 이름일 가능성이 높으므로 그때만 자모 유사도로 견준다(export.fix_book).
    """
    from .export import BOOKS, fix_book

    def repl(m: re.Match) -> str:
        word, tail = m.group(1), m.group(2)
        if word in BOOKS:
            return m.group(0)
        fixed = fix_book(word)
        return (fixed if fixed in BOOKS else word) + tail

    return _BOOK_REF.sub(repl, text)


def fix_text(text: str, work_dir: Path | None = None) -> str:
    if not text:
        return text
    out = text
    for pat, rep in _BUILT:
        out = pat.sub(rep, out)
    for pat, rep in _user_rules(work_dir):
        out = pat.sub(rep, out)
    try:
        out = _fix_books(out)
    except Exception:          # 책 이름 맞추기가 실패해도 나머지는 살린다
        pass
    return out


def fix_transcript(segments: list[dict], work_dir: Path | None = None) -> int:
    """자막 문장들을 그 자리에서 고친다. 고친 문장 수를 돌려준다."""
    changed = 0
    for seg in segments:
        old = seg.get("text") or ""
        new = fix_text(old, work_dir)
        if new != old:
            seg["text"] = new
            changed += 1
    return changed
