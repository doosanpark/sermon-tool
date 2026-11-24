"""[숏폼 편집] 편집기 (2026-11-02).

요약 영상(세로 1080x1920)의 모양을 고르고, 소리와 함께 움직이는 미리 보기로 보며
다듬는 칸이다. 사용자가 보여 준 다른 교회 릴스 두 개(노란 상자 제목 · 아래 제목 띠)와
교회 안 의견(곤색 띠 · 아래 로고 · 쉬운 질문형 제목)에서 나왔다 — core/reels.py 참고.

    ┌ 모양 틀 ─────┐┌── 미리 보기 9:16 ──┐┌ 끌리는 제목 [AI 추천] ─┐
    │ 그림으로 고름 ││ 끌면 목사님 자리,   ││ 제목 모양 · 자막 ·      │
    │               ││ 휠은 크기           ││ 화면 · 로고 · 만들기    │
    └───────────────┘└ ⏮ ◀ ▶ ▶  막대 ─────┘└────────────────────────┘

**보이는 그대로 나온다.** 띠·제목·로고·자막은 여기의 paint_overlay / paint_caption 이
그리고, 만들 때도 같은 함수로 PNG 를 그려 영상에 얹는다 (core/shorts.build 의 look).
목사님 자리는 shorts.crop_rect 와 같은 식으로 자른다.
"""
from __future__ import annotations

import copy
import json
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import (QElapsedTimer, QPointF, QRectF, QSettings, QSize, Qt, QTimer,
                            QUrl, Signal)
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QIcon, QImage,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QFileDialog, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QPushButton, QScrollArea,
                               QSizePolicy, QSlider, QSpinBox, QSplitter, QVBoxLayout,
                               QWidget)

import theme as theme_mod
from core import reels as R
from core import shorts as S

W, H = R.W, R.H
ORG, APP = "창의진교회", "SermonTool"
VERSE_COLOR = "#F3E6CC"

COLORS = [("짙은 곤색", R.NAVY), ("검정", "#000000"), ("짙은 회색", "#1E1F22"),
          ("노랑", "#FFD43B"), ("흰색", "#FFFFFF"), ("하늘색", "#7EC8F2"),
          ("다른 색…", "")]


def _fmt(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}.{int((t - int(t)) * 10)}"


# ====================================================================== 글꼴
_FAMILY: str | None = None


def family() -> str:
    """완성본과 같은 한글 굵은 글꼴 (shorts.find_font) 을 Qt 에 올린다."""
    global _FAMILY
    if _FAMILY is None:
        _FAMILY = "Malgun Gothic"
        font = S.find_font()
        if font is not None:
            fid = QFontDatabase.addApplicationFont(str(font))
            fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if fams:
                _FAMILY = fams[0]
    return _FAMILY


def _font(px: int, bold: bool = True) -> QFont:
    f = QFont(family())
    f.setPixelSize(max(8, int(px)))
    f.setBold(bold)
    return f


def _wrap(text: str, fm: QFontMetricsF, max_w: float, max_lines: int) -> list[str]:
    """띄어쓰기 단위로 줄을 나눈다. 사람이 넣은 줄바꿈은 그대로 둔다."""
    out: list[str] = []
    for para in (text or "").split("\n"):
        words: list[str] = []
        for w in para.split():
            # 띄어 쓰지 않은 긴 말(예: 내부의무너짐을다루지않으면)은 글자 단위로 나눈다
            while fm.horizontalAdvance(w) > max_w and len(w) > 1:
                k = len(w)
                while k > 1 and fm.horizontalAdvance(w[:k]) > max_w:
                    k -= 1
                words.append(w[:k])
                w = w[k:]
            words.append(w)
        line = ""
        for w in words:
            cand = f"{line} {w}".strip()
            if line and fm.horizontalAdvance(cand) > max_w:
                out.append(line)
                line = w
            else:
                line = cand
        if line:
            out.append(line)
    if len(out) > max_lines:
        out = out[:max_lines]
        last = out[-1]
        while last and fm.horizontalAdvance(last + "…") > max_w:
            last = last[:-1]
        out[-1] = last + "…"
    return out


