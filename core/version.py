"""버전을 **여기 한 곳에서만** 정한다.

왜 한 곳인가 (2026-10-20):
    전에는 버전이 installer.iss 에 "1.2.0" 으로 박혀 있는 것이 전부였다.
    화면에도 기록에도 안 나오니, 교회에 건넨 것이 어느 판인지 알 길이 없었다.
    무엇이 고쳐졌는지 물어볼 때도 서로 다른 것을 보고 이야기하게 된다.

    이제 여기 VERSION 하나만 고치면 창 제목·머리띠·실행 기록·설치본·전달
    폴더의 버전.txt 가 모두 따라간다.

올릴 때:
    쓰는 방법 자체가 달라질 만큼 바뀌면 앞자리를 올린다 (1.5 → 2.0).
    고친 것이 쓰는 분에게 보이면 가운데 자리를 올린다 (2.0 → 2.1).
    안에서만 고친 것이면 끝자리를 올린다 (2.0.0 → 2.0.1).
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

VERSION = "2.0.0"

# 이 판에서 무엇이 달라졌는지 한 줄. 화면 도움말에 쓴다.
SUMMARY = ("화면 새로 짬 · 핵심요약/3분요약 두 형식 · 이 교회 설교로 학습한 AI · "
           "낮은 사양 PC 대응 · 모델과 설명서를 함께 배포")


def build_stamp() -> str:
    """이 판이 만들어진 날. 알 수 없으면 빈 글.

    빌드본은 exe 의 날짜를 본다 — PyInstaller 가 묶은 그 시각이다.
    소스로 돌릴 때는 core/ 에서 가장 최근에 고친 파일의 날짜를 본다.
    """
    try:
        if getattr(sys, "frozen", False):
            when = Path(sys.executable).stat().st_mtime
        else:
            here = Path(__file__).resolve().parent
            when = max(f.stat().st_mtime for f in here.glob("*.py"))
        return _dt.datetime.fromtimestamp(when).strftime("%Y-%m-%d")
    except (OSError, ValueError):
        return ""


def short() -> str:
    """화면 구석에 붙일 짧은 글. 예: v1.3.0"""
    return f"v{VERSION}"


def full() -> str:
    """사람이 읽을 한 줄. 예: v1.3.0 (2026-10-20 만듦)"""
    day = build_stamp()
    return f"v{VERSION}" + (f" ({day} 만듦)" if day else "")
