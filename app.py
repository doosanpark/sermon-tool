#!/usr/bin/env python3
"""창의진교회 설교영상 편집기 — 구간 찾기 · 미세 조정 · 내보내기.

    pip install PySide6
    python app.py
"""
from __future__ import annotations

import contextlib
import copy
import gc
import io
import re
import sys
import textwrap
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtCore import QUrl as _QUrl  # noqa: F401
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QKeySequence,
                           QPainter, QPalette, QPen)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSlider, QSplitter,
    QTabWidget, QVBoxLayout, QWidget,
)

from core import export as export_mod
from core import blocks as blocks_mod
from core import fetch, fixwords, highlight as hl_mod, pauses as pause_mod, transcribe
import theme as theme_mod
import ui_header
from core import machine as machine_mod
from core import version as version_mod
from core.config import Config
from core.detect import apply_service_result, detect, format_detection
from core.diarize import diarize
from core.voiceprint import Embedder, Voiceprint, analyze_media
from core.marker import Marker
from core.refine import refine_benediction, refine_sermon
from core.util import ToolError, ffprobe_duration, hms
from ui_summary import SummaryTab

LABEL_KO = {"sermon": "설교", "benediction": "축도", "clip": "구간"}


def fmt(seconds: float) -> str:
    """00:37:37.4 — 미세 조정이 보이도록 소수 한 자리까지."""
    seconds = max(0.0, float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:04.1f}"


def parse_time(text: str) -> float:
    text = text.strip()
    if not text:
        raise ValueError("시각이 비어 있습니다.")
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return total


# ----------------------------------------------------------------- 백그라운드
class Worker(QThread):
    log = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        emitter = self.log

        class _Stream(io.TextIOBase):
            def write(self, text):
                text = text.rstrip()
                if text:
                    emitter.emit(text)
                return len(text)

        try:
            with contextlib.redirect_stdout(_Stream()):
                result = self._fn()
            self.done.emit(result)
        except ToolError as exc:
            self.failed.emit(str(exc))
        except Exception:
            self.failed.emit(traceback.format_exc(limit=6))


# ----------------------------------------------------------------- 타임라인
# 지금 쓰는 화면 색. apply_theme() 이 갈아 끼우고, 직접 그리는 위젯들이 여기서 읽는다.
# 전역 하나로 두는 이유: RangeBar·ZoomBar 는 MainWindow 보다 먼저 만들어질 수 있고,
# 위젯마다 테마를 들고 다니면 테마를 바꿀 때 한 곳이라도 빠지면 화면이 반쪽만 바뀐다.
T = theme_mod.tokens("dark")


def _c(hexcolor: str, alpha: int = 255) -> QColor:
    c = QColor(hexcolor)
    if alpha != 255:
        c.setAlpha(alpha)
    return c


class RangeBar(QWidget):
    """전체 길이 위에 구간과 재생 위치를 그린다. 선택된 구간은 진하게."""

    seeked = Signal(float)
    edge_dragged = Signal(str, float)
    released = Signal()

    GRAB_PX = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(44)
        self.setMouseTracking(True)
        self.duration = 0.0
        self.ranges: list[tuple[float, float, str]] = []
        self.position = 0.0
        self.selected = -1
        self.active_edge = "start"
        self._drag: str | None = None

    def set_data(self, duration, ranges, selected=-1):
        self.duration = max(float(duration), 0.0)
        self.ranges = ranges
        self.selected = selected
        self.update()

    def set_position(self, seconds: float) -> None:
        self.position = seconds
        self.update()

    def _x(self, seconds: float) -> float:
        return 0.0 if self.duration <= 0 else self.width() * (seconds / self.duration)

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), _c(T.tl_bg))

        # 눈금 — 2분마다 짧게, 10분마다 길게 (FilmCraft 눈금자처럼 두 단으로).
        # 한 가지 길이로만 그리면 80분 영상에서 어디가 어디인지 가늠이 안 된다.
        if self.duration > 0:
            for step, tick, alpha in ((120, 5, 150), (600, 10, 255)):
                p.setPen(QPen(_c(T.tl_ruler_tick, alpha), 1))
                t = step
                while t < self.duration:
                    x = int(self._x(t))
                    p.drawLine(x, self.height() - tick, x, self.height())
                    t += step

        for i, (start, end, label) in enumerate(self.ranges):
            x0, x1 = self._x(start), self._x(end)
            base = _c(T.sermon_clip if label != "benediction" else T.bene_clip)
            if i != self.selected and self.selected >= 0:
                # 고르지 않은 구간은 흐리게. 어두운 화면에서는 밝히면 더 튀므로 어둡게 한다.
                base = base.darker(165) if T.dark else base.lighter(155)
            p.fillRect(int(x0), 6, max(int(x1 - x0), 3), self.height() - 20, base)

            if x1 - x0 > 42:
                p.setPen(_c(T.clip_text))
                f = QFont()
                f.setPointSize(8)
                p.setFont(f)
                p.drawText(int(x0) + 5, 20, LABEL_KO.get(label, label))

        # 선택된 구간의 양 끝 손잡이 — 구간이 1픽셀이어도 보이게
        if 0 <= self.selected < len(self.ranges):
            s, e, _ = self.ranges[self.selected]
            for tsec, mark in ((s, "["), (e, "]")):
                x = int(self._x(tsec))
                p.setPen(QPen(_c(T.clip_selected_border), 2))
                p.drawLine(x, 2, x, self.height() - 12)
                p.fillRect(x - 3, 2, 6, 8, _c(T.clip_selected_border))

        if self.duration > 0:
            # 재생 머리 — FilmCraft 와 같은 파란 선에, 위쪽에 작은 머리를 붙인다.
            # 선만 있으면 구간 색과 겹쳐 어디가 지금인지 눈에 안 들어온다.
            x = int(self._x(self.position))
            p.setPen(QPen(_c(T.playhead), 2))
            p.drawLine(x, 0, x, self.height())
            p.fillRect(x - 5, 0, 10, 5, _c(T.playhead))

    # --- 경계 드래그 -------------------------------------------------
    MIN_DRAG_PX = 24

    def _edge_at(self, x: float) -> str | None:
        """가까운 경계를 돌려준다.

        구간이 화면에서 너무 좁으면(1.4시간을 800픽셀에 담으면 1분이 1픽셀)
        경계를 정확히 잡을 수 없고, 실수로 끌면 구간이 통째로 뭉개진다.
        그래서 좁을 때는 드래그를 막고, 정밀 조정 바나 Shift+클릭을 쓰게 한다.
        """
        if not (0 <= self.selected < len(self.ranges)):
            return None
        start, end, _ = self.ranges[self.selected]
        if self._x(end) - self._x(start) < self.MIN_DRAG_PX:
            return None
        ds, de = abs(x - self._x(start)), abs(x - self._x(end))
        if min(ds, de) > self.GRAB_PX:
            return None
        return "start" if ds <= de else "end"

    def _time_at(self, x: float) -> float:
        return max(0.0, min(self.duration, self.duration * x / max(self.width(), 1)))

    def mouseMoveEvent(self, event):  # noqa: N802
        x = event.position().x()
        if self._drag:
            self.edge_dragged.emit(self._drag, self._time_at(x))
            return
        if event.modifiers() & Qt.ShiftModifier:
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(Qt.SizeHorCursor if self._edge_at(x) else Qt.PointingHandCursor)

    def mousePressEvent(self, event):  # noqa: N802
        if self.duration <= 0 or self.width() <= 0:
            return
        x = event.position().x()
        # Shift 를 누른 채 누르면 조정 중인 경계를 그 자리로 곧장 옮긴다.
        # 구간이 좁아 손잡이를 못 잡을 때 쓰는 탈출구.
        if event.modifiers() & Qt.ShiftModifier:
            self._drag = self.active_edge
            self.edge_dragged.emit(self.active_edge, self._time_at(x))
            return
        edge = self._edge_at(x)
        if edge:
            self._drag = edge
        else:
            self.seeked.emit(self._time_at(x))

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._drag:
            self.released.emit()
        self._drag = None


class ZoomBar(QWidget):
    """경계 주변만 확대해서 보여주는 정밀 조정 바.

    전체 타임라인은 1시간 반을 800픽셀에 담으므로 1픽셀이 약 7초다. 미세 조정이
    물리적으로 불가능하다. 이 바는 기본 ±15초만 보여주므로 1픽셀이 0.04초다.
    막대를 좌우로 끌면 그 자리가 곧 컷 지점이다. 휠로 확대/축소한다.
    """

    edge_dragged = Signal(str, float)
    seeked = Signal(float)
    released = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(72)
        self.setMouseTracking(True)
        self.setCursor(Qt.SizeHorCursor)
        self.which = "start"
        self.edge_time = 0.0
        self.clip_start = 0.0
        self.clip_end = 0.0
        self.center = 0.0          # 창의 중심. 경계와 분리해 둬야 드래그가 안 튄다
        self.position = 0.0
        self.span = 30.0
        self.inside_left = False
        self._drag = False
        self._grab_offset = 0.0

    def set_clip(self, start: float, end: float) -> None:
        self.clip_start, self.clip_end = float(start), float(end)
        self.update()

    def set_edge(self, which: str, edge_time: float, recenter: bool | None = None) -> None:
        self.which = which
        self.edge_time = float(edge_time)
        self.inside_left = which == "end"
        if recenter is None:
            recenter = not self._drag      # 끄는 중에는 창을 고정한다
        if recenter:
            self.center = self.edge_time
        self.update()

    def set_position(self, seconds: float) -> None:
        self.position = seconds
        self.update()

    def _lo(self) -> float:
        return self.center - self.span / 2

    def _x(self, seconds: float) -> float:
        return self.width() * (seconds - self._lo()) / self.span

    def _t(self, x: float) -> float:
        return max(0.0, self._lo() + self.span * x / max(self.width(), 1))

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), _c(T.control_well))

        # 구간 안쪽을 칠해 어디가 남는지 보여준다
        ex = int(self._x(self.edge_time))
        keep = _c(T.sermon_clip, 90 if T.dark else 55)
        if self.inside_left:
            p.fillRect(0, 0, max(ex, 0), h - 16, keep)
        else:
            p.fillRect(max(ex, 0), 0, max(w - ex, 0), h - 16, keep)

        f = QFont()
        f.setPointSize(7)
        p.setFont(f)
        lo, hi = self._lo(), self._lo() + self.span
        tick = int(lo) - 1
        while tick <= hi + 1:
            x = int(self._x(tick))
            if 0 <= x <= w:
                major = tick % 5 == 0
                p.setPen(QPen(_c(T.tl_ruler_tick), 1))
                p.drawLine(x, h - 16, x, h - (4 if major else 10))
                if major:
                    p.setPen(_c(T.tl_ruler_text))
                    p.drawText(x + 2, h - 4, fmt(max(tick, 0)))
            tick += 1

        px = int(self._x(self.position))
        if 0 <= px <= w:
            p.setPen(QPen(_c(T.playhead), 1, Qt.DashLine))
            p.drawLine(px, 0, px, h - 16)

        # 반대쪽 경계도 창 안에 있으면 흐리게 그린다
        other_t = self.clip_end if self.which == "start" else self.clip_start
        other_x = int(self._x(other_t))
        if 0 <= other_x <= w:
            p.setPen(QPen(_c(T.text_faint), 2, Qt.DotLine))
            p.drawLine(other_x, 0, other_x, h - 16)
            p.setPen(_c(T.text_faint))
            p.drawText(other_x + 4, 26, "끝" if self.which == "start" else "시작")

        # 조정 중인 경계 — 잡기 쉽게 크게 그린다
        p.setPen(QPen(_c(T.accent), 3))
        p.drawLine(ex, 18, ex, h - 16)
        p.fillRect(ex - 9, 18, 18, 16, _c(T.accent))
        p.setPen(_c(T.text_on_accent))
        f2 = QFont(); f2.setPointSize(8); f2.setBold(True)
        p.setFont(f2)
        p.drawText(ex - 6, 31, "↔")
        p.setFont(f)

        # 항상 보이는 요약 — 시작과 끝을 같이 확인할 수 있어야 한다
        length = max(self.clip_end - self.clip_start, 0.0)
        mark_s = "▶" if self.which == "start" else "  "
        mark_e = "▶" if self.which == "end" else "  "
        p.setPen(_c(T.text))
        p.drawText(6, 14, f"{mark_s}시작 {fmt(self.clip_start)}    "
                          f"{mark_e}끝 {fmt(self.clip_end)}    "
                          f"길이 {length/60:.1f}분    (±{self.span/2:.0f}초 · 휠로 확대)")

    HANDLE_PX = 14

    def _on_handle(self, x: float) -> bool:
        return abs(x - self._x(self.edge_time)) <= self.HANDLE_PX

    def mousePressEvent(self, event):  # noqa: N802
        x = event.position().x()
        if self._on_handle(x):
            self._drag = True          # 손잡이를 잡았을 때만 끌린다
            self._grab_offset = x - self._x(self.edge_time)
        else:
            self.seeked.emit(self._t(x))   # 그 외에는 재생 위치 이동

    def mouseMoveEvent(self, event):  # noqa: N802
        x = event.position().x()
        if not self._drag:
            self.setCursor(Qt.SizeHorCursor if self._on_handle(x) else Qt.PointingHandCursor)
            return
        margin = 26
        if x < margin:
            self.center = max(0.0, self.center - self.span * 0.03)
        elif x > self.width() - margin:
            self.center += self.span * 0.03
        self.edge_dragged.emit(self.which, self._t(x - self._grab_offset))

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._drag:
            self.released.emit()
        self._drag = False
        # 창을 다시 가운데로 되돌리지 않는다. 되돌리면 방금 맞춘 게
        # 원위치로 튀는 것처럼 보여서 조정이 안 되는 느낌을 준다.
        self.update()

    def wheelEvent(self, event):  # noqa: N802
        step = 1.25 if event.angleDelta().y() < 0 else 0.8
        self.span = max(4.0, min(300.0, self.span * step))
        self.update()


