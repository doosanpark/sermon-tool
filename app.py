#!/usr/bin/env python3
"""창의진교회 설교영상 편집기 — 구간 찾기 · 미세 조정 · 내보내기.

    pip install PySide6
    python app.py
"""
from __future__ import annotations

import contextlib
import copy
import io
import re
import sys
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
from core import fetch, highlight as hl_mod, pauses as pause_mod, transcribe
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
        p.fillRect(self.rect(), QColor(235, 236, 240))

        # 10분 눈금
        if self.duration > 0:
            p.setPen(QPen(QColor(205, 207, 212), 1))
            step = 600
            t = step
            while t < self.duration:
                x = int(self._x(t))
                p.drawLine(x, self.height() - 10, x, self.height())
                t += step

        for i, (start, end, label) in enumerate(self.ranges):
            x0, x1 = self._x(start), self._x(end)
            base = QColor(64, 132, 220) if label != "benediction" else QColor(72, 176, 120)
            if i != self.selected and self.selected >= 0:
                base = base.lighter(155)
            p.fillRect(int(x0), 6, max(int(x1 - x0), 3), self.height() - 20, base)

            if x1 - x0 > 42:
                p.setPen(QColor(255, 255, 255))
                f = QFont()
                f.setPointSize(8)
                p.setFont(f)
                p.drawText(int(x0) + 5, 20, LABEL_KO.get(label, label))

        # 선택된 구간의 양 끝 손잡이 — 구간이 1픽셀이어도 보이게
        if 0 <= self.selected < len(self.ranges):
            s, e, _ = self.ranges[self.selected]
            for tsec, mark in ((s, "["), (e, "]")):
                x = int(self._x(tsec))
                p.setPen(QPen(QColor(20, 60, 140), 2))
                p.drawLine(x, 2, x, self.height() - 12)
                p.fillRect(x - 3, 2, 6, 8, QColor(20, 60, 140))

        if self.duration > 0:
            p.setPen(QPen(QColor(215, 45, 45), 2))
            x = int(self._x(self.position))
            p.drawLine(x, 0, x, self.height())

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
        p.fillRect(self.rect(), QColor(246, 247, 250))

        # 구간 안쪽을 칠해 어디가 남는지 보여준다
        ex = int(self._x(self.edge_time))
        keep = QColor(64, 132, 220, 55)
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
                p.setPen(QPen(QColor(180, 183, 190), 1))
                p.drawLine(x, h - 16, x, h - (4 if major else 10))
                if major:
                    p.setPen(QColor(110, 113, 120))
                    p.drawText(x + 2, h - 4, fmt(max(tick, 0)))
            tick += 1

        px = int(self._x(self.position))
        if 0 <= px <= w:
            p.setPen(QPen(QColor(215, 45, 45), 1, Qt.DashLine))
            p.drawLine(px, 0, px, h - 16)

        # 반대쪽 경계도 창 안에 있으면 흐리게 그린다
        other_t = self.clip_end if self.which == "start" else self.clip_start
        other_x = int(self._x(other_t))
        if 0 <= other_x <= w:
            p.setPen(QPen(QColor(120, 150, 200), 2, Qt.DotLine))
            p.drawLine(other_x, 0, other_x, h - 16)
            p.setPen(QColor(110, 140, 190))
            p.drawText(other_x + 4, 26, "끝" if self.which == "start" else "시작")

        # 조정 중인 경계 — 잡기 쉽게 크게 그린다
        p.setPen(QPen(QColor(30, 90, 190), 3))
        p.drawLine(ex, 18, ex, h - 16)
        p.fillRect(ex - 9, 18, 18, 16, QColor(30, 90, 190))
        p.setPen(QColor(255, 255, 255))
        f2 = QFont(); f2.setPointSize(8); f2.setBold(True)
        p.setFont(f2)
        p.drawText(ex - 6, 31, "↔")
        p.setFont(f)

        # 항상 보이는 요약 — 시작과 끝을 같이 확인할 수 있어야 한다
        length = max(self.clip_end - self.clip_start, 0.0)
        mark_s = "▶" if self.which == "start" else "  "
        mark_e = "▶" if self.which == "end" else "  "
        p.setPen(QColor(60, 63, 70))
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
        self.setWindowTitle("창의진교회 설교영상 편집기")
        self._fit_to_screen()

        self.cfg = Config()
        self.cfg.ensure_dirs()
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

        self._build_ui()
        self._compact_for_small_screen()
        self._build_shortcuts()
        self._set_busy(False)
        self._load_prefs()
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
                "font-size:14px; font-weight:bold; background:#ffffff; color:#1c1e21;")
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

    # ============================================================== UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(8)

        root.addWidget(self._build_toolbar())

        editor_scroll = QScrollArea()
        editor_scroll.setWidget(self._build_editor())
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setFrameShape(QFrame.NoFrame)
        editor_scroll.setMinimumWidth(360)
        editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        middle = QSplitter(Qt.Horizontal)
        middle.addWidget(self._build_player())
        middle.addWidget(editor_scroll)
        middle.setSizes([880, 420])
        root.addWidget(middle, 5)

        self.tabs = QTabWidget()
        self.subtitle_list = QListWidget()
        self.subtitle_list.setAlternatingRowColors(True)
        self.subtitle_list.setEditTriggers(QListWidget.DoubleClicked)
        self.subtitle_list.itemChanged.connect(self._on_subtitle_edited)
        self.subtitle_list.setToolTip(
            "한 번 클릭: 그 시각으로 이동해서 들어보기\n"
            "두 번 클릭: 받아쓰기가 틀린 글자를 그 자리에서 고치기\n"
            "고친 내용은 .srt 와 영상에 넣는 자막에 그대로 들어갑니다.")
        self.tabs.addTab(self.subtitle_list, "자막 (클릭: 이동 · 두 번 클릭: 고치기)")
        self.summary_tab = self._build_summary_tab()
        self.tabs.addTab(self.summary_tab, "3분 요약")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setStyleSheet(
            "font-family:Consolas,'D2Coding',monospace; font-size:11px;"
            "background:#ffffff; color:#1c1e21;")
        self.tabs.addTab(self.log_view, "진행 로그")
        self.tabs.setMinimumHeight(130)
        root.addWidget(self.tabs, 2)

        self.hint = QLabel()
        self.hint.setStyleSheet("color:#555; padding:2px;")
        root.addWidget(self.hint)

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

    def _build_toolbar(self) -> QWidget:
        box = QGroupBox("1. 영상 준비")
        lay = QHBoxLayout(box)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("유튜브 주소를 여기에 붙여넣으세요")
        self.url_edit.setMinimumHeight(36)
        self.url_edit.setStyleSheet("font-size:13px; padding:4px;")
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
            b.setStyleSheet("font-size:13px; font-weight:bold;")
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
        self.btn_quick.clicked.connect(lambda: self.on_analyze(with_stt=self.stt_check.isChecked()))
        self.btn_load.setToolTip("저장해 둔 구간 정보를 되살립니다. 영상이 아니라 작업 내용입니다.")
        self.btn_load.clicked.connect(self.on_load)
        return box

    def _build_player(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)

        self.video = QVideoWidget()
        self.video.setMinimumHeight(180)
        self.video.setStyleSheet("background:#111;")
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
        self.btn_play.setStyleSheet("font-size:14px; font-weight:bold;")
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
            "font-size:15px; font-weight:bold; padding:6px; color:#1c1e21;"
            "background:#eef3fb; border:1px solid #c6d6ee; border-radius:4px;")
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
        self.zoom_hint.setStyleSheet("color:#666;")
        edge_row.addWidget(self.zoom_hint)
        lay.addLayout(edge_row)

        self.zoom_bar = ZoomBar()
        lay.addWidget(self.zoom_bar)

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
        self.title_label.setStyleSheet("font-weight:bold; font-size:11px;")
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
            "background:#fbf7e8; border:1px solid #e6dcb8; border-radius:4px;"
            "padding:4px 6px; color:#5a4f2a; font-size:11px;")
        guide.setMaximumHeight(42)
        lay.addWidget(guide)

        lay.addWidget(self._edge_group("start"))
        lay.addWidget(self._edge_group("end"))

        self.conf_label = QLabel("")
        self.conf_label.setStyleSheet("color:#666; font-size:11px;")
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
        self.btn_export.setStyleSheet(
            "font-size:14px; font-weight:bold; background:#2f7d43; color:white;"
            "border-radius:6px;")
        self.btn_mp3 = QPushButton("설교 mp3")
        self.btn_mp3.setFixedHeight(42)
        self.btn_mp3.setStyleSheet(
            "font-size:13px; font-weight:bold; background:#ffffff; color:#2f7d43;"
            "border:2px solid #2f7d43; border-radius:6px;")
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
        self.btn_advanced.setStyleSheet(
            "color:#666; border:none; text-align:left; font-size:11px;")
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
        self.intro_label.setStyleSheet("color:#555;")
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
            "font-size:16px; font-weight:bold; background:#ffffff; color:#1c1e21;")
        lay.addWidget(edit)

        here = QPushButton(f"여기가 {'시작' if is_start else '끝'}")
        here.setFixedHeight(30)
        here.setStyleSheet("font-size:13px; font-weight:bold;")
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
        check.setStyleSheet("font-size:11px;")
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

    def _startup_check(self) -> None:
        """시작할 때 무엇이 되고 무엇이 안 되는지 확인해 로그에 남긴다.

        버튼이 이유 없이 비활성처럼 보이는 일이 없도록, 못 쓰는 기능은
        비활성화하고 툴팁에 이유를 적는다.
        """
        import importlib.util
        from core.util import find_tool
        from core.voiceprint import EMBED_MODEL, MODEL_DIR_CANDIDATES

        cfg = self._config()
        self.log_step("창의진교회 설교영상 편집기", "프로그램 구성을 확인합니다")

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
    def log(self, text: str) -> None:
        self.log_view.appendPlainText(text)
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def log_step(self, title: str, detail: str = "") -> None:
        """새 작업이 시작될 때. 앞 작업과 확실히 갈라 보이게 한다."""
        now = time.strftime("%H:%M:%S")
        self.log("")
        self.log("━" * 60)
        self.log(f"  {title}          {now}")
        if detail:
            self.log(f"  {detail}")
        self.log("━" * 60)

    def log_done(self, message: str, seconds: float | None = None,
                 next_step: str = "") -> None:
        """작업이 끝났을 때. 얼마나 걸렸는지와 다음에 할 일을 알려 준다."""
        took = ""
        if seconds is not None:
            took = (f"  ({seconds:.0f}초)" if seconds < 60
                    else f"  ({int(seconds // 60)}분 {int(seconds % 60)}초)")
        self.log("")
        self.log(f"  ✔ {message}{took}")
        if next_step:
            self.log(f"  → 다음:  {next_step}")
        self.log("")

    def _set_busy(self, busy: bool) -> None:
        for w in (self.btn_fetch, self.btn_open, self.btn_manual, self.btn_quick,
                  self.btn_load, self.btn_export, self.btn_mp3, self.btn_save,
                  self.btn_undo, self.btn_restore_ai):
            w.setEnabled(not busy)

        # 구간을 만지는 것도 잠근다. 작업이 끝나면 _apply_marker 가 구간을 통째로
        # 새로 넣기 때문에, 도는 동안 손잡이를 끌면 그 편집이 소리 없이 사라진다.
        # 실제로 자막 만드는 35분 동안 손잡이를 네 번 끄신 일이 있었다.
        for w in (self.range_bar, self.zoom_bar, self.clip_list,
                  self.btn_add, self.btn_del, self.btn_up, self.btn_down,
                  self.btn_edge_start, self.btn_edge_end,
                  self.btn_sum_pick, self.btn_sum_undo, self.btn_sum_make,
                  self.btn_sum_mp3,
                  self.pick_mode_box, self.btn_ai_get, self.sum_list):
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

        widgets = (self.subtitle_box, self.denoise_box,
                   self.normalize_check, self.bene_check, self.pick_mode_box)
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
        self.bene_check.setChecked(include_bene)
        for label, mode in self.PICK_CHOICES.items():
            if mode == pick:
                self.pick_mode_box.setCurrentText(label)
                break
        for w in widgets:
            w.blockSignals(False)

        self.intro_path = intro if intro and Path(intro).exists() else ""
        self._show_intro()

        self.subtitle_box.currentTextChanged.connect(self._save_prefs)
        self.denoise_box.currentTextChanged.connect(self._save_prefs)
        self.normalize_check.toggled.connect(self._save_prefs)
        self.bene_check.toggled.connect(self._save_prefs)
        self._update_ai_status()

    def _save_prefs(self, *_args) -> None:
        s = QSettings("창의진교회", "SermonTool")
        s.setValue("subtitle_mode",
                   self.SUBTITLE_CHOICES.get(self.subtitle_box.currentText(), "embed"))
        s.setValue("denoise",
                   self.DENOISE_CHOICES.get(self.denoise_box.currentText(), "light"))
        s.setValue("normalize", self.normalize_check.isChecked())
        s.setValue("include_bene", self.bene_check.isChecked())
        s.setValue("intro_path", self.intro_path)
        if hasattr(self, "pick_mode_box"):
            s.setValue("pick_mode",
                       self.PICK_CHOICES.get(self.pick_mode_box.currentText(), "embed"))

    def _config(self) -> Config:
        cfg = Config()
        if hasattr(self, "normalize_check"):
            cfg.normalize_audio = self.normalize_check.isChecked()
            cfg.denoise = self.DENOISE_CHOICES.get(self.denoise_box.currentText(), "light")
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
             done_message: str = "끝났습니다", next_step: str = "") -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "실행 중", "이미 작업이 돌고 있습니다.")
            return
        self._set_busy(True)
        self.tabs.setCurrentWidget(self.log_view)   # 진행 로그 탭을 바로 보여준다
        self.log_step(title, detail)
        self.hint.setText(f"{title} 하는 중입니다...  아래 진행 로그를 보세요.")
        QApplication.processEvents()

        started = time.monotonic()

        def finished(result):
            self._set_busy(False)
            self.log_done(done_message, time.monotonic() - started, next_step)
            on_done(result)
            self._update_hint()

        self.worker = Worker(fn, self)
        self.worker.log.connect(self.log)
        self.worker.done.connect(finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

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

    def on_analyze(self, with_stt: bool) -> None:
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

        self._run(job, lambda r: self._apply_marker(*r),
                  title="② 자동으로 찾기",
                  detail=("자막까지 만듭니다 — 20~40분 더 걸립니다" if with_stt
                          else "목사님 목소리를 대조해 설교와 축도를 찾습니다 (1~2분)"),
                  done_message="설교 구간을 찾았습니다",
                  next_step="시각을 확인하고 [3. 설교 영상 만들기]")

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
                  next_step="[폴더 열기] 로 확인하세요")

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
                  next_step="[폴더 열기] 로 결과를 확인하세요")

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