def _text_path(p: QPainter, x: float, y: float, text: str, font: QFont, fill: QColor,
               outline: QColor | None = None, width: float = 0.0) -> None:
    """글자를 윤곽선과 함께 (밝은 화면 위에서도 읽히게)."""
    path = QPainterPath()
    path.addText(QPointF(x, y), font, text)
    if outline is not None and width > 0:
        p.strokePath(path, QPen(outline, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.fillPath(path, fill)


# ====================================================================== 그리기
def paint_video(p: QPainter, look: R.Look, frame: QImage | None,
                src_size: tuple[int, int]) -> None:
    """영상 자리에 목사님을 자른 화면을 그린다 (shorts.crop_rect 와 같은 식)."""
    y0, ah = look.area()
    p.fillRect(QRectF(0, 0, W, H), QColor(look.bg))
    target = QRectF(0, y0, W, ah)
    if frame is None or frame.isNull() or src_size[0] <= 0:
        p.fillRect(target, QColor("#2a2d33"))
        p.setPen(QColor("#9aa0a6"))
        p.setFont(_font(40, False))
        p.drawText(target, Qt.AlignCenter, "영상 화면을 불러오는 중…")
        return
    cw, ch, cx, cy = look.crop(src_size[0], src_size[1])
    kx, ky = frame.width() / src_size[0], frame.height() / src_size[1]
    p.drawImage(target, frame, QRectF(cx * kx, cy * ky, cw * kx, ch * ky))


def paint_overlay(p: QPainter, look: R.Look, hook: str, verse: str,
                  logo: QImage | None) -> None:
    """띠 · 끌리는 제목 · 로고 — 화면 내내 그대로인 것들 (투명 바탕 위에)."""
    if look.layout == "bands":
        p.fillRect(QRectF(0, 0, W, R.BANDS_TOP), QColor(look.bg))
        p.fillRect(QRectF(0, R.BANDS_TOP + R.BANDS_VIDEO_H, W,
                          H - R.BANDS_TOP - R.BANDS_VIDEO_H), QColor(look.bg))
    verse = verse if look.show_verse else ""
    hook = (hook or "").strip()
    if hook or verse:
        if look.hook_style == "box":
            _hook_box(p, look, hook, verse)
        elif look.hook_style == "bar":
            _hook_bar(p, look, hook, verse)
        else:
            _hook_plain(p, look, hook, verse)
    if logo is not None and not logo.isNull() and look.logo_pos != "none":
        lw = max(40, int(look.logo_w))
        lh = lw * logo.height() / max(1, logo.width())
        if look.logo_pos == "top_right":
            r = QRectF(W - 48 - lw, 48, lw, lh)
        elif look.logo_pos == "top_left":
            r = QRectF(48, 48, lw, lh)
        else:
            r = QRectF((W - lw) / 2, H - 44 - lh, lw, lh)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(r, logo)


def _verse_lines(verse: str, size: int) -> tuple[QFont, list[str]]:
    f = _font(size, True)
    return f, _wrap(verse, QFontMetricsF(f), 960, 1)


def _hook_plain(p, look, hook, verse) -> None:
    f = _font(look.hook_size)
    fm = QFontMetricsF(f)
    lines = _wrap(hook, fm, 980, 2)
    lead = fm.height() * 1.12
    vf, vlines = _verse_lines(verse, 40)
    vh = QFontMetricsF(vf).height() * 1.2 if vlines else 0
    block = len(lines) * lead + (18 + vh if vlines else 0)
    if look.layout == "bands" and look.hook_pos == "top":
        top = (R.BANDS_TOP - block) / 2
    elif look.hook_pos == "bottom":
        top = H - 300 - block
    else:
        top = 120
    shadow = look.layout != "bands"
    y = top
    for line in lines:
        x = (W - fm.horizontalAdvance(line)) / 2
        _text_path(p, x, y + fm.ascent(), line, f, QColor(look.hook_fg),
                   QColor(0, 0, 0, 200) if shadow else None, 10 if shadow else 0)
        y += lead
    if vlines:
        y += 18
        vfm = QFontMetricsF(vf)
        x = (W - vfm.horizontalAdvance(vlines[0])) / 2
        _text_path(p, x, y + vfm.ascent(), vlines[0], vf, QColor(VERSE_COLOR),
                   QColor(0, 0, 0, 200) if shadow else None, 7 if shadow else 0)


def _hook_box(p, look, hook, verse) -> None:
    f = _font(look.hook_size)
    fm = QFontMetricsF(f)
    lines = _wrap(hook, fm, 900, 3)
    lead = fm.height() * 1.1
    pad_x, pad_y = 40, 26
    width = max([fm.horizontalAdvance(t) for t in lines] or [0]) + pad_x * 2
    height = len(lines) * lead + pad_y * 2
    if look.layout == "bands":
        top = max(16.0, (R.BANDS_TOP - height) / 2)
    elif look.hook_pos == "bottom":
        top = H - 330 - height
    else:
        top = 150
    if lines:
        box = QRectF((W - width) / 2, top, width, height)
        p.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(box, 30, 30)
        p.fillPath(path, QColor(look.hook_bg))
        y = top + pad_y
        for line in lines:
            x = (W - fm.horizontalAdvance(line)) / 2
            _text_path(p, x, y + fm.ascent() + (lead - fm.height()) / 2, line, f,
                       QColor(look.hook_fg))
            y += lead
        top = box.bottom()
    vf, vlines = _verse_lines(verse, 40)
    if vlines:
        vfm = QFontMetricsF(vf)
        x = (W - vfm.horizontalAdvance(vlines[0])) / 2
        _text_path(p, x, top + 22 + vfm.ascent(), vlines[0], vf, QColor(VERSE_COLOR),
                   QColor(0, 0, 0, 210), 7)


def _hook_bar(p, look, hook, verse) -> None:
    f = _font(look.hook_size)
    fm = QFontMetricsF(f)
    lines = _wrap(hook, fm, 860, 3)
    lead = fm.height() * 1.08
    vf, vlines = _verse_lines(verse, 38)
    vh = QFontMetricsF(vf).height() * 1.3 if vlines else 0
    text_h = len(lines) * lead + vh
    band_h = text_h + 130
    top = 0 if look.hook_pos == "top" else H - band_h
    if look.layout == "bands":
        band_h, top = (R.BANDS_TOP, 0) if look.hook_pos == "top" else \
            (H - R.BANDS_TOP - R.BANDS_VIDEO_H, R.BANDS_TOP + R.BANDS_VIDEO_H)
    p.fillRect(QRectF(0, top, W, band_h), QColor(look.bg))
    y = top + (band_h - text_h) / 2
    if lines:
        p.fillRect(QRectF(64, y + 6, 14, len(lines) * lead - 12), QColor(look.hook_bg))
    for line in lines:
        _text_path(p, 112, y + fm.ascent() + (lead - fm.height()) / 2, line, f,
                   QColor(look.hook_fg))
        y += lead
    if vlines:
        vfm = QFontMetricsF(vf)
        _text_path(p, 112, y + 10 + vfm.ascent(), vlines[0], vf, QColor(VERSE_COLOR))


def paint_caption(p: QPainter, look: R.Look, text: str) -> None:
    """짧게 끊은 자막 한 덩어리."""
    if not text:
        return
    f = _font(look.sub_size)
    fm = QFontMetricsF(f)
    lines = _wrap(text, fm, 940, 2)
    lead = fm.height() * 1.15
    block = len(lines) * lead
    cy = look.sub_y * H
    top = cy - block / 2
    p.setRenderHint(QPainter.Antialiasing, True)
    if look.sub_box:
        width = max(fm.horizontalAdvance(t) for t in lines) + 48
        box = QRectF((W - width) / 2, top - 14, width, block + 28)
        path = QPainterPath()
        path.addRoundedRect(box, 14, 14)
        p.fillPath(path, QColor(0, 0, 0, 165))
    y = top
    for line in lines:
        x = (W - fm.horizontalAdvance(line)) / 2
        _text_path(p, x, y + fm.ascent() + (lead - fm.height()) / 2, line, f,
                   QColor(look.sub_color),
                   None if look.sub_box else QColor(0, 0, 0, 230), 0 if look.sub_box else 9)
        y += lead


def render_png(path: Path, painter_fn) -> Path:
    img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    painter_fn(p)
    p.end()
    img.save(str(path), "PNG")
    return path


def render_look(out_dir: Path, look: R.Look, hook: str, verse: str,
                chunks: list[tuple[float, float, str]]) -> dict:
    """만들 때 얹을 그림들 (shorts.build 의 look). GUI 스레드에서 부른다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.png"):
        old.unlink(missing_ok=True)
    logo = QImage(look.logo) if look.logo and Path(look.logo).is_file() else None
    overlay = render_png(out_dir / "overlay.png",
                         lambda p: paint_overlay(p, look, hook, verse, logo))
    caps = []
    for k, (a, b, text) in enumerate(chunks):
        png = render_png(out_dir / f"cap{k:04d}.png", lambda p, t=text: paint_caption(p, look, t))
        caps.append((a, b, png))
    return {"area": look.area(), "cx": look.cx, "cy": look.cy, "zoom": look.zoom,
            "overlay": overlay, "captions": caps}


# ====================================================================== 미리 보기 화면
class ReelMonitor(QWidget):
    """9:16 미리 보기. 영상 자리를 끌면 목사님 자리가, 휠을 돌리면 크기가 바뀐다."""

    crop_moved = Signal(float, float)     # (cx, cy)
    crop_zoomed = Signal(float)           # zoom

    def __init__(self, panel: "ReelsPanel") -> None:
        super().__init__(panel)
        self.panel = panel
        self.setMinimumSize(200, 356)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self._drag: QPointF | None = None
        self._start = (0.0, 0.0)
        self.setToolTip("영상 화면을 끌면 목사님 자리가, 마우스 휠을 돌리면 크기가 바뀝니다.")

    def frame_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        fh = min(h, w * 16 / 9)
        fw = fh * 9 / 16
        return QRectF((w - fw) / 2, (h - fh) / 2, fw, fh)

    def paintEvent(self, _e):           # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b0b0b"))
        r = self.frame_rect()
        p.save()
        p.translate(r.topLeft())
        k = r.width() / W
        p.scale(k, k)
        p.setClipRect(QRectF(0, 0, W, H))
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)
        self.panel.paint_canvas(p)
        p.restore()
        p.end()

    def _area_hit(self, pos: QPointF) -> bool:
        r = self.frame_rect()
        y0, ah = self.panel.look.area()
        k = r.width() / W
        return QRectF(r.left(), r.top() + y0 * k, r.width(), ah * k).contains(pos)

    def mousePressEvent(self, e):       # noqa: N802
        if e.button() == Qt.LeftButton and self._area_hit(e.position()):
            self._drag = e.position()
            self._start = self.panel.active_pos()[:2]
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):        # noqa: N802
        if self._drag is None:
            self.setCursor(Qt.OpenHandCursor if self._area_hit(e.position()) else Qt.ArrowCursor)
            return
        sw, sh = self.panel.src_size
        if sw <= 0:
            return
        r = self.frame_rect()
        lk = self.panel.look
        y0, ah = lk.area()
        cw, ch, _x, _y = lk.crop(sw, sh)
        reg = lk.region(sw, sh) or (0, 0, sw, sh)
        # 화면에서 끈 만큼 원본에서 반대로 옮긴다 (그림을 잡아 끄는 느낌)
        k = r.width() / W
        dx = (e.position().x() - self._drag.x()) / (W * k) * cw / max(1, reg[2])
        dy = (e.position().y() - self._drag.y()) / (ah * k) * ch / max(1, reg[3])
        self.crop_moved.emit(min(1.0, max(0.0, self._start[0] - dx)),
                             min(1.0, max(0.0, self._start[1] - dy)))

    def mouseReleaseEvent(self, _e):    # noqa: N802
        if self._drag is not None:
            self._drag = None
            self.setCursor(Qt.OpenHandCursor)
            self.panel.changed.emit()

    def wheelEvent(self, e):            # noqa: N802
        if not self._area_hit(e.position()):
            return
        d = e.angleDelta().y()
        z = self.panel.active_pos()[2] * (0.95 if d > 0 else 1.05)
        self.crop_zoomed.emit(min(1.0, max(0.3, z)))
        e.accept()


class ScrubBar(QWidget):
    """요약 영상의 시간 막대 — 조각 경계와 자막 덩어리를 보여 준다."""

    seek = Signal(float)

    def __init__(self, panel: "ReelsPanel") -> None:
        super().__init__(panel)
        self.panel = panel
        self.setFixedHeight(46)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _e):           # noqa: N802
        t = theme_mod.current()
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(t.field_bg))
        total = max(0.1, self.panel.total)
        acc = 0.0
        for i, (a, b) in enumerate(self.panel.runs):
            x0, x1 = acc / total * w, (acc + b - a) / total * w
            p.fillRect(QRectF(x0, 4, max(1.0, x1 - x0 - 1), 18),
                       QColor("#3b6db3" if i % 2 == 0 else "#2f5a96"))
            acc += b - a
        p.setPen(QColor(t.text_dim))
        for a, b, _txt in self.panel.chunks:
            x = a / total * w
            p.drawLine(QPointF(x, 26), QPointF(x, 38))
        f = p.font()
        f.setPointSize(8)
        p.setFont(f)
        p.drawText(QRectF(4, 4, w, 18), Qt.AlignVCenter | Qt.AlignLeft,
                   f"조각 {len(self.panel.runs)}개 · {_fmt(total)}")
        x = self.panel.playhead / total * w
        p.setPen(QPen(QColor("#ff4d4d"), 2))
        p.drawLine(QPointF(x, 0), QPointF(x, h))
        p.end()

    def _emit(self, x: float) -> None:
        self.seek.emit(max(0.0, min(1.0, x / max(1, self.width()))) * self.panel.total)

    def mousePressEvent(self, e):       # noqa: N802
        self._emit(e.position().x())

    def mouseMoveEvent(self, e):        # noqa: N802
        if e.buttons() & Qt.LeftButton:
            self._emit(e.position().x())


# ====================================================================== 편집기
class ReelsPanel(QWidget):
    """[숏폼 편집] 칸 전체."""

    make_clicked = Signal()
    hook_edited = Signal(str)
    changed = Signal()                       # 모양이 바뀌었다 (기억해 둘 때)
    go_summary = Signal()                    # [요약] 으로 가서 문장을 고르게
    transcribe_requested = Signal()          # 자막이 없다 — 받아쓰기를 해 달라
    _frame_ready = Signal(object, object)    # (시각 열쇠, QImage)
    _hooks_ready = Signal(object, str)       # (제목들, 문제)

    def __init__(self, cfg_getter, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg_getter
        t = theme_mod.current()
        self._dim = f"color:{t.text_dim}; font-size:{t.font_small_pt}pt;"
        self.look = R.Look()
        self.media_path: Path | None = None
        self.src_size = (0, 0)
        self.runs: list[tuple[float, float]] = []
        self.chunks: list[tuple[float, float, str]] = []
        self.cues: list[tuple[float, float, str]] = []
        self.total = 1.0
        self.playhead = 0.0
        self.title = ""
        self.verse = ""
        self.sample = False
        self.kind = "picks"
        self.frame: QImage | None = None
        self._frames: dict[int, QImage] = {}
        self._asked: set[int] = set()
        self._logo: QImage | None = None
        self._logo_path = ""
        self._player: QMediaPlayer | None = None
        self._audio: QAudioOutput | None = None
        self._sink: QVideoSink | None = None
        self._playing = False
        self._clock = QElapsedTimer()
        self._anchor = 0.0
        self._run_i = 0
        self._busy_ai = False
        self._build()
        self._frame_ready.connect(self._on_frame)
        self._hooks_ready.connect(self._on_hooks)
        self._tick = QTimer(self)
        self._tick.setInterval(33)
        self._tick.timeout.connect(self._on_tick)
        self._load_look()

    # ------------------------------------------------------------------ 짜임
    def _build(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        outer.addWidget(split)

        # ---- 왼쪽: 모양 틀
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        head = QLabel("모양 틀 — 고른 뒤 오른쪽에서 무엇이든 바꿀 수 있습니다")
        head.setWordWrap(True)
        head.setStyleSheet("font-weight:600;")
        lv.addWidget(head)
        self.preset_list = QListWidget()
        self.preset_list.setViewMode(QListWidget.IconMode)
        self.preset_list.setIconSize(QSize(99, 176))
        self.preset_list.setGridSize(QSize(118, 230))
        self.preset_list.setResizeMode(QListWidget.Adjust)
        self.preset_list.setMovement(QListWidget.Static)
        self.preset_list.setWordWrap(True)
        for key, (label, _v) in R.PRESETS.items():
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, key)
            it.setToolTip(label)
            self.preset_list.addItem(it)
        self.preset_list.itemClicked.connect(self._pick_preset)
        lv.addWidget(self.preset_list, 1)
        left.setMinimumWidth(250)
        split.addWidget(left)

        # ---- 가운데: 미리 보기
        mid = QWidget()
        mv = QVBoxLayout(mid)
        mv.setContentsMargins(0, 0, 0, 0)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color:{theme_mod.current().warn};")
        mv.addWidget(self.note)
        self.btn_go_summary = QPushButton("← [요약] 에서 문장 고르러 가기")
        self.btn_go_summary.clicked.connect(self.go_summary)
        mv.addWidget(self.btn_go_summary)
        self.monitor = ReelMonitor(self)
        self.monitor.crop_moved.connect(self._on_crop_moved)
        self.monitor.crop_zoomed.connect(self._on_crop_zoomed)
        mv.addWidget(self.monitor, 1)
        self.scrub = ScrubBar(self)
        self.scrub.seek.connect(self.seek)
        mv.addWidget(self.scrub)
        tr = QHBoxLayout()
        self.btn_home = QPushButton("⏮")
        self.btn_back = QPushButton("◀ 5초")
        self.btn_play = QPushButton("▶ 재생")
        self.btn_play.setProperty("primary", True)
        self.btn_play.setToolTip("요약 영상을 소리와 함께 미리 봅니다 (Space)")
        self.btn_fwd = QPushButton("5초 ▶")
        self.time_label = QLabel("00:00.0 / 00:00.0")
        self.time_label.setStyleSheet("font-family:Consolas,'D2Coding',monospace;")
        self.btn_home.clicked.connect(lambda: self.seek(0.0))
        self.btn_back.clicked.connect(lambda: self.step(-5))
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_fwd.clicked.connect(lambda: self.step(5))
        tr.addWidget(self.btn_home)
        tr.addWidget(self.btn_back)
        tr.addWidget(self.btn_play, 1)
        tr.addWidget(self.btn_fwd)
        tr.addWidget(self.time_label)
        mv.addLayout(tr)
        mid.setMinimumWidth(300)
        split.addWidget(mid)

        # ---- 오른쪽: 설정
        body = QWidget()
        sv = QVBoxLayout(body)
        sv.setContentsMargins(0, 0, 6, 0)
        sv.setSpacing(6)

        g1 = QGroupBox("끌리는 제목")
        v1 = QVBoxLayout(g1)
        self.hook_edit = QPlainTextEdit()
        self.hook_edit.setPlaceholderText("예: 우리는 무엇을 믿고 도전하나\n(줄을 바꾸면 그대로 줄이 바뀝니다)")
        self.hook_edit.setFixedHeight(64)
        self.hook_edit.setToolTip("화면에 크게 얹는 제목입니다. 설교 제목보다 쉬운 말, 질문 형태가\n"
                                  "눌러 보게 만듭니다. 파일 이름에는 설교 제목이 그대로 쓰입니다.")
        self.hook_edit.textChanged.connect(self._on_hook_text)
        v1.addWidget(self.hook_edit)
        hrow = QHBoxLayout()
        self.btn_ai = QPushButton("AI 제목 추천")
        self.btn_ai.setToolTip("이 교회 PC 의 AI 가 요약에 들어간 말씀을 읽고 질문형·쉬운 말 제목을\n"
                               "몇 개 지어 줍니다. 모델을 올리느라 1~2분 걸릴 수 있습니다.")
        self.btn_ai.clicked.connect(self._ask_ai)
        self.btn_use_title = QPushButton("설교 제목 쓰기")
        self.btn_use_title.clicked.connect(lambda: self.hook_edit.setPlainText(self.title))
        hrow.addWidget(self.btn_ai, 1)
        hrow.addWidget(self.btn_use_title)
        v1.addLayout(hrow)
        self.hook_list = QListWidget()
        self.hook_list.setMaximumHeight(110)
        self.hook_list.setVisible(False)
        self.hook_list.setToolTip("누르면 그 제목을 씁니다")
        self.hook_list.itemClicked.connect(lambda it: self.hook_edit.setPlainText(it.text()))
        v1.addWidget(self.hook_list)
        self.ai_note = QLabel("")
        self.ai_note.setWordWrap(True)
        self.ai_note.setStyleSheet(self._dim)
        v1.addWidget(self.ai_note)
        sv.addWidget(g1)

        g2 = QGroupBox("제목 모양")
        f2 = QFormLayout(g2)
        f2.setVerticalSpacing(4)
        self.style_box = QComboBox()
        for k, label in (("plain", "글자만"), ("box", "둥근 상자"), ("bar", "띠 + 왼쪽 세로줄")):
            self.style_box.addItem(label, k)
        self.pos_box = QComboBox()
        self.pos_box.addItem("위", "top")
        self.pos_box.addItem("아래", "bottom")
        row = QHBoxLayout()
        row.addWidget(self.style_box, 1)
        row.addWidget(self.pos_box)
        f2.addRow("모양", row)
        self.hook_bg_box = self._color_box()
        self.hook_fg_box = self._color_box()
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("상자·줄"))
        row2.addWidget(self.hook_bg_box, 1)
        row2.addWidget(QLabel("글자"))
        row2.addWidget(self.hook_fg_box, 1)
        f2.addRow("색", row2)
        self.hook_size = QSpinBox()
        self.hook_size.setRange(40, 140)
        self.hook_size.setSuffix(" px")
        self.verse_check = QCheckBox("본문(성경 구절)도 작게")
        row3 = QHBoxLayout()
        row3.addWidget(self.hook_size)
        row3.addWidget(self.verse_check, 1)
        f2.addRow("크기", row3)
        sv.addWidget(g2)

        g3 = QGroupBox("자막")
        f3 = QFormLayout(g3)
        f3.setVerticalSpacing(4)
        self.sub_size = QSpinBox()
        self.sub_size.setRange(30, 100)
        self.sub_size.setSuffix(" px")
        self.sub_chars = QSpinBox()
        self.sub_chars.setRange(6, 30)
        self.sub_chars.setSuffix(" 글자씩")
        self.sub_chars.setToolTip("자막을 이 길이 안쪽으로 짧게 끊어 차례로 보여 줍니다.")
        row4 = QHBoxLayout()
        row4.addWidget(self.sub_size)
        row4.addWidget(self.sub_chars)
        f3.addRow("크기·길이", row4)
        self.sub_y = QSlider(Qt.Horizontal)
        self.sub_y.setRange(8, 95)
        self.sub_y.setToolTip("자막 높이 — 왼쪽이 화면 위, 오른쪽이 아래")
        f3.addRow("높이 (위↔아래)", self.sub_y)
        self.sub_box = QCheckBox("뒤에 반투명 상자")
        self.sub_color = self._color_box()
        row5 = QHBoxLayout()
        row5.addWidget(self.sub_box, 1)
        row5.addWidget(self.sub_color)
        f3.addRow("", row5)
        self.sub_note = QLabel("")
        self.sub_note.setWordWrap(True)
        self.sub_note.setStyleSheet(self._dim)
        f3.addRow(self.sub_note)
        self.btn_stt = QPushButton("받아쓰기로 자막 만들기")
        self.btn_stt.setToolTip("이 영상의 말을 받아써서 자막을 만듭니다. 몇 분짜리면 몇 분 걸립니다.\n"
                                "예전 요약 영상처럼 자막이 화면에 박혀 있던 영상도 새 자막을 얻습니다.")
        self.btn_stt.clicked.connect(self.transcribe_requested)
        f3.addRow(self.btn_stt)
        sv.addWidget(g3)

        g4 = QGroupBox("화면")
        f4 = QFormLayout(g4)
        f4.setVerticalSpacing(4)
        self.layout_box = QComboBox()
        self.layout_box.addItem("위아래 띠 사이에 영상", "bands")
        self.layout_box.addItem("영상이 화면을 꽉 채움", "full")
        f4.addRow("배치", self.layout_box)
        self.bg_box = self._color_box()
        self.bg_box.setToolTip("띠와 제목 띠의 바탕색")
        f4.addRow("띠 색", self.bg_box)
        self.source_box = QComboBox()
        self.source_box.addItem("가로 설교 영상 — 목사님만 크게 잘라", "crop")
        self.source_box.addItem("세로 영상 — 화면 그대로", "whole")
        self.source_box.addItem("예전 요약 영상 — 가운데 영상만 (위아래 띠·글자 빼고)", "middle")
        self.source_box.setToolTip(
            "연 영상의 어디를 쓸지 고릅니다.\n"
            "예전에 만든 세로 요약 영상을 열었다면 '가운데 영상만' 을 고르면, 예전 제목·자막이\n"
            "들어 있던 위아래 띠를 빼고 새 모양을 입힙니다. 열 때 저절로 골라 둡니다.")
        self.source_box.currentIndexChanged.connect(self._on_source)
        f4.addRow("원본에서", self.source_box)
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(30, 100)
        self.zoom_slider.setInvertedAppearance(True)
        self.zoom_slider.setToolTip("오른쪽으로 갈수록 목사님이 크게 — 미리 보기에서 휠로도 됩니다")
        f4.addRow("목사님 크기", self.zoom_slider)
        hint = QLabel("미리 보기의 영상을 끌면 목사님 자리가 옮겨집니다.")
        hint.setStyleSheet(self._dim)
        f4.addRow(hint)
        sv.addWidget(g4)

        g5 = QGroupBox("로고")
        f5 = QFormLayout(g5)
        f5.setVerticalSpacing(4)
        lrow = QHBoxLayout()
        self.logo_label = QLabel("없음")
        self.logo_label.setStyleSheet(self._dim)
        self.btn_logo = QPushButton("고르기…")
        self.btn_logo.setToolTip("교회 로고 그림(png — 바탕이 투명하면 좋습니다)")
        self.btn_logo.clicked.connect(self._pick_logo)
        lrow.addWidget(self.logo_label, 1)
        lrow.addWidget(self.btn_logo)
        f5.addRow(lrow)
        self.logo_pos = QComboBox()
        for k, label in (("bottom", "아래 가운데"), ("top_right", "위 오른쪽"),
                         ("top_left", "위 왼쪽"), ("none", "넣지 않음")):
            self.logo_pos.addItem(label, k)
        self.logo_w = QSpinBox()
        self.logo_w.setRange(60, 600)
        self.logo_w.setSuffix(" px")
        row6 = QHBoxLayout()
        row6.addWidget(self.logo_pos, 1)
        row6.addWidget(self.logo_w)
        f5.addRow("자리·크기", row6)
        sv.addWidget(g5)

        sv.addStretch(1)
        self.btn_make = QPushButton("이 모양으로 숏폼 만들기")
        self.btn_make.setProperty("primary", True)
        self.btn_make.setMinimumHeight(40)
        self.btn_make.setToolTip("[요약] 에서 고른 문장으로 세로 영상(1080x1920)을 만듭니다.\n"
                                 "미리 보기와 같은 띠·제목·자막·로고가 들어갑니다.")
        self.btn_make.clicked.connect(self.make_clicked)
        side = QScrollArea()
        side.setWidgetResizable(True)
        side.setFrameShape(QFrame.NoFrame)
        side.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side.setWidget(body)
        # 만들기 단추는 스크롤 밖에 둔다 — 낮은 화면에서도 늘 보이게
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(side, 1)
        self.make_note = QLabel("")
        self.make_note.setWordWrap(True)
        self.make_note.setStyleSheet(f"color:{theme_mod.current().warn};")
        self.make_note.setVisible(False)
        rv.addWidget(self.make_note)
        rv.addWidget(self.btn_make)
        right.setMinimumWidth(340)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 4)
        split.setStretchFactor(2, 3)
        split.setSizes([260, 520, 380])

        # 설정 칸 → look
        for w in (self.style_box, self.pos_box, self.layout_box, self.logo_pos):
            w.currentIndexChanged.connect(self._from_widgets)
        for w in (self.hook_size, self.sub_size, self.sub_chars, self.logo_w):
            w.valueChanged.connect(self._from_widgets)
        for w in (self.sub_y, self.zoom_slider):
            w.valueChanged.connect(self._from_widgets)
        for w in (self.verse_check, self.sub_box):
            w.toggled.connect(self._from_widgets)
        for w in (self.hook_bg_box, self.hook_fg_box, self.sub_color, self.bg_box):
            w.activated.connect(lambda _i, box=w: self._color_picked(box))

    def _color_box(self) -> QComboBox:
        box = QComboBox()
        for label, hexv in COLORS:
            pix = QPixmap(14, 14)
            pix.fill(QColor(hexv) if hexv else QColor("#888888"))
            box.addItem(QIcon(pix), label, hexv)
        return box

    def _set_color(self, box: QComboBox, hexv: str) -> None:
        i = box.findData(hexv.upper()) if hexv else -1
        if i < 0:
            i = box.findData(hexv)
        if i < 0:
            pix = QPixmap(14, 14)
            pix.fill(QColor(hexv))
            box.insertItem(box.count() - 1, QIcon(pix), hexv, hexv)
            i = box.count() - 2
        box.setCurrentIndex(i)

    def _color_picked(self, box: QComboBox) -> None:
        if not box.currentData():
            col = QColorDialog.getColor(QColor("#FFFFFF"), self, "색 고르기")
            if not col.isValid():
                self._to_widgets()
                return
            self._set_color(box, col.name().upper())
        self._from_widgets()

    # ------------------------------------------------------------------ look ↔ 칸
    def _to_widgets(self) -> None:
        lk = self.look
        widgets = (self.style_box, self.pos_box, self.layout_box, self.logo_pos, self.hook_size,
                   self.sub_size, self.sub_chars, self.logo_w, self.sub_y, self.zoom_slider,
                   self.source_box,
                   self.verse_check, self.sub_box, self.hook_bg_box, self.hook_fg_box,
                   self.sub_color, self.bg_box)
        for w in widgets:
            w.blockSignals(True)
        self.style_box.setCurrentIndex(max(0, self.style_box.findData(lk.hook_style)))
        self.pos_box.setCurrentIndex(max(0, self.pos_box.findData(lk.hook_pos)))
        self.layout_box.setCurrentIndex(max(0, self.layout_box.findData(lk.layout)))
        self.logo_pos.setCurrentIndex(max(0, self.logo_pos.findData(lk.logo_pos)))
        self.hook_size.setValue(int(lk.hook_size))
        self.sub_size.setValue(int(lk.sub_size))
        self.sub_chars.setValue(int(lk.sub_chars))
        self.logo_w.setValue(int(lk.logo_w))
        self.sub_y.setValue(int(round(lk.sub_y * 100)))
        self.zoom_slider.setValue(int(round(self.active_pos()[2] * 100)))
        self.source_box.setCurrentIndex(max(0, self.source_box.findData(lk.source)))
        self.verse_check.setChecked(bool(lk.show_verse))
        self.sub_box.setChecked(bool(lk.sub_box))
        self._set_color(self.hook_bg_box, lk.hook_bg)
        self._set_color(self.hook_fg_box, lk.hook_fg)
        self._set_color(self.sub_color, lk.sub_color)
        self._set_color(self.bg_box, lk.bg)
        for w in widgets:
            w.blockSignals(False)
        self.logo_label.setText(Path(lk.logo).name if lk.logo else "없음 — 로고 그림을 고르세요")
        for i in range(self.preset_list.count()):
            it = self.preset_list.item(i)
            it.setSelected(it.data(Qt.UserRole) == lk.preset)

    def _from_widgets(self, *_a) -> None:
        lk = self.look
        old_chars = lk.sub_chars
        lk.hook_style = self.style_box.currentData()
        lk.hook_pos = self.pos_box.currentData()
        lk.layout = self.layout_box.currentData()
        lk.logo_pos = self.logo_pos.currentData()
        lk.hook_size = self.hook_size.value()
        lk.sub_size = self.sub_size.value()
        lk.sub_chars = self.sub_chars.value()
        lk.logo_w = self.logo_w.value()
        lk.sub_y = self.sub_y.value() / 100
        if lk.source == "crop":
            lk.zoom = self.zoom_slider.value() / 100
        else:
            lk.vzoom = self.zoom_slider.value() / 100
        lk.show_verse = self.verse_check.isChecked()
        lk.sub_box = self.sub_box.isChecked()
        lk.hook_bg = self.hook_bg_box.currentData() or lk.hook_bg
        lk.hook_fg = self.hook_fg_box.currentData() or lk.hook_fg
        lk.sub_color = self.sub_color.currentData() or lk.sub_color
        lk.bg = self.bg_box.currentData() or lk.bg
        if lk.sub_chars != old_chars:
            self.chunks = R.chunk_captions(self.cues, lk.sub_chars)
        self._save_look()
        self.monitor.update()
        self.scrub.update()
        self.changed.emit()

    def _on_source(self, _i: int) -> None:
        self.look.source = self.source_box.currentData() or "crop"
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(int(round(self.active_pos()[2] * 100)))
        self.zoom_slider.blockSignals(False)
        self._save_look()
        self.monitor.update()
        self.changed.emit()

    def _pick_preset(self, item: QListWidgetItem) -> None:
        self.look = R.apply_preset(self.look, item.data(Qt.UserRole))
        self.chunks = R.chunk_captions(self.cues, self.look.sub_chars)
        self._to_widgets()
        self._save_look()
        self.monitor.update()
        self.scrub.update()
        self.changed.emit()

    def active_pos(self) -> tuple[float, float, float]:
        """지금 쓰는 (가로 자리, 세로 자리, 크기) — 원본 종류에 따라 다른 값."""
        lk = self.look
        return (lk.cx, lk.cy, lk.zoom) if lk.source == "crop" else (lk.vcx, lk.vcy, lk.vzoom)

    def _on_crop_moved(self, cx: float, cy: float) -> None:
        if self.look.source == "crop":
            self.look.cx, self.look.cy = cx, cy
        else:
            self.look.vcx, self.look.vcy = cx, cy
        self.monitor.update()

    def _on_crop_zoomed(self, z: float) -> None:
        if self.look.source == "crop":
            self.look.zoom = z
        else:
            self.look.vzoom = z
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(int(round(z * 100)))
        self.zoom_slider.blockSignals(False)
        self._save_look()
        self.monitor.update()

    def _load_look(self) -> None:
        st = QSettings(ORG, APP)
        try:
            data = json.loads(str(st.value("reel_look", "") or "{}"))
        except ValueError:
            data = {}
        if data:
            self.look = R.Look.from_dict(data)
        else:
            cfg = self._cfg()
            self.look = R.apply_preset(R.Look(cx=cfg.shorts_center_x, cy=cfg.shorts_center_y,
                                              zoom=cfg.shorts_zoom), "navy")
        self._to_widgets()

    def _save_look(self) -> None:
        QSettings(ORG, APP).setValue("reel_look", json.dumps(self.look.to_dict(),
                                                            ensure_ascii=False))

    def _pick_logo(self) -> None:
        start = str(Path(self.look.logo).parent) if self.look.logo else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "교회 로고 그림", start, "그림 (*.png *.jpg *.jpeg *.webp *.bmp);;모든 파일 (*.*)")
        if path:
            self.look.logo = path
            if self.look.logo_pos == "none":
                self.look.logo_pos = "bottom"
            self._to_widgets()
            self._save_look()
            self.monitor.update()
            self.changed.emit()

    def logo_image(self) -> QImage | None:
        if self.look.logo != self._logo_path:
            self._logo_path = self.look.logo
            img = QImage(self.look.logo) if self.look.logo else QImage()
            self._logo = None if img.isNull() else img
        return self._logo

    # ------------------------------------------------------------------ 밖에서
    def set_session(self, media_path: Path | None, runs: list[tuple[float, float]],
                    cues: list[tuple[float, float, str]], title: str, verse: str, hook: str,
                    kind: str = "picks") -> None:
        """다루는 영상이 바뀌었다 — 소리·화면 파일, 이어붙일 조각, 자막, 제목.

        kind: picks = [요약] 에서 고른 문장 | range = 고른 문장이 없어 설교 구간 전체 |
              whole = 구간도 없어 파일 전체 (예: 이미 만든 세로 요약 영상)
        """
        self.stop()
        new_media = Path(media_path) if media_path else None
        if new_media != self.media_path:
            self._frames.clear()
            self._asked.clear()
            self.frame = None
            self._icons_have_frame = False
            self.src_size = (0, 0)
            self.media_path = new_media
            if new_media and new_media.exists():
                try:
                    from core import cut as cut_mod
                    vp = cut_mod.video_params(new_media)
                    self.src_size = (int(vp["width"]), int(vp["height"]))
                except Exception:
                    self.src_size = (0, 0)
            self._auto_source()
        self.runs = [(float(a), float(b)) for a, b in runs if b > a]
        self.cues = list(cues)
        self.chunks = R.chunk_captions(self.cues, self.look.sub_chars)
        self.total = max(0.1, sum(b - a for a, b in self.runs))
        self.title, self.verse = title, verse
        self.kind = kind
        self.sample = False
        self.hook_edit.blockSignals(True)
        self.hook_edit.setPlainText(hook or title)
        self.hook_edit.blockSignals(False)
        sw, sh = self.src_size
        mins = f"{int(self.total // 60)}분 {int(self.total % 60):02d}초"
        if not self.media_path:
            why = "영상을 먼저 여세요 — [설교 찾기] 의 [영상 가져오기] 나 [내 컴퓨터에서 열기]."
        elif sw <= 0:
            why = ("이 파일에는 화면이 없습니다. 숏폼은 화면이 있는 영상에서 만듭니다 "
                   "(소리만 있는 설교는 [음성 설교] 를 쓰세요).")
        else:
            why = ""
        if why:
            note = why
        elif kind == "range":
            note = (f"[요약] 에서 고른 문장이 없어 설교 구간 전체({mins})로 만듭니다. "
                    "짧게 만들려면 [요약] 에서 문장을 고르세요.")
        elif kind == "whole":
            note = f"연 영상 전체({mins})로 만듭니다."
        else:
            note = ""
        if not why and self.total > 180:
            note += "  릴스·쇼츠는 보통 3분 안쪽입니다."
        self.note.setText(note.strip())
        self.make_note.setText(("만들 수 없는 까닭: " + why) if why else "")
        self.make_note.setVisible(bool(why))
        self.btn_go_summary.setVisible(kind != "picks" and sw > 0 and sh <= sw)
        self.btn_make.setEnabled(not why)
        self.btn_make.setToolTip(why or "세로 영상(1080x1920)을 만듭니다.\n"
                                        "미리 보기와 같은 띠·제목·자막·로고가 들어갑니다.")
        has_audio = bool(self.media_path) and not why
        self.sub_note.setText("" if self.cues else
                              "이 영상에는 자막이 없습니다. 받아쓰기로 만들거나, 자막 없이 만들 수 있습니다.")
        self.sub_note.setVisible(not self.cues)
        self.btn_stt.setVisible(not self.cues and has_audio)
        self.seek(min(2.0, self.total))
        self._refresh_preset_icons()

    def _auto_source(self) -> None:
        """연 영상에 맞춰 '원본에서 어디를 쓸지' 를 골라 둔다 (사람이 바꿀 수 있다)."""
        sw, sh = self.src_size
        if sw <= 0:
            return
        if sh > sw:
            # 우리가 만든 요약 영상(…_핵심요약.mp4 · …_3분요약.mp4)은 위아래 띠에 예전 제목·
            # 자막이 들어 있다 — 가운데 영상만 쓴다. 다른 세로 영상은 화면 그대로.
            name = self.media_path.name if self.media_path else ""
            self.look.source = "middle" if "요약" in name else "whole"
        else:
            self.look.source = "crop"
        self._to_widgets()

    def hook(self) -> str:
        return self.hook_edit.toPlainText().strip()

    def _on_hook_text(self) -> None:
        self.monitor.update()
        self.hook_edited.emit(self.hook())

    def paint_canvas(self, p: QPainter) -> None:
        """미리 보기 한 장면 (1080x1920 좌표)."""
        paint_video(p, self.look, self.frame, self.src_size)
        paint_overlay(p, self.look, self.hook(), self.verse, self.logo_image())
        paint_caption(p, self.look, R.caption_at(self.chunks, self.playhead))

    def _refresh_preset_icons(self) -> None:
        for i in range(self.preset_list.count()):
            it = self.preset_list.item(i)
            look = R.apply_preset(self.look, it.data(Qt.UserRole))
            img = QImage(W // 6, H // 6, QImage.Format_ARGB32_Premultiplied)
            img.fill(QColor("#000000"))
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.scale(1 / 6, 1 / 6)
            paint_video(p, look, self.frame, self.src_size)
            paint_overlay(p, look, self.hook() or "우리는 무엇을 믿고 도전하나", self.verse,
                          self.logo_image())
            paint_caption(p, look, "짧게 끊은 자막")
            p.end()
            it.setIcon(QIcon(QPixmap.fromImage(img)))

    # ------------------------------------------------------------------ AI 제목
    def _ask_ai(self) -> None:
        if self._busy_ai:
            return
        text = " ".join(t for _a, _b, t in self.cues)
        if not text.strip():
            self.ai_note.setText("요약에 들어간 말씀이 없습니다. [요약] 에서 문장을 먼저 고르세요.")
            return
        self._busy_ai = True
        self.btn_ai.setEnabled(False)
        self.btn_ai.setText("AI 가 짓는 중…")
        self.ai_note.setText("AI 모델을 올리고 제목을 짓습니다. 1~2분 걸릴 수 있습니다.")
        cfg, title, verse = self._cfg(), self.title, self.verse

        def work():
            try:
                hooks = R.suggest_hooks(cfg, title, verse, text, 5, say=lambda _t: None)
                self._hooks_ready.emit(hooks, "")
            except Exception as exc:          # 실패해도 손으로 적으면 된다
                self._hooks_ready.emit([], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_hooks(self, hooks, problem: str) -> None:
        self._busy_ai = False
        self.btn_ai.setEnabled(True)
        self.btn_ai.setText("AI 제목 추천")
        if problem:
            self.ai_note.setText("AI 를 쓰지 못했습니다: " + problem[:300])
            return
        self.hook_list.clear()
        for h in hooks:
            self.hook_list.addItem(h)
        self.hook_list.setVisible(True)
        self.ai_note.setText("마음에 드는 것을 누르면 위 칸에 들어갑니다. 고쳐 써도 됩니다. "
                             "다시 누르면 다른 제목을 짓습니다.")

    # ------------------------------------------------------------------ 화면 장면
    def _src_time(self, t: float) -> tuple[int, float] | None:
        acc = 0.0
        for i, (a, b) in enumerate(self.runs):
            if t < acc + (b - a) or i == len(self.runs) - 1:
                return i, min(b, a + (t - acc))
            acc += b - a
        return None

    def _want_frame(self, t: float) -> None:
        """멈춰 있을 때의 화면 — ffmpeg 로 그 장면 하나를 뽑아 온다 (뒤에서)."""
        hit = self._src_time(t)
        if not hit or not self.media_path or self.src_size[0] <= 0:
            return
        src_t = hit[1]
        key = int(round(src_t * 5))            # 0.2초 단위로 모아 둔다
        if key in self._frames:
            self.frame = self._frames[key]
            self.monitor.update()
            return
        if key in self._asked:
            return
        self._asked.add(key)
        path = self.media_path

        def work():
            img = QImage()
            try:
                from core.util import require, tool_env
                proc = subprocess.run(
                    [require("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{src_t:.2f}",
                     "-i", str(path), "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "3",
                     "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=tool_env(),
                    timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                img = QImage.fromData(proc.stdout, "JPG")
            except Exception:
                pass
            self._frame_ready.emit(key, img)

        threading.Thread(target=work, daemon=True).start()

    def _on_frame(self, key, img) -> None:
        self._asked.discard(key)
        if img is None or img.isNull():
            return
        self._frames[key] = img
        if len(self._frames) > 60:
            self._frames.pop(next(iter(self._frames)))
        hit = self._src_time(self.playhead)
        if self._playing or (hit and int(round(hit[1] * 5)) == key):
            self.frame = img
            self.monitor.update()
        if not self._icons_have_frame and self.frame is not None:
            self._icons_have_frame = True    # 틀 그림을 실제 화면으로 다시 그린다
            self._refresh_preset_icons()

    _icons_have_frame = False

    # ------------------------------------------------------------------ 재생
    def _ensure_player(self) -> bool:
        if not self.media_path or not self.media_path.exists() or not self.runs:
            return False
        if self._player is None:
            try:
                self._player = QMediaPlayer(self)
                self._audio = QAudioOutput(self)
                self._audio.setVolume(0.9)
                self._player.setAudioOutput(self._audio)
                self._sink = QVideoSink(self)
                self._player.setVideoSink(self._sink)
                self._sink.videoFrameChanged.connect(self._on_sink_frame)
            except Exception:
                self._player = None
                return False
        want = QUrl.fromLocalFile(self.media_path.resolve().as_posix())
        if self._player.source() != want:
            self._player.setSource(want)
        return True

    def _on_sink_frame(self, frame) -> None:
        if not self._playing:
            return
        try:
            img = frame.toImage()
        except Exception:
            return
        if not img.isNull():
            self.frame = img

    def toggle_play(self) -> None:
        if self._playing:
            self.stop()
        else:
            self.play()

    def play(self) -> None:
        if not self.runs:
            return
        if self.playhead >= self.total - 0.1:
            self.playhead = 0.0
        self._playing = True
        self.btn_play.setText("❚❚ 멈춤")
        self._anchor = self.playhead
        self._clock.start()
        if self._ensure_player():
            hit = self._src_time(self.playhead)
            if hit:
                self._run_i = hit[0]
                self._player.setPosition(int(hit[1] * 1000))
                self._player.play()
        self._tick.start()

    def stop(self) -> None:
        if not self._playing:
            return
        self._playing = False
        self._tick.stop()
        if self._player is not None:
            self._player.pause()
        self.btn_play.setText("▶ 재생")
        self._want_frame(self.playhead)

    def _offset(self, i: int) -> float:
        return sum(b - a for a, b in self.runs[:i])

    def _on_tick(self) -> None:
        t = self._anchor + self._clock.elapsed() / 1000.0
        player_ok = (self._player is not None and
                     self._player.playbackState() == QMediaPlayer.PlayingState)
        if player_ok:
            i = self._run_i
            a, b = self.runs[i]
            pos = self._player.position() / 1000.0
            if pos >= b - 0.02:
                if i + 1 < len(self.runs):              # 다음 조각으로 건너뛴다
                    self._run_i = i + 1
                    self._player.setPosition(int(self.runs[i + 1][0] * 1000))
                    self._anchor = self._offset(i + 1)
                    self._clock.restart()
                    t = self._anchor
                else:
                    self._player.pause()
            elif a - 0.5 <= pos < b:
                heard = self._offset(i) + (pos - a)
                if abs(heard - t) > 0.25:
                    self._anchor = heard
                    self._clock.restart()
                    t = heard
        else:
            self._want_frame(t)          # 소리 장치가 없으면 뽑은 장면으로라도
        if t >= self.total:
            t = self.total
            self.playhead = t
            self.stop()
        self.playhead = t
        self.monitor.update()
        self.scrub.update()
        self._update_time()

    def seek(self, t: float) -> None:
        self.playhead = max(0.0, min(self.total, float(t)))
        if self._playing:
            self._anchor = self.playhead
            self._clock.restart()
            hit = self._src_time(self.playhead)
            if self._player is not None and hit:
                self._run_i = hit[0]
                self._player.setPosition(int(hit[1] * 1000))
        else:
            self._want_frame(self.playhead)
        self.monitor.update()
        self.scrub.update()
        self._update_time()

    def step(self, seconds: float) -> None:
        self.seek(self.playhead + seconds)

    def _update_time(self) -> None:
        self.time_label.setText(f"{_fmt(self.playhead)} / {_fmt(self.total)}")

    def hideEvent(self, e):             # noqa: N802
        self.stop()
        super().hideEvent(e)

    # ------------------------------------------------------------------ 만들기
    def render_for_build(self, out_dir: Path) -> dict:
        """완성본에 얹을 그림들 (shorts.build 의 look)."""
        out = render_look(out_dir, copy.deepcopy(self.look), self.hook(), self.verse,
                          list(self.chunks))
        out.update(self.look.build_args(*self.src_size))
        return out