# ----------------------------------------------------------------- 메인 창
class MainWindow(SummaryTab, QWidget):
    # 3분 요약에서 문장을 무엇으로 고를지. 뒤로 갈수록 잘 고르고 느리다.

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"창의진교회 설교영상 편집기 {version_mod.short()}")
        self._fit_to_screen()

        self.cfg = Config()
        self.cfg.ensure_dirs()
        self._log_path = self._open_run_log()
        fixwords.ensure_user_file(self.cfg.work_dir)   # 자막 오인식 사전 (고칠말.txt)
        self.marker: Marker | None = None
        self.transcript: list[dict] = []
        self.worker: Worker | None = None
        self.media_path: Path | None = None
        self.stop_at: float | None = None
        self._small_screen = False
        self.edit_edge = "start"
        self.intro_path = ""
        self._last_seek = 0.0
        self._undo: list[list[dict]] = []
        self._drag_snapshot = False

        # 3분 요약
        self.sum_marks: list = []
        self.sum_outline = hl_mod.Outline()
        self.sum_pauses: list = []
        self.sum_undo: list[list[bool]] = []
        self.sum_blocks: list = []          # 덩어리 (core/blocks.py)
        self.sum_slots: list = []           # 형식의 칸과 칸마다 후보
        self.sum_initial: list[bool] = []   # 프로그램이 처음 고른 것 — 학습 자료에 남긴다
        self.sum_theme = ""                 # AI 가 본 설교 주제 한 문장

        self._build_ui()
        self._compact_for_small_screen()
        self._build_shortcuts()
        self._set_busy(False)
        self._load_prefs()
        self._refresh_header_theme()
        self._startup_check()
        self._update_hint()

    def _compact_for_small_screen(self) -> None:
        """세로가 좁은 노트북에서 편집 패널이 잘리지 않게 한 번 더 줄인다.

        1366x768 에서는 편집 패널에 쓸 수 있는 높이가 470px 남짓이다.
        안내문을 접고 버튼 높이를 몇 픽셀씩 깎으면 스크롤 없이 들어간다.
        """
        if not self._small_screen:
            return

        self.guide.setVisible(False)          # 같은 내용이 화면 아래 안내줄에도 있다
        self.title_label.setMaximumHeight(20)
        self.tabs.setMinimumHeight(110)
        self.video.setMinimumHeight(150)
        self.btn_export.setFixedHeight(36)
        self.btn_mp3.setFixedHeight(36)

        for edit in (self.start_edit, self.end_edit):
            edit.setFixedHeight(26)
            edit.setStyleSheet(
                f"font-size:{T.font_big_pt}pt; font-weight:bold;"
                f"background:{T.field_bg}; color:{T.timecode};")
        for b in (self.btn_start_here, self.btn_end_here):
            b.setFixedHeight(26)
        for b in (self.btn_start_check, self.btn_end_check):
            b.setFixedHeight(22)
        for b in (self.btn_undo, self.btn_restore_ai):
            b.setFixedHeight(24)

        self.log("작은 화면이라 편집 패널을 줄였습니다. 창을 최대화하면 더 편합니다.")

    def _fit_to_screen(self) -> None:
        """모니터를 넘지 않게 창 크기를 정한다.

        1366x768 노트북에서도 창이 화면 밖으로 나가지 않아야 한다.
        작업표시줄을 뺀 실제 사용 가능 영역을 기준으로 삼는다.
        """
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        if area is None:
            self._small_screen = False
            self.resize(1200, 800)
            return
        self._small_screen = area.height() < 900
        w = min(1360, int(area.width() * 0.95))
        h = min(960, int(area.height() * 0.94))
        self.resize(w, h)
        self.move(area.left() + (area.width() - w) // 2,
                  area.top() + max((area.height() - h) // 2, 0))

    def _keep_on_screen(self) -> None:
        """창 바깥 테두리(제목 표시줄 포함)가 작업 표시줄 위에 들어오게 고친다.

        resize() 는 창 안쪽 크기만 정하고 제목 표시줄(배율 150% 에서 약 45px)은
        그 위에 따로 붙는다. 그래서 계산상 꼭 맞아도 실제로는 아래가 작업 표시줄
        뒤로 넘쳤다. 화면에 띄운 뒤 실제 바깥 크기를 재서 넘친 만큼 줄이고 올린다.
        """
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None or self.isMaximized():
            return
        area = screen.availableGeometry()
        frame = self.frameGeometry()
        extra = frame.height() - self.height()          # 제목 표시줄 + 테두리
        if frame.height() > area.height():
            self.resize(self.width(), max(area.height() - extra, self.minimumHeight()))
            frame = self.frameGeometry()
        top = frame.top()
        if frame.bottom() > area.bottom():
            top = max(area.top(), area.bottom() - frame.height() + 1)
        if top < area.top():
            top = area.top()
        if top != frame.top():
            self.move(frame.left(), top)

    # ============================================================== UI
    # 머리띠의 모드 탭 — **사용자가 작업하는 것** 두 가지다.
    #
    # '만들기' 를 모드로 두었다가 뺐다 (2026-10-20). 이 프로그램에 만드는 화면은
    # 따로 없다 — 만들기 단추는 하던 일 옆에 있다. 구간을 정했으면 거기서
    # [설교 영상 만들기], 요약을 골랐으면 거기서 [요약 영상 만들기] 를 누른다.
    # 별도 모드로 두니 하는 일이 '로그 띄우기' 뿐이라 용도가 없었다.
    #
    # 진행 로그도 모드가 아니라 가끔 들여다보는 것이라 머리띠의 [기록] 단추로 옮겼다.
    MODES = ("설교 찾기", "요약")
    TAB_LOG = 2

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 맨 위 머리띠 (FilmCraft 짜임새). 창 끝까지 닿아야 하므로 바깥 여백을 0 으로
        # 두고, 나머지 내용은 안쪽 칸에 넣어 여백을 준다.
        self.header = ui_header.HeaderBar(
            f"창의진교회 설교영상 편집기  {version_mod.short()}", list(self.MODES))
        self.header.mode_clicked.connect(self._on_mode_clicked)
        self.header.fullscreen_clicked.connect(self._toggle_fullscreen)
        self.header.log_clicked.connect(self._on_log_clicked)
        root.addWidget(self.header)

        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(8, 8, 8, 6)
        lay.setSpacing(8)
        root.addWidget(inner, 1)

        self.toolbar_box = self._build_toolbar()
        lay.addWidget(self.toolbar_box)

        # [만들기] 모드에서 만들기 단추까지 내려 주려면 나중에도 잡을 수 있어야 한다
        editor_scroll = self.editor_scroll = QScrollArea()
        editor_scroll.setWidget(self._build_editor())
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setFrameShape(QFrame.NoFrame)
        editor_scroll.setMinimumWidth(360)
        editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 영상 칸도 스크롤 안에 둔다. 영상·버튼·막대를 합친 최소 높이가 450px 남짓이라,
        # 화면 배율 150% 노트북(쓸 수 있는 높이 약 670px)에서는 창이 화면보다 커져
        # 아래 탭이 작업 표시줄 뒤로 숨었다. 이렇게 하면 창은 화면 안에 들어가고,
        # 정말 모자랄 때만 영상 칸에 세로 스크롤이 생긴다.
        player_scroll = QScrollArea()
        player_scroll.setWidget(self._build_player())
        player_scroll.setWidgetResizable(True)
        player_scroll.setFrameShape(QFrame.NoFrame)
        player_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 패널로 감싼다 — 영상 칸에는 제목 띠를, 편집 칸에는 테두리만.
        # 편집 칸은 안에 [2. 설교 구간 정하기] 라는 제목이 이미 있어 띠를 또 올리면
        # 같은 말이 두 번 적힌다. 지금 만지고 있는 칸에는 파란 테두리가 생긴다.
        middle = QSplitter(Qt.Horizontal)
        middle.addWidget(ui_header.Panel("영상", player_scroll))
        middle.addWidget(ui_header.Panel(None, editor_scroll))
        middle.setSizes([880, 420])

        self.tabs = QTabWidget()
        self.subtitle_list = QListWidget()
        self.subtitle_list.setAlternatingRowColors(True)
        self.subtitle_list.setEditTriggers(QListWidget.DoubleClicked)
        self.subtitle_list.itemChanged.connect(self._on_subtitle_edited)
        self.subtitle_list.setToolTip(
            "한 번 클릭: 그 시각으로 이동해서 들어보기\n"
            "두 번 클릭: 받아쓰기가 틀린 글자를 그 자리에서 고치기\n"
            "고친 내용은 .srt 와 영상에 넣는 자막에 그대로 들어갑니다.")
        self.tabs.addTab(self.subtitle_list, "자막 고치기")
        self.summary_tab = self._build_summary_tab()
        self.tabs.addTab(self.summary_tab, "요약")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setStyleSheet(
            "font-family:Consolas,'D2Coding',monospace;"
            f"font-size:{T.font_mono_pt}pt;"
            f"background:{T.field_bg}; color:{T.text};")
        self.tabs.addTab(self._build_log_tab(), "진행 로그")
        self.tabs.setMinimumHeight(130)

        # 위(영상·편집)와 아래(탭)를 끌어서 나눌 수 있게 한다. 노트북처럼 세로가 좁으면
        # 고정 비율로는 [3분 요약] 탭의 목록과 버튼이 잘린다 (1366x768 에서 목록 한 줄).
        self.vsplit = QSplitter(Qt.Vertical)
        self.vsplit.addWidget(middle)
        # 아래 칸 — 탭 줄은 숨긴다. 머리띠의 모드 탭이 같은 것을 가리켜 탭이 두 줄로
        # 겹쳐 보였다 (2026-10-20). 지금 무엇을 보고 있는지는 패널 제목 띠에 적는다.
        self.tabs.tabBar().hide()
        self.bottom_panel = ui_header.Panel(self.tabs.tabText(0), self.tabs)
        self.vsplit.addWidget(self.bottom_panel)
        self.vsplit.setChildrenCollapsible(False)
        self.vsplit.setStretchFactor(0, 5)
        self.vsplit.setStretchFactor(1, 2)
        lay.addWidget(self.vsplit, 1)
        self._split_before_summary: list[int] | None = None
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.hint = QLabel()
        self.hint.setStyleSheet(f"color:{T.text_dim}; padding:2px;")
        lay.addWidget(self.hint)

        # 미디어
        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)
        self.player.setVideoOutput(self.video)
        self.audio_out.setVolume(0.85)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(lambda ms: self.slider.setRange(0, ms))

        self.subtitle_list.itemClicked.connect(
            lambda it: self._seek(float(it.data(Qt.UserRole))))

    def _on_tab_changed(self, _index: int) -> None:
        """[3분 요약] 탭을 열면 아래 칸을 넓히고, 다른 탭으로 가면 원래대로 돌린다.

        요약은 문장 목록을 읽으며 고르는 일이라 아래 칸이 넓어야 한다. 그 동안은
        구간 경계를 만지지 않으므로 정밀 조정 줄을 접고 영상 칸을 줄여 높이를 낸다.
        """
        if hasattr(self, "header"):
            # 진행 로그를 보는 중이면 어느 모드도 고르지 않은 상태로 둔다 (-1).
            self.header.set_mode(_index if _index < len(self.MODES) else -1)
            self.bottom_panel.set_title(self.tabs.tabText(_index))
        if not hasattr(self, "vsplit"):
            return
        on_summary = self.tabs.currentWidget() is self.summary_tab
        if on_summary and self._split_before_summary is None:
            self._split_before_summary = self.vsplit.sizes()
            self.fine_box.setVisible(False)
            self.video.setMinimumHeight(90)
            total = sum(self.vsplit.sizes())
            want = min(max(360, int(total * 0.5)), total - 120)
            if self.tabs.height() < want:
                self.vsplit.setSizes([total - want, want])
        elif not on_summary and self._split_before_summary is not None:
            self.fine_box.setVisible(True)
            self.video.setMinimumHeight(150 if self._small_screen else 180)
            self.vsplit.setSizes(self._split_before_summary)
            self._split_before_summary = None

    def _on_mode_clicked(self, index: int) -> None:
        """머리띠의 모드 탭을 눌렀을 때 — 아래 칸의 내용을 바꾼다."""
        if 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)

    def _on_log_clicked(self) -> None:
        """[기록] 단추 — 아래 칸에 진행 로그를 띄운다. 다시 누르면 하던 데로 돌아간다."""
        if self.tabs.currentIndex() == self.TAB_LOG:
            self.tabs.setCurrentIndex(max(self.header.current, 0))
        else:
            self.tabs.setCurrentIndex(self.TAB_LOG)

    def _toggle_fullscreen(self) -> None:
        """머리띠 오른쪽 꺾쇠 단추 — 전체 화면으로 갔다 돌아온다."""
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _build_toolbar(self) -> QWidget:
        box = QGroupBox("1. 영상 준비")
        lay = QHBoxLayout(box)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("유튜브 주소를 여기에 붙여넣으세요")
        self.url_edit.setMinimumHeight(36)
        self.url_edit.setStyleSheet(f"font-size:{T.font_big_pt}pt; padding:4px;")
        self.quality_box = QComboBox()
        self.quality_box.addItems(["원본", "720p", "360p"])
        self.btn_fetch = QPushButton("영상 가져오기")
        self.btn_open = QPushButton("내 컴퓨터에서 열기")
        self.btn_manual = QPushButton("직접 정하기")
        self.btn_quick = QPushButton("자동으로 찾기")
        self.stt_check = QCheckBox("자막도")
        self.btn_load = QPushButton("이전 작업 불러오기")
        for b in (self.btn_fetch, self.btn_open):
            b.setMinimumHeight(38)
            b.setProperty("primary", True)
        for b in (self.btn_manual, self.btn_quick, self.btn_load):
            b.setMinimumHeight(34)

        self.btn_fetch.setToolTip("유튜브에서 영상을 내려받아 재생합니다. 이미 받았으면 그 파일을 씁니다.")
        self.btn_manual.setToolTip(
            "영상을 보면서 시작과 끝을 직접 정합니다. 가장 빠릅니다 (몇 분).")
        self.btn_quick.setToolTip(
            "목사님 성문으로 설교와 축도를 자동으로 찾습니다.\n"
            "80분 영상이 CPU에서 1~2분이면 끝납니다.\n"
            "성문이 없으면 화자 분리로 넘어가며, 그때는 30~90분 걸립니다.")
        self.stt_check.setToolTip(
            "자막(.srt)까지 만들고 경계를 문장 단위로 다듬습니다.\n"
            "CPU 에서 20~40분 더 걸립니다.")
        self.btn_open.setToolTip("이미 가지고 있는 영상·오디오 파일을 열어 재생합니다.")

        lay.addWidget(self.url_edit, 1)
        lay.addWidget(QLabel("화질"))
        lay.addWidget(self.quality_box)
        lay.addWidget(self.btn_fetch)
        lay.addWidget(self.btn_open)
        lay.addSpacing(14)
        lay.addWidget(self.btn_manual)
        lay.addWidget(self.btn_quick)
        lay.addWidget(self.stt_check)
        lay.addWidget(self.btn_load)

        self.btn_fetch.clicked.connect(self.on_fetch)
        self.btn_open.clicked.connect(self.on_open_file)
        self.btn_manual.clicked.connect(self.on_manual)
        self.btn_quick.clicked.connect(self.on_quick)
        self.btn_load.setToolTip("저장해 둔 구간 정보를 되살립니다. 영상이 아니라 작업 내용입니다.")
        self.btn_load.clicked.connect(self.on_load)
        return box

    def _build_player(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)

        self.video = QVideoWidget()
        self.video.setMinimumHeight(180)
        self.video.setStyleSheet(f"background:{T.media_bg};")
        lay.addWidget(self.video, 1)

        nav = QHBoxLayout()
        for text, delta, tip in [
            ("◀◀ 10초", -10, "J"), ("◀ 1초", -1, ""), ("◀ 0.1", -0.1, ""),
        ]:
            b = QPushButton(text)
            b.setMinimumHeight(34)
            b.clicked.connect(lambda _, d=delta: self._nudge_play(d))
            if tip:
                b.setToolTip(f"단축키 {tip}")
            nav.addWidget(b)

        self.btn_play = QPushButton("▶ 재생")
        self.btn_play.setMinimumWidth(120)
        self.btn_play.setMinimumHeight(38)
        self.btn_play.setStyleSheet(
            f"font-size:{T.font_big_pt}pt; font-weight:bold;")
        self.btn_play.setToolTip("단축키 Space 또는 K")
        nav.addWidget(self.btn_play)

        for text, delta, tip in [
            ("0.1 ▶", 0.1, ""), ("1초 ▶", 1, ""), ("10초 ▶▶", 10, "L"),
        ]:
            b = QPushButton(text)
            b.setMinimumHeight(34)
            b.clicked.connect(lambda _, d=delta: self._nudge_play(d))
            if tip:
                b.setToolTip(f"단축키 {tip}")
            nav.addWidget(b)
        lay.addLayout(nav)

        row = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal)
        self.time_label = QLabel("00:00:00.0 / 00:00:00.0")
        self.time_label.setMinimumWidth(190)
        row.addWidget(self.slider, 1)
        row.addWidget(self.time_label)
        lay.addLayout(row)

        self.range_bar = RangeBar()
        lay.addWidget(self.range_bar)

        self.span_label = QLabel("구간 없음")
        self.span_label.setStyleSheet(
            f"font-size:{T.font_big_pt + 1}pt; font-weight:bold; padding:6px;"
            f"color:{T.text}; background:{T.pill_active_bg};"
            f"border:1px solid {T.control_border}; border-radius:{T.radius_sm}px;")
        lay.addWidget(self.span_label)

        edge_row = QHBoxLayout()
        self.btn_edge_start = QPushButton("◀ 시작 조정")
        self.btn_edge_end = QPushButton("끝 조정 ▶")
        for b in (self.btn_edge_start, self.btn_edge_end):
            b.setCheckable(True)
            b.setMaximumWidth(140)
        self.btn_edge_start.setChecked(True)
        edge_row.addWidget(QLabel("정밀 조정"))
        edge_row.addWidget(self.btn_edge_start)
        edge_row.addWidget(self.btn_edge_end)
        edge_row.addStretch(1)
        self.zoom_hint = QLabel(
            "파란 ↔ 손잡이를 잡아 끌면 0.1초 단위 · 휠로 확대 · Ctrl+←/→ 로도 조정")
        self.zoom_hint.setStyleSheet(f"color:{T.text_faint};")
        edge_row.addWidget(self.zoom_hint)

        # 정밀 조정 줄과 확대 막대는 한 묶음으로 둔다. [3분 요약] 탭을 볼 때는
        # 구간 경계를 만질 일이 없으므로 접어서 그 높이를 요약 목록에 준다.
        self.fine_box = QWidget()
        fine = QVBoxLayout(self.fine_box)
        fine.setContentsMargins(0, 0, 0, 0)
        fine.addLayout(edge_row)
        self.zoom_bar = ZoomBar()
        fine.addWidget(self.zoom_bar)
        lay.addWidget(self.fine_box)

        self.btn_edge_start.clicked.connect(lambda: self._select_edge("start"))
        self.btn_edge_end.clicked.connect(lambda: self._select_edge("end"))
        self.zoom_bar.edge_dragged.connect(self._on_edge_dragged)
        self.zoom_bar.seeked.connect(self._seek)
        self.range_bar.edge_dragged.connect(self._on_edge_dragged)
        self.zoom_bar.released.connect(self._on_edge_released)
        self.range_bar.released.connect(self._on_edge_released)
        self.btn_play.clicked.connect(self.toggle_play)
        self.slider.sliderMoved.connect(lambda v: self.player.setPosition(v))
        self.range_bar.seeked.connect(self._seek)
        return panel

    def _build_editor(self) -> QWidget:
        box = QGroupBox("2. 설교 구간 정하기")
        lay = QVBoxLayout(box)
        lay.setSpacing(5)
        lay.setContentsMargins(9, 11, 9, 7)

        self.title_label = QLabel("영상 없음")
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet(
            f"font-weight:bold; font-size:{T.font_small_pt}pt;")
        self.title_label.setMaximumHeight(28)
        lay.addWidget(self.title_label)

        title_box = QHBoxLayout()
        title_box.setSpacing(5)
        title_box.addWidget(QLabel("설교 제목"))
        self.sermon_title_edit = QLineEdit()
        self.sermon_title_edit.setPlaceholderText("예: 하나님이 세상을 이처럼 사랑하사")
        self.sermon_title_edit.setFixedHeight(28)
        self.sermon_title_edit.setToolTip(
            "여기 적은 제목이 만들어질 영상의 파일 이름이 됩니다.\n"
            "비워 두면 날짜와 영상 번호로 저장됩니다.")
        title_box.addWidget(self.sermon_title_edit, 1)
        lay.addLayout(title_box)

        self.guide = QLabel(
            "영상을 보다가 설교가 시작되는 곳에서 [여기가 시작],\n"
            "끝나는 곳에서 [여기가 끝] 을 누르세요.")
        guide = self.guide
        guide.setStyleSheet(
            f"background:{T.row_alt}; border:1px solid {T.separator};"
            f"border-left:3px solid {T.warn}; border-radius:{T.radius_sm}px;"
            f"padding:4px 6px; color:{T.text_dim}; font-size:{T.font_small_pt}pt;")
        guide.setMaximumHeight(42)
        lay.addWidget(guide)

        lay.addWidget(self._edge_group("start"))
        lay.addWidget(self._edge_group("end"))

        self.conf_label = QLabel("")
        self.conf_label.setStyleSheet(
            f"color:{T.text_faint}; font-size:{T.font_small_pt}pt;")
        self.conf_label.setMaximumHeight(18)
        lay.addWidget(self.conf_label)

        undo_row = QHBoxLayout()
        undo_row.setSpacing(5)
        self.btn_undo = QPushButton("되돌리기  (Ctrl+Z)")
        self.btn_restore_ai = QPushButton("AI가 찾은 구간으로")
        self.btn_undo.setToolTip("방금 바꾼 구간을 되돌립니다.")
        self.btn_restore_ai.setToolTip("손댄 값을 버리고 AI가 찾아 준 구간으로 되돌립니다.")
        for b in (self.btn_undo, self.btn_restore_ai):
            b.setFixedHeight(27)
            undo_row.addWidget(b)
        lay.addLayout(undo_row)

        self.btn_export = QPushButton("3.  설교 영상 만들기")
        self.btn_export.setFixedHeight(42)
        self.btn_export.setProperty("primary", True)
        self.btn_export.setStyleSheet(f"font-size:{T.font_big_pt}pt;")
        self.btn_mp3 = QPushButton("설교 mp3")
        self.btn_mp3.setFixedHeight(42)
        self.btn_mp3.setStyleSheet(
            f"font-size:{T.font_pt}pt; font-weight:600; background:transparent;"
            f"color:{T.accent}; border:1px solid {T.accent};"
            f"border-radius:{T.radius_sm}px;")
        self.btn_mp3.setToolTip(
            "설교를 소리만 mp3 로 뽑습니다. 영상은 만들지 않습니다.\n"
            "구간 목록에 있는 그대로 들어갑니다 (시작 영상은 넣지 않습니다).\n"
            "40분 분량이 1분 안쪽이면 끝나고 파일도 작아 (30MB 안팎)\n"
            "휴대폰으로 보내거나 차에서 듣기 좋습니다.")

        self.bene_check = QCheckBox("축도도 함께 넣기")
        self.bene_check.setToolTip(
            "기본은 설교만 만듭니다.\n"
            "켜면 AI 가 찾아 둔 축도 구간이 구간 목록에 더해져 설교 뒤에 이어집니다.\n"
            "중간의 찬양·헌금은 어느 쪽이든 빠집니다.")
        lay.addWidget(self.bene_check)

        make_row = QHBoxLayout()
        make_row.setSpacing(6)
        make_row.addWidget(self.btn_export, 3)
        make_row.addWidget(self.btn_mp3, 1)
        lay.addLayout(make_row)

        # ---------------- 고급 (평소엔 숨김) ----------------
        self.btn_advanced = QPushButton("고급 설정 보기  ▼")
        self.btn_advanced.setCheckable(True)
        self.btn_advanced.setFixedHeight(22)
        self.btn_advanced.setProperty("flat", True)
        lay.addWidget(self.btn_advanced)

        self.advanced = QWidget()
        adv = QVBoxLayout(self.advanced)
        adv.setContentsMargins(0, 0, 0, 0)
        adv.setSpacing(4)

        noise_row = QHBoxLayout()
        noise_row.addWidget(QLabel("잡음 제거"))
        self.denoise_box = QComboBox()
        self.denoise_box.addItems(["약하게 (권장)", "강하게", "끔"])
        self.denoise_box.setToolTip(
            "예배 방송에 섞인 웅웅거리는 잡음(마이크 험·에어컨)을 줄입니다.\n"
            "약하게: 잡음 13dB 감소, 목소리 손상 거의 없음\n"
            "강하게: 잡음 19dB 감소, 잡음이 심한 날에\n"
            "소리를 키우기 전에 잡음을 먼저 지우므로 잡음까지 커지지 않습니다.")
        noise_row.addWidget(self.denoise_box, 1)
        adv.addLayout(noise_row)

        self.voice_check = QCheckBox("말소리 다듬기 (권장)")
        self.voice_check.setChecked(True)
        self.voice_check.setToolTip(
            "예배당 울림을 줄이고 목소리를 또렷하게 다듬습니다.\n"
            "설교 영상·mp3·3분 요약에 모두 들어갑니다.\n"
            "  · 울림 줄이기: 예배당에서 말꼬리가 뭉치는 것을 줄입니다 (잡음 제거와 다릅니다)\n"
            "  · 발음대 강조: 말이 또렷해집니다. 휴대폰 스피커에서 특히 다릅니다\n"
            "  · 말 사이 소음 누르기\n"
            "소리 크기는 따로 맞추므로, 켜고 끄며 들어 봐도 크기 차이로 헷갈리지 않습니다.\n"
            "울림 줄이기는 36분 설교에 1분쯤 걸리고, 한 번 하면 다시 쓰므로\n"
            "같은 설교의 두 번째 내보내기부터는 시간이 들지 않습니다.")
        adv.addWidget(self.voice_check)

        self.normalize_check = QCheckBox("소리 크기 맞추기 (권장)")
        self.normalize_check.setChecked(True)
        self.normalize_check.setToolTip(
            "예배 방송은 소리가 작게 녹음되는 경우가 많습니다.\n"
            "방송·유튜브 표준 음량(-16 LUFS)에 맞춰 키웁니다.\n"
            "영상은 그대로 두고 소리만 다시 만들므로 40분 분량에 1분이면 됩니다.")
        adv.addWidget(self.normalize_check)

        self.exact_check = QCheckBox("정확히 자르기 (느림, 다시 인코딩)")
        self.exact_check.setToolTip(
            "끄면 수 초 만에 끝나지만 컷 지점이 최대 몇 초 밀립니다.\n"
            "설교 시작·끝은 앞뒤로 여유가 있어 보통 꺼도 충분합니다.")
        adv.addWidget(self.exact_check)

        sub_row = QHBoxLayout()
        sub_row.addWidget(QLabel("자막 넣는 방법"))
        self.subtitle_box = QComboBox()
        self.subtitle_box.addItems([
            "껐다 켤 수 있게 (권장)",
            "항상 보이게 (느림)",
        ])
        self.subtitle_box.setToolTip(
            "위에서 [자막도] 를 켰을 때만 씁니다. 어느 쪽이든 자막은 영상 안에\n"
            "들어가므로 영상 파일 하나만 보내면 됩니다.\n\n"
            "껐다 켤 수 있게: 보는 사람이 자막을 켜고 끌 수 있습니다. 몇 초면 끝납니다.\n"
            "항상 보이게: 자막이 그림처럼 화면에 박혀 어떤 재생기에서도 보입니다.\n"
            "  대신 화면을 다시 그리느라 40분 분량에 20~30분 걸리고, 나중에 뺄 수 없습니다.\n\n"
            ".srt 파일은 어느 쪽이든 '자막백업' 폴더에 따로 남습니다 (유튜브에 올릴 때 씁니다).")
        sub_row.addWidget(self.subtitle_box, 1)
        adv.addLayout(sub_row)

        intro_row = QHBoxLayout()
        intro_row.addWidget(QLabel("시작 영상"))
        self.intro_label = QLabel("없음")
        self.intro_label.setStyleSheet(f"color:{T.text_dim};")
        self.btn_intro = QPushButton("고르기")
        self.btn_intro_clear = QPushButton("빼기")
        for b in (self.btn_intro, self.btn_intro_clear):
            b.setFixedHeight(24)
            b.setMaximumWidth(70)
        tip = ("설교 영상 앞에 붙일 짧은 영상입니다 (교회 로고·주보·인사말 등).\n"
               "한 번 고르면 계속 쓰이고, [빼기] 를 누르면 다시 안 붙습니다.\n"
               "크기가 달라도 본 영상에 맞춰 자동으로 맞춥니다.\n"
               "소리가 없는 영상이면 무음으로 넣습니다.")
        self.btn_intro.setToolTip(tip)
        self.intro_label.setToolTip(tip)
        intro_row.addWidget(self.intro_label, 1)
        intro_row.addWidget(self.btn_intro)
        intro_row.addWidget(self.btn_intro_clear)
        adv.addLayout(intro_row)

        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("화면 모양"))
        self.theme_box = QComboBox()
        self.theme_box.addItems(list(theme_mod.THEMES.values()))
        self.theme_box.setToolTip(
            "화면 색과 글자 크기를 고릅니다. 고르면 바로 바뀝니다.\n\n"
            "어두운 화면: 영상 편집 프로그램과 같은 색입니다. 영상 색이 제대로 보이고\n"
            "  어두운 곳에서 눈이 편합니다.\n"
            "밝은 화면: 예전 화면입니다. 밝은 사무실에서 보기 좋습니다.\n"
            "밝은 화면 · 큰 글자: 글자와 줄 높이를 키웁니다.")
        theme_row.addWidget(self.theme_box, 1)
        adv.addLayout(theme_row)

        adv.addWidget(QLabel("구간 목록 (여러 구간을 이어붙일 때)"))
        self.clip_list = QListWidget()
        self.clip_list.setMaximumHeight(80)
        adv.addWidget(self.clip_list)

        row = QHBoxLayout()
        self.btn_add = QPushButton("+ 구간 추가")
        self.btn_del = QPushButton("− 삭제")
        self.btn_up = QPushButton("▲")
        self.btn_down = QPushButton("▼")
        for b in (self.btn_add, self.btn_del, self.btn_up, self.btn_down):
            row.addWidget(b)
        adv.addLayout(row)

        self.btn_save = QPushButton("구간 저장")
        adv.addWidget(self.btn_save)

        self.advanced.setVisible(False)
        lay.addWidget(self.advanced)
        lay.addStretch(1)

        # ---------------- 연결 ----------------
        self.btn_undo.clicked.connect(self.undo)
        self.btn_restore_ai.clicked.connect(self.restore_ai)
        self.btn_advanced.toggled.connect(self._toggle_advanced)
        self.clip_list.currentRowChanged.connect(self._on_clip_selected)
        self.btn_add.clicked.connect(self.on_add_clip)
        self.btn_del.clicked.connect(self.on_del_clip)
        self.btn_up.clicked.connect(lambda: self._move_clip(-1))
        self.btn_down.clicked.connect(lambda: self._move_clip(1))
        self.btn_save.clicked.connect(self.on_save)
        self.btn_export.clicked.connect(self.on_export)
        self.btn_mp3.clicked.connect(self.on_export_mp3)
        self.btn_intro.clicked.connect(self.on_pick_intro)
        self.btn_intro_clear.clicked.connect(self.on_clear_intro)
        self.bene_check.toggled.connect(self._on_bene_toggled)
        self.start_edit.editingFinished.connect(lambda: self._commit_edit("start"))
        self.end_edit.editingFinished.connect(lambda: self._commit_edit("end"))
        return box

    def _edge_group(self, which: str) -> QWidget:
        """시작 또는 끝 한 세트. 세로로 쌓되 높이를 최대한 줄였다."""
        is_start = which == "start"
        box = QGroupBox("설교 시작" if is_start else "설교 끝")
        lay = QVBoxLayout(box)
        lay.setSpacing(4)
        lay.setContentsMargins(8, 6, 8, 6)

        edit = QLineEdit()
        edit.setAlignment(Qt.AlignCenter)
        edit.setFixedHeight(30)
        edit.setStyleSheet(
            f"font-size:{T.font_big_pt + 2}pt; font-weight:bold;"
            f"background:{T.field_bg}; color:{T.timecode};")
        lay.addWidget(edit)

        here = QPushButton(f"여기가 {'시작' if is_start else '끝'}")
        here.setFixedHeight(30)
        here.setProperty("primary", True)
        here.setToolTip("지금 보고 있는 화면의 시각을 "
                        + ("시작" if is_start else "끝") + "으로 정합니다.")
        lay.addWidget(here)

        nudge = QHBoxLayout()
        nudge.setSpacing(3)
        for label, delta in (("◀◀1초", -1.0), ("◀0.1", -0.1), ("0.1▶", 0.1), ("1초▶▶", 1.0)):
            b = QPushButton(label)
            b.setFixedHeight(27)
            b.clicked.connect(lambda _, w=which, d=delta: self._shift(w, d))
            b.setToolTip("경계를 조금씩 옮깁니다. 옮긴 자리가 화면에 바로 보입니다.")
            nudge.addWidget(b)
        lay.addLayout(nudge)

        check = QPushButton("이 지점 확인 (앞뒤 3초 재생)")
        check.setFixedHeight(25)
        check.setStyleSheet(f"font-size:{T.font_small_pt}pt;")
        check.clicked.connect(lambda: self._check_edge(which))
        lay.addWidget(check)

        if is_start:
            self.start_edit, self.btn_start_here, self.btn_start_check = edit, here, check
        else:
            self.end_edit, self.btn_end_here, self.btn_end_check = edit, here, check
        here.clicked.connect(lambda: self._mark(which))
        return box

    def _toggle_advanced(self, on: bool) -> None:
        self.advanced.setVisible(on)
        self.btn_advanced.setText("고급 설정 숨기기  ▲" if on else "고급 설정 보기  ▼")

    def _build_shortcuts(self) -> None:
        def act(seq, fn):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(fn)
            self.addAction(a)

        act("I", lambda: self._mark("start"))
        act("O", lambda: self._mark("end"))
        act("Space", self.toggle_play)
        act("K", self.toggle_play)
        act("J", lambda: self._nudge_play(-10))
        act("L", lambda: self._nudge_play(10))
        act("Left", lambda: self._nudge_play(-1))
        act("Right", lambda: self._nudge_play(1))
        act("Ctrl+S", self.on_save)
        act("Ctrl+Z", self.undo)
        act("Ctrl+Left", lambda: self._shift(self.edit_edge, -0.1))
        act("Ctrl+Right", lambda: self._shift(self.edit_edge, 0.1))
        act("Ctrl+Shift+Left", lambda: self._shift(self.edit_edge, -1.0))
        act("Ctrl+Shift+Right", lambda: self._shift(self.edit_edge, 1.0))

    def _build_log_tab(self):
        """진행 로그 칸. 위에 손볼 단추 한 줄을 둔다.

        [메모리 넉넉하게 만들기] 는 윈도우의 페이징 파일을 늘린다. 이 PC 는
        메모리 13.8GB 가 달렸는데 페이징 파일이 896MB 로 잡혀 통틀어 쓸 수
        있는 양이 14.7GB 뿐이었고, 그래서 영상 만들기가 멈췄다. 고치는 길이
        윈도우 고급 설정뿐인데 교회에서 쓰시는 분들은 거기까지 들어가기
        어려우므로, 프로그램이 대신 해 드린다 (2026-10-20 사용자 요청).
        """
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.btn_pagefile = QPushButton("메모리 넉넉하게 만들기")
        self.btn_pagefile.clicked.connect(self.on_fix_pagefile)
        row.addWidget(self.btn_pagefile)
        self.lbl_pagefile = QLabel("")
        self.lbl_pagefile.setStyleSheet(f"color:{T.text_dim};")
        row.addWidget(self.lbl_pagefile, 1)
        lay.addLayout(row)
        lay.addWidget(self.log_view, 1)
        self._refresh_pagefile_button()
        return box

    def _refresh_pagefile_button(self) -> None:
        """단추를 지금 사정에 맞춰 켜고 끈다. 필요 없으면 아예 숨긴다."""
        try:
            from core import pagefile
            need = pagefile.needs_fix()
            now, limit = pagefile.pagefile_mb(), machine_mod.commit_limit_mb()
        except Exception:
            need, now, limit = False, None, None
        self.btn_pagefile.setVisible(need)
        self.lbl_pagefile.setVisible(need)
        if not need:
            return
        here = (f"지금 통틀어 쓸 수 있는 양 {limit / 1024:.1f}GB "
                f"(페이징 파일 {now / 1024:.1f}GB)" if limit and now is not None else "")
        self.lbl_pagefile.setText(
            "영상 만들기가 메모리 때문에 멈춘다면 이 단추를 눌러 주세요 — " + here)
        self.btn_pagefile.setToolTip(
            "윈도우가 통틀어 내주는 메모리를 늘립니다.\n"
            "관리자 권한을 한 번 묻고, 바꾼 뒤 윈도우를 다시 시작하면 적용됩니다.\n"
            "언제든 윈도우 설정에서 '자동 관리' 로 되돌릴 수 있습니다.")

    def on_fix_pagefile(self) -> None:
        """[메모리 넉넉하게 만들기] — 무엇을 바꿀지 보여 주고, 허락받고 바꾼다."""
        from core import pagefile

        plan = pagefile.plan()
        if not plan.get("ok"):
            QMessageBox.information(self, "메모리 넉넉하게 만들기", plan.get("why", ""))
            self.log("[메모리] " + plan.get("why", "").replace("\n", " "))
            return

        ask = QMessageBox(self)
        ask.setWindowTitle("메모리 넉넉하게 만들기")
        ask.setIcon(QMessageBox.Question)
        ask.setText("윈도우가 통틀어 내주는 메모리를 늘립니다.")
        ask.setInformativeText(plan["why"])
        ask.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        ask.button(QMessageBox.Yes).setText("바꾸기")
        ask.button(QMessageBox.Cancel).setText("그만두기")
        ask.setDefaultButton(QMessageBox.Yes)
        if ask.exec() != QMessageBox.Yes:
            self.log("[메모리] 바꾸지 않았습니다")
            return

        self.log("[메모리] 페이징 파일을 "
                 f"{plan['initial'] / 1024:.0f}GB(최대 {plan['max'] / 1024:.0f}GB) 로 잡습니다")
        self.btn_pagefile.setEnabled(False)
        try:
            got = pagefile.apply(say=lambda t: self.log("  " + t))
        finally:
            self.btn_pagefile.setEnabled(True)
        for line in str(got.get("message", "")).split("\n"):
            if line:
                self.log("  " + line)
        if got.get("ok"):
            QMessageBox.information(self, "메모리 넉넉하게 만들기", got["message"])
        else:
            QMessageBox.warning(self, "메모리 넉넉하게 만들기",
                                got.get("message", "바꾸지 못했습니다."))
        self._refresh_pagefile_button()

    def _startup_check(self) -> None:
        """시작할 때 무엇이 되고 무엇이 안 되는지 확인해 로그에 남긴다.

        버튼이 이유 없이 비활성처럼 보이는 일이 없도록, 못 쓰는 기능은
        비활성화하고 툴팁에 이유를 적는다.
        """
        import importlib.util
        from core.util import find_tool
        from core.voiceprint import EMBED_MODEL, MODEL_DIR_CANDIDATES

        cfg = self._config()
        self.log_step(f"창의진교회 설교영상 편집기 {version_mod.full()}",
                      "프로그램 구성을 확인합니다")
        self.log(f"  [컴퓨터] {machine_mod.summary()}")
        self.log(f"  [기록] {getattr(self, '_log_why', '(모름)')}")
        # 지난번에 메모리가 모자라 창째 사라졌다면, 우리가 띄워 둔 도우미가
        # 남아 메모리를 계속 잡고 있다. 켤 때 치워서 넉넉한 상태로 시작한다.
        try:
            from core import cleanup
            left = cleanup.leftovers()
            if left:
                self.log(f"  [정리] 지난번에 남은 {len(left)}개를 치웁니다")
                for pid, path in left:
                    self.log(f"         {path.name} (번호 {pid})")
                cleanup.sweep(left)
                self.log(f"  [컴퓨터] 치운 뒤: {machine_mod.summary()}")
        except Exception as exc:
            self.log(f"  [정리] 건너뜁니다 — {exc}")
        # 메모리가 빠듯하면 일을 시작하기 전에 미리 알려 준다. 돌다가 멈추면
        # 기다린 시간이 통째로 날아가고, 무엇이 문제인지도 알기 어렵다.
        tip = machine_mod.advice()
        if tip:
            for i, line in enumerate(textwrap.wrap(tip, 70)):
                self.log(("  [주의] " if i == 0 else "         ") + line)
            self.log("         자막 만들기는 메모리를 많이 씁니다 — 모자라면 작은 모델로 자동으로 바꿉니다.")

        tools = {name: find_tool(name) for name in ("ffmpeg", "ffprobe", "yt-dlp")}
        for name, path in tools.items():
            self.log(f"  [{'OK ' if path else '없음'}] {name}")

        has_sherpa = importlib.util.find_spec("sherpa_onnx") is not None
        model = next((d / EMBED_MODEL for d in MODEL_DIR_CANDIDATES
                      if (d / EMBED_MODEL).exists()), None)
        has_vp = cfg.voiceprint_path.exists()
        self.log(f"  [{'OK ' if has_sherpa else '없음'}] 성문 검출 엔진 (sherpa-onnx)")
        self.log(f"  [{'OK ' if model else '없음'}] 성문 모델 {EMBED_MODEL}")
        self.log(f"  [{'OK ' if has_vp else '없음'}] 목사님 성문")

        # AI 모델이 어디서 찾아졌는지 남긴다. 2026-10-20 에 모델이 PC 안에
        # 멀쩡히 있는데도 "아직 없음" 이 떠서 한참 헤맸다. 자리를 적어 두면
        # 다음에는 기록만 보고 바로 안다.
        try:
            from core import embed as embed_mod, llm as llm_mod
            emb_ok = embed_mod.is_ready(cfg)
            self.log(f"  [{'OK ' if emb_ok else '없음'}] 뜻 보기 모델"
                     + (f" — {embed_mod.model_dir(cfg)}" if emb_ok else ""))
            ai = llm_mod.model_path(cfg)
            self.log(f"  [{'OK ' if ai.exists() else '없음'}] AI 모델 {ai.name}"
                     + (f" — {ai.parent}" if ai.exists() else "")
                     + (" (이 교회 설교로 학습한 모델)" if llm_mod.is_tuned(cfg) else ""))
        except Exception as exc:
            self.log(f"  [없음] AI 모델 확인 실패 — {exc}")

        auto_ok = has_sherpa and model is not None and has_vp
        self.btn_quick.setEnabled(auto_ok)
        if not auto_ok:
            missing = []
            if not has_sherpa:
                missing.append("성문 검출 엔진")
            if model is None:
                missing.append(f"성문 모델({EMBED_MODEL})")
            if not has_vp:
                missing.append("목사님 성문")
            why = ", ".join(missing)
            self.btn_quick.setToolTip(f"쓸 수 없습니다 — {why} 이(가) 없습니다.\n"
                                      "[직접 정하기] 로 직접 구간을 정하실 수 있습니다.")
            self.log(f"  → [자동으로 찾기] 를 쓸 수 없습니다: {why}")

        has_stt = importlib.util.find_spec("faster_whisper") is not None
        self.stt_check.setEnabled(has_stt)
        self.log(f"  [{'OK ' if has_stt else '없음'}] 자막 엔진 (faster-whisper)")
        if not has_stt:
            self.stt_check.setChecked(False)
            self.stt_check.setToolTip(
                "이 설치본에는 자막 기능이 들어 있지 않습니다.\n"
                "설교 구간을 찾고 영상을 만드는 데는 지장이 없습니다.")

        if not tools["ffmpeg"] or not tools["ffprobe"]:
            self.log("  → ffmpeg 가 없어 영상 자르기가 되지 않습니다. bin 폴더를 확인하세요.")

        self.log("확인 완료" + ("" if auto_ok else " — 일부 기능 사용 불가"))

    # ============================================================== 재생
    def _pos(self) -> float:
        return self.player.position() / 1000.0

    def _seek(self, seconds: float) -> None:
        self.player.setPosition(int(max(0.0, seconds) * 1000))

    def _nudge_play(self, delta: float) -> None:
        self.stop_at = None
        self._seek(self._pos() + delta)

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.btn_play.setText("▶ 재생")
        else:
            self.player.play()
            self.btn_play.setText("❚❚ 일시정지")

    def _on_position(self, ms: int) -> None:
        seconds = ms / 1000.0
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        self.range_bar.set_position(seconds)
        self.zoom_bar.set_position(seconds)
        self.time_label.setText(f"{fmt(seconds)} / {fmt(self.player.duration()/1000.0)}")
        if self.stop_at is not None and seconds >= self.stop_at:
            self.stop_at = None
            self.player.pause()
            self.btn_play.setText("▶ 재생")
        if getattr(self, "_preview", None):
            self._preview_tick(seconds)          # [3분 요약] 미리보기: 다음 조각으로 건너뛰기

    def _play_span(self, start: float, end: float) -> None:
        self.stop_at = end
        self._seek(start)
        self.player.play()
        self.btn_play.setText("❚❚ 일시정지")

    # ============================================================== 구간 편집
    def _clips(self) -> list[dict]:
        return self.marker.clips if self.marker else []

    def _current(self) -> dict | None:
        row = self.clip_list.currentRow()
        clips = self._clips()
        return clips[row] if 0 <= row < len(clips) else None

    def _refresh_clips(self, keep_row: int | None = None) -> None:
        row = self.clip_list.currentRow() if keep_row is None else keep_row
        self.clip_list.blockSignals(True)
        self.clip_list.clear()
        for c in self._clips():
            name = LABEL_KO.get(c.get("label", "clip"), c.get("label", "구간"))
            length = (c["end"] - c["start"]) / 60.0
            self.clip_list.addItem(
                QListWidgetItem(f"{name}   {fmt(c['start'])} ~ {fmt(c['end'])}   ({length:.1f}분)"))
        self.clip_list.blockSignals(False)
        if self._clips():
            self.clip_list.setCurrentRow(min(max(row, 0), len(self._clips()) - 1))
        self._update_span_label()
        self._refresh_bar()

    def _refresh_bar(self) -> None:
        if not self.marker:
            return
        duration = self.marker.duration or self.player.duration() / 1000.0
        self.range_bar.set_data(duration, self.marker.ranges(), self.clip_list.currentRow())

    def _on_clip_selected(self, row: int) -> None:
        clip = self._current()
        if clip:
            self.start_edit.setText(fmt(clip["start"]))
            self.end_edit.setText(fmt(clip["end"]))
            self.zoom_bar.set_edge(self.edit_edge, clip[self.edit_edge])
            self.zoom_bar.set_clip(clip["start"], clip["end"])
        self._update_span_label()
        self._refresh_bar()

    def _select_edge(self, which: str) -> None:
        """정밀 조정 대상 경계를 바꾼다. 그 지점으로 재생 위치도 옮긴다."""
        self.edit_edge = which
        self.range_bar.active_edge = which
        self.btn_edge_start.setChecked(which == "start")
        self.btn_edge_end.setChecked(which == "end")
        clip = self._current()
        if clip:
            self.zoom_bar.set_edge(which, clip[which])
            self.zoom_bar.set_clip(clip["start"], clip["end"])
            self._update_span_label()
            self._seek(clip[which])

    def _on_edge_dragged(self, which: str, seconds: float) -> None:
        """타임라인/정밀바에서 경계를 끌 때. 끄는 동안 그 위치를 미리보기한다.

        시작을 끝보다 뒤로 끌면 막지 않고 끝을 밀어낸다. 막아버리면 60초짜리
        빈 구간에서 시작을 37분으로 옮기는 것 자체가 불가능해진다.
        """
        clip = self._current()
        if not clip:
            return
        if not self._drag_snapshot:          # 드래그 한 번에 스냅샷 하나
            self._snapshot()
            self._drag_snapshot = True
        seconds = max(0.0, seconds)
        total = self.marker.duration if self.marker else 0.0
        if total:
            seconds = min(seconds, total)
        clip[which] = seconds
        self._reconcile(clip, which)
        if which == self.edit_edge:
            self.zoom_bar.set_edge(which, clip[which])
        self.zoom_bar.set_clip(clip["start"], clip["end"])
        self.start_edit.setText(fmt(clip["start"]))
        self.end_edit.setText(fmt(clip["end"]))
        self._update_span_label()
        self._refresh_clip_row()

        # 큰 영상은 탐색이 무거워서 매 픽셀마다 시크하면 끌리는 느낌이 사라진다
        now = time.monotonic()
        if now - self._last_seek > 0.12:
            self._last_seek = now
            self._seek(clip[which])

    def _on_edge_released(self) -> None:
        self._drag_snapshot = False
        clip = self._current()
        if not clip:
            return
        self._last_seek = 0.0
        self._seek(clip[self.edit_edge])
        self._refresh_clips()
        self.log(f"시작 {fmt(clip['start'])}  끝 {fmt(clip['end'])}"
                 f"  ({(clip['end']-clip['start'])/60:.1f}분)")

    def _update_span_label(self) -> None:
        clip = self._current()
        if not clip:
            self.span_label.setText("구간 없음 —  재생하며 시작에서 I, 끝에서 O 를 누르세요")
            return
        length = clip["end"] - clip["start"]
        s_mark = "▶ " if self.edit_edge == "start" else "   "
        e_mark = "▶ " if self.edit_edge == "end" else "   "
        self.span_label.setText(
            f"{s_mark}시작 {fmt(clip['start'])}     {e_mark}끝 {fmt(clip['end'])}"
            f"     길이 {length/60:.1f}분")

    def _refresh_clip_row(self) -> None:
        """드래그 중에는 목록 전체를 다시 그리지 않고 현재 줄만 갱신한다."""
        row = self.clip_list.currentRow()
        clip = self._current()
        item = self.clip_list.item(row) if clip else None
        if item:
            name = LABEL_KO.get(clip.get("label", "clip"), "구간")
            length = (clip["end"] - clip["start"]) / 60.0
            item.setText(f"{name}   {fmt(clip['start'])} ~ {fmt(clip['end'])}   ({length:.1f}분)")
        self._refresh_bar()

    def _mark(self, which: str) -> None:
        """I / O — 구간이 없으면 만들고, 현재 위치를 그 경계로 찍는다."""
        if not self.media_path:
            return
        self._ensure_clip()
        self._snapshot()
        pos = self._pos()
        clip = self._current()
        if clip and which == "start" and pos >= clip["end"]:
            clip["end"] = min(pos + 60.0, self.marker.duration or pos + 60.0)
        self._set_edge(which, pos)
        self._select_edge(which)

    def _snapshot(self) -> None:
        """구간을 바꾸기 직전 상태를 쌓아 둔다. Ctrl+Z 로 되돌린다."""
        if not self.marker:
            return
        self._undo.append(copy.deepcopy(self.marker.clips))
        del self._undo[:-30]

    def undo(self) -> None:
        if not self._undo or not self.marker:
            self.log("되돌릴 것이 없습니다")
            return
        self.marker.clips = self._undo.pop()
        self._refresh_clips()
        self._on_clip_selected(self.clip_list.currentRow())
        self.log("되돌렸습니다")

    def restore_ai(self) -> None:
        """사람이 손댄 값을 버리고 AI 가 찾은 구간으로 되돌린다."""
        if not self.marker:
            return
        if self.marker.sermon_ai_start is None:
            QMessageBox.information(self, "AI 값 없음",
                                    "이 영상에는 AI가 찾은 구간이 없습니다.")
            return
        self._snapshot()
        m = self.marker
        m.sermon_human_start = m.sermon_human_end = None
        m.bene_human_start = m.bene_human_end = None
        m.clips = []
        m.sync_clips_from_detection()
        m.corrected = False
        self._refresh_clips(0)
        self._on_clip_selected(0)
        self.log("AI가 찾은 구간으로 되돌렸습니다")

    def _reconcile(self, clip: dict, moved: str) -> None:
        """두 경계가 뒤집히면 방금 움직이지 않은 쪽을 밀어낸다."""
        total = (self.marker.duration if self.marker else 0.0) or 0.0
        if clip["end"] - clip["start"] >= 0.5:
            return
        if moved == "start":
            clip["end"] = clip["start"] + 60.0
            if total:
                clip["end"] = min(clip["end"], total)
            if clip["end"] - clip["start"] < 0.5:
                clip["start"] = max(0.0, clip["end"] - 60.0)
        else:
            clip["start"] = max(0.0, clip["end"] - 60.0)

    def _set_edge(self, which: str, seconds: float) -> None:
        clip = self._current()
        if not clip:
            return
        clip[which] = max(0.0, seconds)
        self._reconcile(clip, which)
        self._refresh_clips()
        self._on_clip_selected(self.clip_list.currentRow())
        if which == self.edit_edge:
            self.zoom_bar.set_edge(which, clip[which])
        self.zoom_bar.set_clip(clip["start"], clip["end"])
        self._update_span_label()
        self.log(f"{'시작' if which == 'start' else '끝'} → {fmt(clip[which])}")

    def _shift(self, which: str, delta: float) -> None:
        if self.media_path:
            self._ensure_clip()
        clip = self._current()
        if not clip:
            QMessageBox.information(
                self, "먼저 영상", "먼저 영상을 가져온 뒤 [여기가 시작]을 눌러 주세요.")
            return
        self._snapshot()
        self._select_edge(which)
        self._set_edge(which, clip[which] + delta)
        self._seek(clip[which])

    def _commit_edit(self, which: str) -> None:
        edit = self.start_edit if which == "start" else self.end_edit
        try:
            self._set_edge(which, parse_time(edit.text()))
        except ValueError:
            QMessageBox.warning(self, "시각 형식", "예: 00:37:37.4 또는 37:37")
            self._on_clip_selected(self.clip_list.currentRow())

    def _check_edge(self, which: str) -> None:
        """경계 앞뒤 몇 초만 재생해서 눈으로 확인한다."""
        clip = self._current()
        if not clip:
            QMessageBox.information(
                self, "먼저 지점 정하기", "먼저 [여기가 시작] 또는 [여기가 끝]을 눌러 주세요.")
            return
        self._select_edge(which)
        t = clip[which]
        self._play_span(max(0.0, t - 3.0), t + 3.0)

    def on_play_clip(self) -> None:
        clip = self._current()
        if clip:
            self._play_span(clip["start"], clip["end"])

    def on_manual(self) -> None:
        """AI 없이 직접 편집. 구간이 없으면 하나 만들어 바로 다듬을 수 있게 한다."""
        if not self.media_path:
            QMessageBox.information(self, "먼저 영상", "① 영상 받기 또는 영상 열기를 먼저 하세요.")
            return
        created = self._ensure_clip()
        self._select_edge("start")
        self.log_step("② 직접 정하기", "영상을 보면서 시작과 끝을 직접 정합니다")
        self.log("   1) 영상을 재생하다가 설교가 시작되면  [ 여기가 시작 ]  클릭")
        self.log("   2) 설교가 끝나면  [ 여기가 끝 ]  클릭")
        self.log("   3) ◀ 0.1  0.1 ▶  버튼으로 다듬고 [ 이 지점 확인 ] 으로 들어보기")
        self.log("   4) [ 3. 설교 영상 만들기 ] 클릭")
        self.log("")
        if created:
            self.log("  · 빈 구간을 하나 만들었습니다. I/O 로 채우면 됩니다.")
        self.hint.setText(
            "영상을 보면서 [여기가 시작]과 [여기가 끝]을 누르세요. 그다음 [설교 영상 만들기].")

    def _ensure_clip(self) -> bool:
        """마커와 구간이 없으면 만들어 준다. 새로 만들었으면 True."""
        if not self.marker:
            path = self.media_path
            self.marker = Marker(
                video_id=path.stem, title=path.name,
                duration=ffprobe_duration(path) if path else 0.0)
            self.title_label.setText(path.name if path else "")
        if not self.marker.duration and self.player.duration():
            self.marker.duration = self.player.duration() / 1000.0
        if self.marker.clips:
            if self.clip_list.currentRow() < 0:
                self.clip_list.setCurrentRow(0)
            return False
        start = self._pos()
        end = min(start + 60.0, self.marker.duration or start + 60.0)
        self.marker.clips.append({"label": "sermon", "start": start, "end": end})
        self._refresh_clips(0)
        return True

    def on_add_clip(self) -> None:
        if not self.marker:
            QMessageBox.information(self, "먼저 영상", "영상을 열거나 구간을 찾은 뒤에 추가하세요.")
            return
        self._snapshot()
        start = self._pos()
        self.marker.clips.append({"label": "clip", "start": start, "end": start + 30.0})
        self.marker.clips.sort(key=lambda c: c["start"])
        self._refresh_clips()

    def on_del_clip(self) -> None:
        row = self.clip_list.currentRow()
        if 0 <= row < len(self._clips()):
            self._snapshot()
            self.marker.clips.pop(row)
            self._refresh_clips(max(0, row - 1))

    def _move_clip(self, delta: int) -> None:
        row = self.clip_list.currentRow()
        clips = self._clips()
        new = row + delta
        if 0 <= row < len(clips) and 0 <= new < len(clips):
            clips[row], clips[new] = clips[new], clips[row]
            self._refresh_clips(new)

    # ============================================================== 작업
    def _open_run_log(self):
        """이번에 켠 기록을 담을 파일. 창이 그냥 사라져도 단서가 남는다.

        메모리가 모자라면 계산 라이브러리가 오류를 던지지 못하고 프로그램을
        통째로 끝내 버린다 (2026-10-20 교회 PC). 그러면 화면의 진행 로그가
        같이 사라져 무엇을 하다 죽었는지 알 길이 없었다.
        """
        try:
            path = Path(self.cfg.work_dir) / "실행기록.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 2 * 2 ** 20:
                path.unlink()                       # 너무 커지면 새로 시작
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 켬 =====\n")
                fh.write(f"  이 컴퓨터: {machine_mod.summary()}\n")
            import faulthandler
            self._crash_file = path.open("a", encoding="utf-8", buffering=1)
            faulthandler.enable(file=self._crash_file)   # 통째로 죽어도 자취를 남긴다
            self._log_why = f"기록 파일: {path}"
            return path
        except Exception as exc:
            # 조용히 실패하면 나중에 "왜 기록이 안 남지" 를 알 길이 없다.
            self._log_why = f"기록 파일을 열지 못했습니다 ({type(exc).__name__}: {exc})"
            return None

    def log(self, text: str) -> None:
        self.log_view.appendPlainText(text)
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())
        if getattr(self, "_log_path", None):
            try:
                with self._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except OSError:
                pass                                 # 기록 실패가 일을 막으면 안 된다

    def log_step(self, title: str, detail: str = "") -> None:
        """새 작업이 시작될 때. 앞 작업과 확실히 갈라 보이게 한다."""
        now = time.strftime("%H:%M:%S")
        if getattr(self, "_busy", False) and hasattr(self, "header"):
            # 진행 로그는 [기록] 안에 있어 다른 모드에서는 안 보인다.
            # 돌고 있다는 사실만은 머리띠에 띄워 어디서든 보이게 한다.
            # **도는 동안에만** 띄운다 — 켤 때 찍는 시작 알림은 작업이 아니다.
            self.header.set_progress(f"{title} 중…")
        self.log("")
        self.log("━" * 60)
        self.log(f"  {title}          {now}")
        if detail:
            self.log(f"  {detail}")
        self.log("━" * 60)

    def log_done(self, message: str, seconds: float | None = None,
                 next_step: str = "") -> None:
        """작업이 끝났을 때. 얼마나 걸렸는지와 다음에 할 일을 알려 준다."""
        if hasattr(self, "header"):
            self.header.set_progress("")      # 끝났으니 다시 영상 이름을 보여 준다
        took = ""
        if seconds is not None:
            took = (f"  ({seconds:.0f}초)" if seconds < 60
                    else f"  ({int(seconds // 60)}분 {int(seconds % 60)}초)")
        self.log("")
        self.log(f"  ✔ {message}{took}")
        if next_step:
            self.log(f"  → 다음:  {next_step}")
        self.log("")

    def _release_models(self) -> None:
        """다 쓴 모델을 메모리에서 내려놓는다.

        자막 모델이 1GB 안팎, 문장 임베딩 모델이 300MB 안팎을 잡고 있다. 한 번
        올리면 프로그램을 닫을 때까지 그대로 남아 있어서, 자막을 만든 뒤 영상을
        만들면 그만큼 모자란 상태에서 시작하게 된다. 둘 중 하나가 실패해도
        다른 하나는 내려놓도록 따로 감싼다.
        """
        try:
            transcribe.release_model()          # 자막 모델 (1GB 안팎)
        except Exception:
            pass
        try:
            from core import embed as embed_mod
            embed_mod.release_model()           # 문장 임베딩 모델 (300MB 안팎)
        except Exception:
            pass
        gc.collect()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if not busy:
            # 일이 끝나면 올려 둔 모델들을 내려놓는다. 쥐고 있으면 다음 일(영상
            # 만들기)이 쓸 메모리가 1GB 넘게 줄어, 빠듯한 PC 는 거기서 실패한다.
            self._release_models()
        if hasattr(self, "header"):
            # 모드 탭도 같이 잠근다. 안 잠그면 눌릴 것처럼 보이고 실제로 눌려서,
            # 돌고 있는 작업과 화면이 어긋난다 (2026-10-20 사용자 지적).
            self.header.set_busy(busy)
            # 시작할 때는 두루뭉술하게 적고, 바로 뒤의 log_step 이 진짜 이름으로 바꾼다.
            # 끝나면 지운다 — 작업이 중간에 멈춰도 글이 남지 않는다.
            self.header.set_progress("작업 중…" if busy else "")
        # 칸을 통째로 잠근다. 위젯 이름을 손으로 적어 두면 새로 더할 때마다 빠뜨린다 —
        # 실제로 [여기가 시작]·[여기가 끝]·[이 지점 확인] 넷이 빠져서, 요약 영상을
        # 만드는 중에 구간을 바꿀 수 있었다 (2026-10-20 사용자 지적).
        # 부모를 잠가도 자식이 따로 꺼 둔 상태는 그대로 남으므로 안전하다.
        for box in (getattr(self, "toolbar_box", None), getattr(self, "editor_scroll", None),
                    getattr(self, "summary_tab", None)):
            if box is not None:
                box.setEnabled(not busy)

        # 아래 목록은 위 칸 밖에 있거나, 무엇을 잠그는지 분명히 해 두려고 남긴다.
        if hasattr(self, "btn_pagefile"):
            # 일하는 중에 페이징 파일을 바꾸면 돌고 있는 ffmpeg 가 휘말린다
            self.btn_pagefile.setEnabled(not busy)
        for w in (self.btn_fetch, self.btn_open, self.btn_manual, self.btn_quick,
                  self.btn_load, self.btn_export, self.btn_mp3, self.btn_save,
                  self.btn_undo, self.btn_restore_ai,
                  self.btn_start_here, self.btn_end_here,
                  self.btn_start_check, self.btn_end_check,
                  # 자막도 잠근다. 도는 동안 고치면, 작업이 끝날 때 자막이 통째로
                  # 새로 들어오면서 그 편집이 소리 없이 사라진다. 진행 로그만 열어 둔다.
                  self.subtitle_list):
            w.setEnabled(not busy)

        # 구간을 만지는 것도 잠근다. 작업이 끝나면 _apply_marker 가 구간을 통째로
        # 새로 넣기 때문에, 도는 동안 손잡이를 끌면 그 편집이 소리 없이 사라진다.
        # 실제로 자막 만드는 35분 동안 손잡이를 네 번 끄신 일이 있었다.
        for w in (self.range_bar, self.zoom_bar, self.clip_list,
                  self.btn_add, self.btn_del, self.btn_up, self.btn_down,
                  self.btn_edge_start, self.btn_edge_end,
                  self.btn_sum_pick, self.btn_sum_undo, self.btn_sum_make,
                  self.btn_sum_mp3,
                  self.pick_mode_box, self.btn_ai_get, self.sum_list,
                  self.sum_format_box, self.sum_slot_box,
                  self.btn_sum_preview, self.btn_sum_hear, self.btn_sum_edit,
                  self.voice_check, self.denoise_box, self.normalize_check,
                  self.theme_box):
            w.setEnabled(not busy)
        if busy:
            self.btn_ai_get.setEnabled(False)
        else:
            self._update_ai_status()      # [모델 받기] 는 필요할 때만 켠다

    DENOISE_CHOICES = {"약하게 (권장)": "light", "강하게": "strong", "끔": ""}
    SUBTITLE_CHOICES = {
        "껐다 켤 수 있게 (권장)": "embed",
        "항상 보이게 (느림)": "burn",
    }

    # -------- 고급 설정 기억 (한 번 고르면 다음에 켤 때도 유지) --------
    def _load_prefs(self) -> None:
        st = QSettings("창의진교회", "SermonTool")

        # 값을 먼저 전부 읽는다. 하나씩 읽으면서 위젯에 넣으면, 위젯이 바뀌며
        # _save_prefs 가 불려 아직 읽지 않은 값을 지워 버린다.
        sub = str(st.value("subtitle_mode", "embed"))
        den = str(st.value("denoise", "light"))
        normalize = st.value("normalize", True, type=bool)
        include_bene = st.value("include_bene", False, type=bool)
        intro = str(st.value("intro_path", "") or "")
        pick = str(st.value("pick_mode", "embed"))
        fmt = str(st.value("summary_format", "two"))
        voice = st.value("voice_fx", True, type=bool)
        th = saved_theme()

        widgets = (self.subtitle_box, self.denoise_box,
                   self.normalize_check, self.bene_check, self.pick_mode_box,
                   self.sum_format_box, self.voice_check, self.theme_box)
        for w in widgets:
            w.blockSignals(True)
        for label, mode in self.SUBTITLE_CHOICES.items():
            if mode == sub:
                self.subtitle_box.setCurrentText(label)
                break
        for label, mode in self.DENOISE_CHOICES.items():
            if mode == den:
                self.denoise_box.setCurrentText(label)
                break
        self.normalize_check.setChecked(normalize)
        self.voice_check.setChecked(voice)
        self.bene_check.setChecked(include_bene)
        for label, mode in self.PICK_CHOICES.items():
            if mode == pick:
                self.pick_mode_box.setCurrentText(label)
                break
        for key, label in blocks_mod.FORMATS.items():
            if key == fmt:
                self.sum_format_box.setCurrentText(label)
                break
        self.theme_box.setCurrentText(theme_mod.THEMES.get(th, theme_mod.THEMES["dark"]))
        for w in widgets:
            w.blockSignals(False)

        self.intro_path = intro if intro and Path(intro).exists() else ""
        self._show_intro()

        self.subtitle_box.currentTextChanged.connect(self._save_prefs)
        self.denoise_box.currentTextChanged.connect(self._save_prefs)
        self.normalize_check.toggled.connect(self._save_prefs)
        self.voice_check.toggled.connect(self._save_prefs)
        self.bene_check.toggled.connect(self._save_prefs)
        self.theme_box.currentTextChanged.connect(self._on_theme_changed)
        self._update_ai_status()

    def _save_prefs(self, *_args) -> None:
        s = QSettings("창의진교회", "SermonTool")
        s.setValue("subtitle_mode",
                   self.SUBTITLE_CHOICES.get(self.subtitle_box.currentText(), "embed"))
        s.setValue("denoise",
                   self.DENOISE_CHOICES.get(self.denoise_box.currentText(), "light"))
        s.setValue("normalize", self.normalize_check.isChecked())
        if hasattr(self, "voice_check"):
            s.setValue("voice_fx", self.voice_check.isChecked())
        if hasattr(self, "theme_box"):
            s.setValue("theme", self._theme_key())
        s.setValue("include_bene", self.bene_check.isChecked())
        s.setValue("intro_path", self.intro_path)
        if hasattr(self, "sum_format_box"):
            s.setValue("summary_format", self._sum_format())
        if hasattr(self, "pick_mode_box"):
            s.setValue("pick_mode",
                       self.PICK_CHOICES.get(self.pick_mode_box.currentText(), "embed"))

    def _theme_key(self) -> str:
        label = self.theme_box.currentText()
        for key, name in theme_mod.THEMES.items():
            if name == label:
                return key
        return "dark"

    def _on_theme_changed(self, *_a) -> None:
        """고른 화면 모양을 바로 입힌다.

        위젯마다 박아 둔 색(큰 글자·강조 단추 등)은 만들 때 한 번만 들어가므로,
        전체를 다시 그리려면 프로그램을 다시 켜야 완전히 바뀐다. 대부분은 바로
        바뀌므로 다시 켜라고만 알린다 — 작업 중인 구간을 잃게 할 수는 없다.
        """
        self._save_prefs()
        apply_theme(QApplication.instance(), self._theme_key())
        self._refresh_header_theme()
        self.log("화면 모양을 바꿨습니다. 프로그램을 다시 켜면 모든 칸에 적용됩니다.")

    def _refresh_header_theme(self) -> None:
        """머리띠에 지금 화면 모양 이름을 적고, 글자 크기에 맞춰 높이를 다시 잡는다."""
        if not hasattr(self, "header"):
            return
        # 상자의 글자가 아니라 **실제로 입혀진** 테마를 적는다. 둘이 어긋나면
        # 화면은 어두운데 "밝은 화면" 이라고 적히는 일이 생긴다.
        self.header.set_corner(theme_mod.THEMES.get(theme_mod.current().name, ""))
        self.header.refresh_theme()
        for strip in self.findChildren(ui_header._Strip):
            strip.refresh_theme()

    def _config(self) -> Config:
        cfg = Config()
        if hasattr(self, "normalize_check"):
            cfg.normalize_audio = self.normalize_check.isChecked()
            cfg.denoise = self.DENOISE_CHOICES.get(self.denoise_box.currentText(), "light")
        if hasattr(self, "voice_check"):
            on = self.voice_check.isChecked()
            cfg.enhance_speech = on
            if not on:
                cfg.dereverb_amount = 0.0
        if hasattr(self, "subtitle_box"):
            cfg.subtitle_mode = self.SUBTITLE_CHOICES.get(
                self.subtitle_box.currentText(), "embed")
            # 축도는 늘 찾아서 마커에 넣어 둔다. 넣을지 말지는 구간 목록이
            # 정한다 — 그래야 나중에 [축도도 함께 넣기] 를 켜면 바로 나온다.
            cfg.include_benediction = True
            cfg.intro_path = self.intro_path
        if hasattr(self, "pick_mode_box"):
            mode = self.PICK_CHOICES.get(self.pick_mode_box.currentText(), "embed")
            cfg.use_embedding = mode in ("embed", "llm")
            cfg.use_llm = (mode == "llm")
        cfg.ensure_dirs()
        return cfg

    def _run(self, fn, on_done, title: str = "작업", detail: str = "",
             done_message: str = "끝났습니다", next_step: str = "",
             est_kind: str = "", est_minutes: float = 0.0, est_model: str = "") -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "실행 중", "이미 작업이 돌고 있습니다.")
            return
        # 하던 자리를 기억해 둔다. 작업이 끝나면 거기로 돌려놓는다 — 진행 로그에
        # 남겨 두면 "내가 뭘 누른 거지" 싶게 된다 (2026-10-20 사용자 지적).
        self._mode_before_job = self.tabs.currentIndex()
        self._set_busy(True)
        self.tabs.setCurrentIndex(self.TAB_LOG)     # 도는 동안은 진행 로그를 보여준다
        # 얼마나 걸릴지 미리 알려 준다. 느린 PC 에서는 한 시간이 넘을 수도 있어,
        # 모르고 기다리는 것이 제일 답답하다. 실제로 걸린 시간은 아래에서 기록해
        # 두었다가 다음부터 이 PC 의 기록으로 말한다.
        guess = machine_mod.estimate(est_kind, est_minutes, self.cfg.work_dir, est_model) \
            if est_kind else ""
        self.log_step(title, " · ".join(x for x in (detail, guess) if x))
        self.hint.setText(f"{title} 하는 중입니다{('  ·  ' + guess) if guess else ''}"
                          "  —  [기록] 에서 진행 상황을 볼 수 있습니다.")
        QApplication.processEvents()

        started = time.monotonic()

        def finished(result):
            self._set_busy(False)
            self._restore_mode()
            took = time.monotonic() - started
            if est_kind and est_minutes > 0:
                machine_mod.remember(est_kind, est_minutes, took, self.cfg.work_dir)
            self.log_done(done_message, took, next_step)
            on_done(result)
            self._update_hint()

        self.worker = Worker(fn, self)
        self.worker.log.connect(self.log)
        self.worker.done.connect(finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _restore_mode(self) -> None:
        """작업 전에 보던 칸으로 돌려놓는다. 진행 로그를 보고 있었으면 그대로 둔다."""
        back = getattr(self, "_mode_before_job", None)
        self._mode_before_job = None
        if back is not None and 0 <= back < self.tabs.count() and back != self.TAB_LOG:
            self.tabs.setCurrentIndex(back)

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self.log("")
        self.log("  ✖ 문제가 생겨 멈췄습니다")
        for line in message.splitlines():
            self.log(f"     {line}")
        self.log("")
        self._update_hint()
        QMessageBox.critical(self, "오류", message[:1200])

    def _update_hint(self) -> None:
        if hasattr(self, "header"):
            # 머리띠 가운데에는 지금 다루는 영상 이름을 적는다 (FilmCraft 와 같은 자리)
            self.header.set_title(self.media_path.name if self.media_path else "")
        if not self.media_path:
            self.hint.setText("먼저 유튜브 주소를 붙여넣고 [영상 가져오기]를 누르세요.")
        elif not self.marker or not self.marker.clips:
            self.hint.setText(
                "[직접 정하기]를 누른 뒤, 영상을 보면서 [여기가 시작] · [여기가 끝]을 누르세요.")
        else:
            self.hint.setText(
                "◀ 0.1 / 0.1 ▶ 버튼으로 다듬고, [이 지점 확인]으로 들어본 뒤 "
                "[설교 영상 만들기]를 누르세요.")

    def _video_id(self, url: str) -> str:
        return fetch.info(url)["id"] if url.startswith("http") else url

    def on_fetch(self) -> None:
        url = self.url_edit.text().strip() or (self.marker.url if self.marker else "")
        if not url:
            QMessageBox.warning(self, "입력 필요", "유튜브 URL을 넣으세요.")
            return
        cfg = self._config()
        quality = {"원본": None, "720p": 720, "360p": 360}[self.quality_box.currentText()]
        def job():
            meta = fetch.info(url) if url.startswith("http") else {
                "id": url, "title": url, "webpage_url": "", "duration": 0.0, "upload_date": ""}
            path = cfg.preview_path(meta["id"])
            if path.exists():
                print(f"이미 받아둔 영상을 씁니다: {path.name}")
            else:
                label = "원본 화질" if quality is None else f"{quality}p"
                print(f"영상 다운로드 중 ({label})")
                fetch.download_preview(meta["webpage_url"] or url, path, quality)
            return path, meta

        def done(result):
            path, meta = result
            self._switch_target(meta["id"])
            marker_path = cfg.marker_path(meta["id"])
            if marker_path.exists():
                self._apply_marker(Marker.load(marker_path),
                                   transcribe.load_transcript(cfg.transcript_path(meta["id"])))
            elif not self.marker:
                self.marker = Marker(video_id=meta["id"], url=meta["webpage_url"] or url,
                                     title=meta["title"], upload_date=meta.get("upload_date", ""),
                                     duration=meta["duration"])
                self.title_label.setText(meta["title"])
                self._refresh_clips()
            self._load_media(Path(path))

        self._run(job, done,
                  title="① 영상 가져오기",
                  detail=f"화질 {self.quality_box.currentText()}   {url[:60]}",
                  done_message="영상을 가져왔습니다",
                  next_step="[자동으로 찾기] 또는 [직접 정하기]")

    def on_open_file(self) -> None:
        cfg = self._config()
        path, _ = QFileDialog.getOpenFileName(
            self, "영상 또는 오디오 열기", str(cfg.work_dir),
            "미디어 (*.mp4 *.mkv *.webm *.wav *.mp3 *.m4a);;모든 파일 (*.*)")
        if not path:
            return
        chosen = Path(path)
        self._switch_target(chosen.stem)
        if not self.marker:
            saved = cfg.marker_path(chosen.stem)
            if saved.exists():
                self._apply_marker(Marker.load(saved),
                                   transcribe.load_transcript(cfg.transcript_path(chosen.stem)))
                self.log(f"저장된 구간을 함께 불러왔습니다: {saved.name}")
            else:
                self.marker = Marker(video_id=chosen.stem, title=chosen.name,
                                     duration=ffprobe_duration(chosen))
                self.title_label.setText(chosen.name)
                self._refresh_clips()
        self._load_media(chosen)

    def _ask_stt(self) -> str:
        """[자동으로 찾기] 를 누르면 자막도 만들지 묻는다. 'yes' / 'no' / 'cancel'.

        [자막도] 칸을 못 보고 지나치는 일이 많았다 — 자막 없이 찾고 나서 [3분 요약] 에서
        다시 20~40분을 기다리게 된다. 누르는 순간 한 번 물어 둘 중 하나를 고르게 한다.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("자막도 만들까요?")
        box.setText(
            "[자동으로 찾기] 는 목사님 목소리로 설교 구간을 찾습니다. 1~2분이면 끝납니다.\n\n"
            "자막도 함께 만들면\n"
            "  · 설교 영상에 자막을 넣을 수 있고\n"
            "  · [3분 요약] 을 바로 쓸 수 있습니다.\n"
            "대신 20~40분이 더 걸립니다.")
        yes = box.addButton("자막도 만들기 (20~40분 더)", QMessageBox.YesRole)
        no = box.addButton("설교 구간만 찾기 (1~2분)", QMessageBox.NoRole)
        box.addButton("취소", QMessageBox.RejectRole)
        box.setDefaultButton(no)
        box.exec()
        clicked = box.clickedButton()
        return "yes" if clicked is yes else "no" if clicked is no else "cancel"

    def on_quick(self) -> None:
        """[자동으로 찾기]. 자막 엔진이 있고 [자막도] 가 꺼져 있으면 먼저 묻는다."""
        if not self.stt_check.isEnabled():          # 자막 엔진이 없는 설치본
            self.on_analyze(with_stt=False)
            return
        if self.stt_check.isChecked():              # 이미 켜 두셨으면 묻지 않는다
            self.on_analyze(with_stt=True)
            return
        answer = self._ask_stt()
        if answer == "cancel":
            return
        self.stt_check.setChecked(answer == "yes")  # 고른 대로 칸에도 보이게
        self.on_analyze(with_stt=(answer == "yes"))

    def on_analyze(self, with_stt: bool, then=None) -> None:
        url = self.url_edit.text().strip() or (self.marker.url if self.marker else "")
        local = self.media_path if (self.media_path and self.media_path.exists()) else None
        if not url and not local:
            QMessageBox.information(
                self, "먼저 영상",
                "유튜브 주소를 넣고 [영상 가져오기] 하시거나,\n"
                "[내 컴퓨터에서 열기] 로 영상 파일을 열어 주세요.")
            return
        cfg = self._config()
        cfg.whisper_model = "medium"
        def job():
            if url.startswith("http"):
                meta = fetch.info(url)
            elif url:
                meta = {"id": url, "title": url, "webpage_url": "",
                        "duration": 0.0, "upload_date": ""}
            else:
                # URL 없이 내 컴퓨터 영상만 열어 둔 경우
                meta = {"id": local.stem, "title": local.name, "webpage_url": "",
                        "duration": 0.0, "upload_date": ""}
            vid = meta["id"]
            print(f"대상: {meta['title']} [{vid}]")

            # 로컬 파일이 있으면 그걸 그대로 분석한다 (다운로드 없음)
            wav = cfg.audio_path(vid)
            if local is None and not wav.exists():
                if not (meta["webpage_url"] or url).startswith("http"):
                    raise ToolError(
                        "분석할 오디오가 없습니다.\n"
                        "[내 컴퓨터에서 열기] 로 영상을 열거나 유튜브 주소를 넣어 주세요.")
                print("분석용 오디오 다운로드 중")
                fetch.download_audio(meta["webpage_url"] or url, wav)
            source = local or wav
            print(f"분석 대상 파일: {Path(source).name}")
            duration = meta["duration"] or ffprobe_duration(source)

            marker = Marker(video_id=vid, url=meta["webpage_url"] or url,
                            title=meta["title"], upload_date=meta.get("upload_date", ""),
                            duration=duration)

            if cfg.use_voiceprint and cfg.voiceprint_path.exists():
                print("성문 대조로 설교·축도 찾는 중")
                vp = Voiceprint.load(cfg.voiceprint_path)
                result = analyze_media(source, vp, Embedder(threads=4),
                                       hop=cfg.vp_hop, threshold=cfg.vp_threshold)
                if not result.sermon:
                    raise ToolError(
                        "성문으로 설교를 찾지 못했습니다.\n"
                        "다른 목사님이거나 음향이 크게 달라진 경우입니다.\n"
                        "[직접 정하기] 로 수동 편집하시거나, 성문을 다시 만드세요.")
                apply_service_result(marker, result, cfg)
                for line in result.reasons:
                    print(f"  · {line}")
            else:
                print("성문이 없어 화자 분리로 진행합니다 (오래 걸립니다)")
                if not wav.exists():
                    fetch.download_audio(meta["webpage_url"] or url, wav)
                segments = diarize(wav, cfg, cache_path=cfg.diar_path(vid))
                det = detect(segments, duration, cfg)
                print(format_detection(det))
                marker.sermon_ai_start, marker.sermon_ai_end = det.sermon.start, det.sermon.end
                if det.benediction:
                    marker.bene_ai_start = det.benediction.start
                    marker.bene_ai_end = det.benediction.end
                marker.confidence = det.confidence
                marker.bene_confidence = det.bene_confidence
                marker.reasons = det.reasons
                marker.pastor_speaker = det.pastor

            transcript: list[dict] = []
            if not with_stt:
                # 구간만 다시 찾을 때 예전에 만든 자막을 버리지 않는다 (있으면 그대로 쓴다)
                transcript = transcribe.load_transcript(cfg.transcript_path(vid))
                if transcript:
                    print(f"  예전에 만든 자막 {len(transcript)}문장을 그대로 씁니다")
            if with_stt:
                if not wav.exists():
                    print("자막용 오디오 준비 중")
                    if local is not None:
                        from core.voiceprint import to_wav
                        to_wav(local, wav)
                    else:
                        fetch.download_audio(meta["webpage_url"] or url, wav)
                print("확정 구간 STT — 오래 걸립니다")
                for start, end, label in marker.ranges():
                    print(f"  {label}: {hms(start)} ~ {hms(end)}")
                    transcript.extend(transcribe.transcribe_range(wav, start, end, cfg, label))
                transcribe.save_transcript(cfg.transcript_path(vid), transcript)
                if transcript:
                    r = marker.sermon_range()
                    if r:
                        s, e, notes = refine_sermon(transcript, r[0], r[1], cfg)
                        marker.sermon_ai_start, marker.sermon_ai_end = s, e
                        for n in notes:
                            print(f"  · {n}")
                    r = marker.bene_range()
                    if r:
                        s, e, notes = refine_benediction(transcript, r[0], r[1], cfg)
                        marker.bene_ai_start, marker.bene_ai_end = s, e
                        for n in notes:
                            print(f"  · {n}")
                    marker.refined = True

            marker.sync_clips_from_detection()
            marker.save(cfg.marker_path(vid))
            print("구간 저장 완료")
            return marker, transcript

        def done(r):
            self._apply_marker(*r)
            if then:
                QTimer.singleShot(0, then)      # 이어서 할 일 (예: 3분 요약 고르기)

        self._run(job, done,
                  title="② 자동으로 찾기",
                  detail=("자막까지 만듭니다 — 20~40분 더 걸립니다" if with_stt
                          else "목사님 목소리를 대조해 설교와 축도를 찾습니다 (1~2분)"),
                  done_message="설교 구간을 찾았습니다",
                  next_step="시각을 확인하고 [3. 설교 영상 만들기]",
                  est_kind="find", est_minutes=getattr(self.range_bar, "duration", 0.0) / 60)

    def transcribe_current(self, then=None) -> None:
        """지금 정해 둔 구간 그대로 자막만 만든다. 구간은 건드리지 않는다.

        [자동으로 찾기] 는 구간을 처음부터 다시 찾으므로, 손으로 다듬어 둔 구간이
        있을 때 자막만 필요하면 이쪽을 쓴다. [3분 요약] 이 자막 없이 불렸을 때 쓴다.
        """
        if not self.marker or not self.marker.ranges():
            return
        marker = self.marker
        local = self.media_path if (self.media_path and self.media_path.exists()) else None
        url = marker.url or ""
        cfg = self._config()
        cfg.whisper_model = "medium"
        total = sum(e - s for s, e, _ in marker.ranges())

        def job():
            wav = cfg.audio_path(marker.video_id)
            if not wav.exists():
                print("자막용 오디오 준비 중")
                if local is not None:
                    from core.voiceprint import to_wav
                    to_wav(local, wav)
                elif url.startswith("http"):
                    fetch.download_audio(url, wav)
                else:
                    raise ToolError("자막을 만들 오디오가 없습니다.\n"
                                    "[내 컴퓨터에서 열기] 로 영상을 다시 열어 주세요.")
            transcript: list[dict] = []
            for start, end, label in marker.ranges():
                print(f"  {label}: {hms(start)} ~ {hms(end)}")
                transcript.extend(transcribe.transcribe_range(wav, start, end, cfg, label))
            transcribe.save_transcript(cfg.transcript_path(marker.video_id), transcript)
            return transcript

        def done(transcript):
            self._apply_marker(marker, transcript)
            if then:
                QTimer.singleShot(0, then)

        self._run(job, done, title="자막 만들기",
                  detail=f"정해 둔 구간 {total / 60:.0f}분의 자막을 만듭니다",
                  done_message="자막을 만들었습니다",
                  est_kind="stt", est_minutes=total / 60, est_model=cfg.whisper_model)

    def on_load(self) -> None:
        cfg = self._config()
        target = self.url_edit.text().strip()
        if target:
            vid = self._video_id(target)
            path = cfg.marker_path(vid)
            if not path.exists():
                QMessageBox.warning(self, "없음", f"저장된 구간이 없습니다: {path}")
                return
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "구간 파일 선택", str(cfg.marker_dir), "JSON (*.json)")
            if not path:
                return
        marker = Marker.load(Path(path))
        self._apply_marker(marker, transcribe.load_transcript(cfg.transcript_path(marker.video_id)))

    def on_save(self) -> None:
        if not self.marker:
            return
        cfg = self._config()
        self.marker.sermon_title = self.sermon_title_edit.text().strip()
        self.marker.corrected = True
        path = self.marker.save(cfg.marker_path(self.marker.video_id))
        self.log(f"저장: {path}")

    # -------- 축도 넣기 / 시작 영상 --------
    def _on_bene_toggled(self, checked: bool) -> None:
        self._save_prefs()
        if self.marker:
            self._snapshot()
        self._apply_bene_choice()

    def _apply_bene_choice(self, quiet: bool = False) -> None:
        """축도 구간을 목록에 넣거나 뺀다. 눈에 보이는 목록이 곧 결과다."""
        if not self.marker:
            return
        want = self.bene_check.isChecked()
        has = any(c.get("label") == "benediction" for c in self.marker.clips)

        if want and not has:
            r = self.marker.bene_range()
            if not r:
                if not quiet:
                    QMessageBox.information(
                        self, "축도를 찾지 못했습니다",
                        "이 영상에서는 축도 구간을 찾지 못했습니다.\n"
                        "[+ 구간 추가] 로 직접 만들어 주세요.")
                self.bene_check.blockSignals(True)
                self.bene_check.setChecked(False)
                self.bene_check.blockSignals(False)
                return
            self.marker.clips.append(
                {"label": "benediction", "start": r[0], "end": r[1]})
            self.marker.clips.sort(key=lambda c: c["start"])
            if not quiet:
                self.log(f"축도를 넣었습니다  {fmt(r[0])} ~ {fmt(r[1])}")
        elif not want and has:
            self.marker.clips = [c for c in self.marker.clips
                                 if c.get("label") != "benediction"]
            if not quiet:
                self.log("축도를 뺐습니다 — 설교만 만듭니다")
        else:
            return
        self._refresh_clips(0)

    def on_pick_intro(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "시작 영상 고르기", "",
            "영상 (*.mp4 *.mov *.avi *.mkv *.wmv *.webm);;모든 파일 (*.*)")
        if not path:
            return
        self.intro_path = path
        self._show_intro()
        self._save_prefs()
        self.log(f"시작 영상: {Path(path).name} — 설교 영상 앞에 붙습니다")

    def on_clear_intro(self) -> None:
        if not self.intro_path:
            return
        self.intro_path = ""
        self._show_intro()
        self._save_prefs()
        self.log("시작 영상을 뺐습니다")

    def _show_intro(self) -> None:
        name = Path(self.intro_path).name if self.intro_path else "없음"
        self.intro_label.setText(name if len(name) <= 28 else name[:26] + "…")

    def _ask_title(self) -> bool:
        """제목이 비었으면 한 번 물어본다. 계속해도 되면 True."""
        if self.sermon_title_edit.text().strip():
            return True
        answer = QMessageBox.question(
            self, "설교 제목이 비어 있습니다",
            "설교 제목을 적으면 그 제목으로 파일이 저장됩니다.\n"
            "비워 두면 날짜와 영상 번호로 저장됩니다.\n\n"
            "[Yes] 이대로 만들기   [No] 제목을 적고 오기",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            self.sermon_title_edit.setFocus()
            return False
        return True

    def on_export_mp3(self) -> None:
        """설교를 소리만 mp3 로. 축도는 빼고 설교 구간만 넣는다."""
        if not self.marker or not self.marker.clips:
            QMessageBox.information(
                self, "구간을 먼저 정하세요",
                "영상을 보면서 [여기가 시작]과 [여기가 끝]을 눌러\n"
                "설교 구간을 먼저 정해 주세요.")
            return

        src = self.media_path
        if not src or not Path(src).exists():
            QMessageBox.information(
                self, "영상이 필요합니다",
                "mp3 는 받아 둔 영상에서 소리만 뽑습니다.\n"
                "[영상 가져오기] 나 [내 컴퓨터에서 열기] 로 영상을 먼저 여세요.")
            return

        if not self._ask_title():
            return

        self.on_save()
        cfg = self._config()
        marker = self.marker
        ranges = [(a, b) for a, b, _ in marker.ranges()]
        total = sum(b - a for a, b in ranges)
        has_bene = any(label == "benediction" for _, _, label in marker.ranges())

        if total < 5.0:
            QMessageBox.information(
                self, "구간이 너무 짧습니다",
                f"설교 구간이 {total:.1f}초밖에 되지 않습니다.\n"
                "구간을 다시 확인해 주세요.")
            return

        def job():
            return export_mod.export_mp3(marker, cfg, Path(src), sermon_only=False)

        def done(result):
            lines = "\n".join(f"{k}: {v}" for k, v in result.items())
            self.log(lines)
            box = QMessageBox(self)
            box.setWindowTitle("mp3 완료")
            box.setText(lines)
            open_btn = box.addButton("폴더 열기", QMessageBox.ActionRole)
            box.addButton("닫기", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is open_btn:
                folder = Path(result["mp3"]).parent
                QDesktopServices.openUrl(_QUrl.fromLocalFile(str(folder.resolve())))

        self._run(job, done,
                  title="설교 mp3 만들기",
                  detail=(f"{total / 60:.0f}분   소리만 뽑습니다   "
                          + ("설교 + 축도" if has_bene else "설교만")),
                  done_message="mp3 를 만들었습니다",
                  next_step="[폴더 열기] 로 확인하세요",
                  est_kind="export", est_minutes=total / 60)

    def on_export(self) -> None:
        if not self.marker or not self.marker.clips:
            QMessageBox.information(
                self, "구간을 먼저 정하세요",
                "영상을 보면서 [여기가 시작]과 [여기가 끝]을 눌러\n"
                "설교 구간을 먼저 정해 주세요.")
            return

        if not self.sermon_title_edit.text().strip():
            answer = QMessageBox.question(
                self, "설교 제목이 비어 있습니다",
                "설교 제목을 적으면 그 제목으로 파일이 저장됩니다.\n"
                "비워 두면 날짜와 영상 번호로 저장됩니다.\n\n"
                "[Yes] 이대로 만들기   [No] 제목을 적고 오기",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self.sermon_title_edit.setFocus()
                return

        tiny = [(s, e, label) for s, e, label in self.marker.ranges() if e - s < 5.0]
        if tiny:
            names = ", ".join(f"{LABEL_KO.get(l, l)} {(e-s):.1f}초" for s, e, l in tiny)
            answer = QMessageBox.question(
                self, "구간이 너무 짧습니다",
                f"5초도 안 되는 구간이 있습니다: {names}\n"
                "실수로 뭉개진 구간일 수 있습니다.\n\n"
                "[Yes] 그래도 내보내기   [No] 취소하고 확인하기",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self.log("내보내기를 멈췄습니다 — [AI가 찾은 구간으로] 또는 Ctrl+Z 로 되돌려 보세요")
                return

        total = sum(e - s for s, e, _ in self.marker.ranges())
        if self.exact_check.isChecked() and total > 600:
            mins = total / 60
            answer = QMessageBox.question(
                self, "시간이 걸립니다",
                f"{mins:.0f}분 분량을 정확히 자르려면 다시 인코딩해야 합니다.\n"
                f"CPU에서 {mins*0.4:.0f}~{mins*0.8:.0f}분 걸릴 수 있습니다.\n\n"
                "[Yes] 그대로 정확히 자르기\n"
                "[No] 빠른 컷으로 바꾸기 (수 초, 컷 지점이 몇 초 밀릴 수 있음)",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Yes)
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.No:
                self.exact_check.setChecked(False)

        self.on_save()
        cfg = self._config()
        marker = self.marker
        transcript = self.transcript
        src = self.media_path
        exact = self.exact_check.isChecked()

        def job():
            if src and src.exists() and src.suffix.lower() not in (".wav", ".mp3", ".m4a"):
                print(f"로컬 영상에서 자릅니다: {src.name}")
                return export_local_wrapper(marker, cfg, src, transcript, exact)
            print("로컬 영상이 없어 유튜브에서 구간을 받아옵니다")
            return export_mod.export(marker, cfg, transcript)

        def done(result):
            lines = "\n".join(f"{k}: {v}" for k, v in result.items())
            self.log(lines)
            box = QMessageBox(self)
            box.setWindowTitle("내보내기 완료")
            box.setText(lines)
            open_btn = box.addButton("폴더 열기", QMessageBox.ActionRole)
            box.addButton("닫기", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is open_btn:
                folder = Path(result["video"]).parent
                QDesktopServices.openUrl(_QUrl.fromLocalFile(str(folder.resolve())))

        mins = total / 60
        self._run(job, done,
                  title="③ 설교 영상 만들기",
                  detail=(f"{mins:.0f}분 분량   "
                          + ("정확히 자르기 (시간이 걸립니다)" if exact else "빠른 컷")
                          + ("   소리 크기 맞춤" if self.normalize_check.isChecked() else "")
                          + (f"   잡음 제거 {self.denoise_box.currentText().split()[0]}"
                             if self.DENOISE_CHOICES.get(self.denoise_box.currentText()) else "")
                          + (f"   자막 {self.subtitle_box.currentText().split(' (')[0]}"
                             if transcript else "")
                          + ("   자막 태우기는 시간이 오래 걸립니다"
                             if transcript and self._config().subtitle_mode == "burn"
                             else "")),
                  done_message="영상을 만들었습니다",
                  next_step="[폴더 열기] 로 결과를 확인하세요",
                  est_kind="export", est_minutes=mins)

    # ============================================================== 표시
    def _reset_session(self) -> None:
        """다른 영상으로 넘어갈 때 이전 작업 내용을 전부 비운다.

        이걸 빼먹으면 새 영상을 열어도 앞 영상의 구간과 video_id 가 남아서
        엉뚱한 이름으로, 엉뚱한 구간이 저장된다.
        """
        self.player.stop()
        self.marker = None
        self.transcript = []
        self.media_path = None
        self.stop_at = None
        self.edit_edge = "start"

        self.sum_marks = []
        self.sum_outline = hl_mod.Outline()
        self.sum_pauses = []
        self.sum_undo = []
        self._reset_summary_blocks()
        self.sum_list.blockSignals(True)
        self.sum_list.clear()
        self.sum_list.blockSignals(False)
        self._update_summary_meter()

        self.clip_list.blockSignals(True)
        self.clip_list.clear()
        self.clip_list.blockSignals(False)
        self.subtitle_list.clear()
        self.start_edit.clear()
        self.end_edit.clear()
        self.conf_label.setText("")
        self.sermon_title_edit.clear()
        self.title_label.setText("영상 없음")
        self.range_bar.set_data(0.0, [], -1)
        self.zoom_bar.set_clip(0.0, 0.0)
        self.zoom_bar.set_edge("start", 0.0)
        self._update_span_label()

    def _switch_target(self, video_id: str) -> None:
        """다루는 영상이 바뀌었으면 초기화한다."""
        if self.marker and self.marker.video_id != video_id:
            self.log(f"다른 영상으로 전환 — 이전 작업({self.marker.video_id})을 비웁니다")
            self._reset_session()

    def _on_subtitle_edited(self, item: QListWidgetItem) -> None:
        """자막 줄을 두 번 클릭해 고쳤을 때. 고친 글을 저장까지 해 둔다."""
        idx = item.data(Qt.UserRole + 1)
        if idx is None or not (0 <= idx < len(self.transcript)):
            return
        seg = self.transcript[idx]
        text = item.text()
        m = re.match(r"^\s*\[[^\]]*\]\s*(.*)$", text, re.S)
        if m:
            text = m.group(1)
        text = " ".join(text.split())
        changed = bool(text) and text != seg["text"]
        if changed:
            seg["text"] = text
            if self.marker:
                try:
                    transcribe.save_transcript(
                        self._config().transcript_path(self.marker.video_id),
                        self.transcript)
                except OSError:
                    pass
            self.log(f"자막 고침  [{hms(seg['start'])}]  {text}")
        self.subtitle_list.blockSignals(True)
        item.setText(f"[{hms(seg['start'])}] {seg['text']}")
        self.subtitle_list.blockSignals(False)

    def _apply_marker(self, marker: Marker, transcript: list[dict]) -> None:
        marker.sync_clips_from_detection()
        self.marker = marker
        self._apply_bene_choice(quiet=True)
        self.transcript = transcript or []
        self.title_label.setText(f"{marker.title}\n[{marker.video_id}]")
        self.sermon_title_edit.setText(marker.sermon_title or "")
        self.conf_label.setText(
            f"AI 신뢰도  설교 {marker.confidence} / 축도 {marker.bene_confidence}")

        self.subtitle_list.blockSignals(True)
        self.subtitle_list.clear()
        for i, seg in enumerate(self.transcript):
            item = QListWidgetItem(f"[{hms(seg['start'])}] {seg['text']}")
            item.setData(Qt.UserRole, seg["start"])
            item.setData(Qt.UserRole + 1, i)
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            self.subtitle_list.addItem(item)
        self.subtitle_list.blockSignals(False)

        self._restore_summary(marker)
        self._refresh_clips(0)

        if not self.media_path:
            cfg = self._config()
            for candidate in (cfg.preview_path(marker.video_id), cfg.audio_path(marker.video_id)):
                if candidate.exists():
                    self._load_media(candidate)
                    break

    def _restore_summary(self, marker: Marker) -> None:
        """저장해 둔 요약 선택을 화면에 되살린다.

        쉼도 여기서 다시 찾는다. 안 찾으면 눈금이 거짓말을 한다 — 컷을 쉼에
        맞추면서 구간이 앞뒤로 벌어지기 때문에, 스냅을 빼고 세면 실제보다
        30초 넘게 짧게 나온다(실측 2분 55초짜리가 2분 22초로 떴다).
        36분치를 훑는 데 1초도 안 걸리므로 불러오기가 느려지지 않는다.
        """
        self.sum_marks = []
        self.sum_outline = hl_mod.Outline()
        self.sum_pauses = []
        self.sum_undo = []
        self._reset_summary_blocks()
        rng = marker.sermon_range()
        if not rng or not self.transcript or not marker.summary_picks:
            self.sum_list.blockSignals(True)
            self.sum_list.clear()
            self.sum_list.blockSignals(False)
            self._update_summary_meter()
            return
        marks = hl_mod.build_marks(self.transcript, rng[0], rng[1])
        if not marks:
            self._update_summary_meter()
            return

        wav = self._config().audio_path(marker.video_id)
        if wav.exists():
            try:
                self.sum_pauses = pause_mod.find_pauses(wav, rng[0], rng[1])
            except (ToolError, OSError) as exc:
                self.log(f"쉼을 찾지 못했습니다 — 길이가 조금 다르게 보일 수 있습니다: {exc}")

        self.sum_outline = hl_mod.find_outline(marks, rng[0], rng[1])
        hl_mod.score_marks(marks, hl_mod.keywords(marks))
        for m, on in zip(marks, marker.summary_chosen([x.start for x in marks])):
            m.chosen = on
        self.sum_marks = marks
        self._fill_summary_list()
        self.log(f"3분 요약 — 저장해 둔 문장 {sum(1 for m in marks if m.chosen)}개를 불러왔습니다")

    def _load_media(self, path: Path) -> None:
        self.media_path = Path(path)
        self.player.setSource(QUrl.fromLocalFile(self.media_path.resolve().as_posix()))
        self.btn_play.setText("▶ 재생")
        self.log(f"열기: {path.name}")
        if self.marker and not self.marker.duration:
            QTimer.singleShot(800, self._refresh_bar)
        self._update_hint()


def export_local_wrapper(marker, cfg, src, transcript, exact):
    return export_mod.export_local(marker, cfg, src, transcript, reencode=exact)


def apply_theme(app: QApplication, name: str = "dark") -> None:
    """화면 색을 통째로 갈아 끼운다 (theme.py 의 토큰).

    OS 가 다크 모드든 아니든 늘 같은 화면으로 고정한다. PC 마다 달라 보이면
    설명서와 어긋나기 때문이다.

    팔레트와 스타일시트를 **둘 다** 설정한다. 스타일시트가 안 닿는 곳(기본
    대화상자, 직접 그리는 위젯의 기본 펜 색)은 팔레트가 맡는다.
    """
    global T
    T = theme_mod.use(name)        # 직접 그리는 위젯들이 theme.current() 로 읽는다
    app.setStyle("Fusion")

    p = QPalette()
    p.setColor(QPalette.Window, _c(T.panel_bg))
    p.setColor(QPalette.WindowText, _c(T.text))
    p.setColor(QPalette.Base, _c(T.field_bg))
    p.setColor(QPalette.AlternateBase, _c(T.row_alt))
    p.setColor(QPalette.Text, _c(T.text))
    p.setColor(QPalette.PlaceholderText, _c(T.text_faint))
    p.setColor(QPalette.Button, _c(T.button_bg))
    p.setColor(QPalette.ButtonText, _c(T.text))
    p.setColor(QPalette.Highlight, _c(T.accent))
    p.setColor(QPalette.HighlightedText, _c(T.text_on_accent))
    p.setColor(QPalette.ToolTipBase, _c(T.header_bg))
    p.setColor(QPalette.ToolTipText, _c(T.text))
    p.setColor(QPalette.Link, _c(T.hot_text))

    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, _c(T.disabled_text))

    app.setPalette(p)
    app.setStyleSheet(theme_mod.stylesheet(T))

    font = app.font()
    font.setPointSize(T.font_pt)
    app.setFont(font)


def saved_theme() -> str:
    """지난번에 고른 화면 모양. 처음이면 어두운 화면."""
    name = str(QSettings("창의진교회", "SermonTool").value("theme", "dark"))
    return name if name in theme_mod.THEMES else "dark"


def main() -> int:
    app = QApplication(sys.argv)
    apply_theme(app, saved_theme())
    win = MainWindow()
    if win._small_screen:
        # 노트북처럼 세로가 좁으면 처음부터 최대화한다. Windows 가 작업 표시줄과
        # 제목 표시줄을 빼고 딱 맞춰 주므로 아래가 가려질 일이 없다.
        win.showMaximized()
    else:
        win.show()
        QTimer.singleShot(0, win._keep_on_screen)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