def apply_light_theme(app: QApplication) -> None:
    """OS 테마와 무관하게 늘 같은 밝은 화면으로 고정한다.

    윈도우가 다크 모드면 Qt 가 어두운 팔레트를 쓰는데, 타임라인·정밀 조정 바를
    밝은 색으로 직접 그리고 있어 화면이 반쪽만 어두워진다. PC 마다 화면이 달라
    보이면 설명서와도 어긋나므로 밝은 쪽으로 통일한다.
    """
    app.setStyle("Fusion")

    p = QPalette()
    p.setColor(QPalette.Window, QColor(245, 246, 248))
    p.setColor(QPalette.WindowText, QColor(28, 30, 33))
    p.setColor(QPalette.Base, QColor(255, 255, 255))
    p.setColor(QPalette.AlternateBase, QColor(240, 241, 244))
    p.setColor(QPalette.Text, QColor(28, 30, 33))
    p.setColor(QPalette.PlaceholderText, QColor(138, 143, 152))
    p.setColor(QPalette.Button, QColor(236, 238, 241))
    p.setColor(QPalette.ButtonText, QColor(28, 30, 33))
    p.setColor(QPalette.Highlight, QColor(47, 111, 208))
    p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ToolTipBase, QColor(255, 255, 225))
    p.setColor(QPalette.ToolTipText, QColor(20, 20, 20))
    p.setColor(QPalette.Link, QColor(47, 111, 208))

    grey = QColor(150, 154, 160)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, grey)

    app.setPalette(p)
    app.setStyleSheet(
        "QGroupBox { font-weight: bold; border: 1px solid #d5d8de;"
        " border-radius: 5px; margin-top: 10px; padding-top: 8px; }"
        "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }"
        "QPushButton { padding: 4px 10px; }"
        "QPushButton:disabled { color: #9a9ea6; }"
    )


def main() -> int:
    app = QApplication(sys.argv)
    apply_light_theme(app)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
