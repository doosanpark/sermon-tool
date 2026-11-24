"""[음성 설교] 편집기 (2026-11-02).

소리만 있는 설교(음성 설교)에 사진·영상을 입혀 영상을 만드는 작업 칸이다. 처음에는
폴더 하나를 고르면 정해진 대로 차례로 까는 설정 칸이었는데, "필름크래프트 수준이 아닌
듯하다" 는 말씀에 간단한 편집기로 다시 만들었다 (FilmCraft 의 Assembly 작업 공간 짜임).

    ┌ 사진·영상 목록 ─┐┌──────── 미리 보기 (소리와 함께) ────────┐┌ 고른 장면 ─────┐
    │ 썸네일 · 넣기   ││                                          ││ 길이·움직임·   │
    │ 끌어서 ↓        │└ ⏮ ◀ ▶ ▶ ⏭   00:01:23 / 00:42:10 ────────┘│ 넘기기 · 글자· │
    └─────────────────┘                                            │ 음악 · 만들기  │
    ┌ 타임라인: 눈금 · 글자 · 장면(끌어서 순서·오른쪽 끝을 끌어 길이) · 설교 소리 파형 ┐

장면 하나는 marker.visual_items 에 사전(core/visual.new_item) 으로 남는다. 미리 보기는
완성본과 같은 칸 나누기(visual.slots_from_items)와 같은 움직임 식(visual.motion_rect)으로
그리므로 보이는 대로 만들어진다. 사진은 완성본이 쓰는 준비된 그림(prepare_photo)을,
영상은 작게 뽑아 둔 장면을 쓴다. 소리는 설교 구간을 이어서 재생한다 — 소리 시계를 따라
화면을 그린다 (FilmCraft 의 audio-clock playback).
"""
from __future__ import annotations

import concurrent.futures as cf
import copy
import json
import math
import random
import shutil
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import (QElapsedTimer, QMimeData, QObject, QPointF, QRectF, QSettings,
                            QSize, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import (QColor, QFont, QIcon, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMenu, QPushButton, QScrollArea, QScrollBar, QSizePolicy,
                               QSplitter, QToolButton, QVBoxLayout, QWidget)

import theme as theme_mod
from core import visual as V

MIME = "application/x-sermon-visual-items"
ORG, APP = "창의진교회", "SermonTool"
FPS = V.FPS
PREVIEW_W, PREVIEW_H = 768, 432          # 미리 보기용으로 줄여 둔 사진 크기 (한 장 1.3MB)
KEEP_PHOTOS = 80                         # 메모리에 들고 있을 사진 수 — 넘으면 오래된 것부터 버린다


def _fmt(t: float, tenths: bool = False) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    out = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    if tenths:
        out += f".{int((t - int(t)) * 10)}"
    return out


def _short(t: float) -> str:
    return f"{t:.1f}초" if t < 60 else f"{int(t) // 60}분 {int(t) % 60:02d}초"


# ====================================================================== 그림 창고
class Media(QObject):
    """썸네일 · 미리 보기용 사진 · 영상 장면을 뒤에서 만들어 두는 곳.

    ffmpeg 를 부르는 일은 모두 뒤 스레드에서 한다. 다 되면 ready 로 알려 다시 그리게 한다.
    """

    ready = Signal(str)

    def __init__(self, cache_dir_getter, parent=None) -> None:
        super().__init__(parent)
        self._cache_dir = cache_dir_getter
        self._pool = cf.ThreadPoolExecutor(max_workers=2)
        self._lock = threading.Lock()
        self.thumbs: dict[str, QImage] = {}
        self.photos: dict[str, QImage] = {}
        self.proxies: dict[str, list[bytes]] = {}
        self._asked: set[str] = set()
        self._decoded: tuple[str, int, QImage] | None = None

    @staticmethod
    def thumb_key(item: dict) -> str:
        return f"{item['path']}|{float(item.get('in') or 0):.1f}"

    def thumb(self, item: dict) -> QImage | None:
        key = self.thumb_key(item)
        img = self.thumbs.get(key)
        if img is None:
            self._ask("t:" + key, self._make_thumb, dict(item), key)
        return img

    def photo(self, item: dict) -> QImage | None:
        path = item["path"]
        img = self.photos.get(path)
        if img is None:
            self._ask("p:" + path, self._make_photo, dict(item))
        return img

    def proxy_frame(self, item: dict, at: float) -> QImage | None:
        """영상 장면의 at 초(원본 기준) 그림. 아직 없으면 None."""
        path = item["path"]
        frames = self.proxies.get(path)
        if frames is None:
            self._ask("v:" + path, self._make_proxy, dict(item))
            return None
        if not frames:
            return None
        idx = min(len(frames) - 1, max(0, int(at * V.PROXY_FPS)))
        if self._decoded and self._decoded[0] == path and self._decoded[1] == idx:
            return self._decoded[2]
        img = QImage.fromData(frames[idx], "JPG")
        self._decoded = (path, idx, img)
        return img

    def forget_proxy(self, path: str) -> None:
        """시작 지점·길이가 바뀌어 뽑아 둔 범위를 넘으면 다시 뽑는다."""
        self.proxies.pop(path, None)
        self._asked.discard("v:" + path)

    # -- 뒤 스레드
    def _ask(self, key: str, fn, *args) -> None:
        with self._lock:
            if key in self._asked:
                return
            self._asked.add(key)
        self._pool.submit(self._guard, fn, *args)

    def _guard(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception:            # 그림 하나 못 만들어도 편집기는 계속 돈다
            pass

    def _make_thumb(self, item: dict, key: str) -> None:
        data = V.thumb_jpeg(item, 256, 144)
        img = QImage.fromData(data, "JPG") if data else QImage()
        if img.isNull():
            img = QImage(256, 144, QImage.Format_RGB32)
            img.fill(QColor("#3a3a3a"))
        self.thumbs[key] = img
        self.ready.emit(item["path"])

    def _make_photo(self, item: dict) -> None:
        m = V.item_media(item)
        prepared = V.prepare_photo(m, Path(self._cache_dir()) / "visual_photos")
        img = QImage(str(prepared)) if prepared else QImage()
        if img.isNull():
            return
        self.photos[item["path"]] = img.scaled(PREVIEW_W, PREVIEW_H, Qt.IgnoreAspectRatio,
                                               Qt.SmoothTransformation)
        # 메모리가 빠듯한 PC 가 있다 (더 쓸 수 있는 양 0.7GB 를 본 적이 있다)
        while len(self.photos) > KEEP_PHOTOS:
            old = next(iter(self.photos))
            self.photos.pop(old, None)
            with self._lock:
                self._asked.discard("p:" + old)
        self.ready.emit(item["path"])

    def close(self) -> None:
        """프로그램을 닫을 때 — 기다리는 일은 버린다."""
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _make_proxy(self, item: dict) -> None:
        self.proxies[item["path"]] = V.video_proxy(item)
        self.ready.emit(item["path"])


# ====================================================================== 사진·영상 목록
class MediaBin(QListWidget):
    """쓸 사진·영상 목록 (FilmCraft 의 Bin). 끌어서 타임라인에 놓는다."""

    add_requested = Signal(list)        # 밖에서 끌어 넣은 파일 경로들

    def __init__(self, media: Media, parent=None) -> None:
        super().__init__(parent)
        self.media = media
        self.setViewMode(QListWidget.IconMode)
        self.setIconSize(QSize(112, 63))
        self.setGridSize(QSize(124, 96))
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setWordWrap(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setToolTip("끌어서 아래 타임라인에 놓으세요. 두 번 누르면 타임라인 끝에 붙습니다.\n"
                        "탐색기에서 사진·영상을 끌어 여기에 놓아도 됩니다.")
        media.ready.connect(self._refresh_icons)

    def set_items(self, items: list[dict]) -> None:
        self.clear()
        for it in items:
            self.add_item(it)

    def add_item(self, it: dict) -> None:
        name = Path(it["path"]).name
        label = name if len(name) <= 16 else name[:14] + "…"
        if it.get("kind") == "video":
            label += f"\n영상 {_fmt(float(it.get('len') or 0))}"
        row = QListWidgetItem(label)
        row.setData(Qt.UserRole, it)
        row.setToolTip(it["path"])
        self.addItem(row)
        self._set_icon(row)

    def all_items(self) -> list[dict]:
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]

    def chosen(self) -> list[dict]:
        rows = sorted(self.selectedItems(), key=lambda r: self.row(r))
        return [r.data(Qt.UserRole) for r in rows]

    def _set_icon(self, row: QListWidgetItem) -> None:
        img = self.media.thumb(row.data(Qt.UserRole))
        if img is not None:
            row.setIcon(QIcon(QPixmap.fromImage(img)))

    def _refresh_icons(self, path: str) -> None:
        for i in range(self.count()):
            row = self.item(i)
            if row.data(Qt.UserRole)["path"] == path and row.icon().isNull():
                self._set_icon(row)

    # -- 끌기
    def mimeData(self, rows):           # noqa: N802 (Qt 이름)
        data = QMimeData()
        items = [r.data(Qt.UserRole) for r in sorted(rows, key=lambda r: self.row(r))]
        data.setData(MIME, json.dumps(items, ensure_ascii=False).encode("utf-8"))
        return data

    def mimeTypes(self):                # noqa: N802
        return [MIME, "text/uri-list"]

    def dragEnterEvent(self, e):        # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):         # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):             # noqa: N802
        if e.mimeData().hasUrls():
            self.add_requested.emit([u.toLocalFile() for u in e.mimeData().urls()
                                     if u.isLocalFile()])
            e.acceptProposedAction()
        else:
            e.ignore()


# ====================================================================== 화면 그리기
class Renderer:
    """시각 t(완성본 기준 초)의 화면을 그린다. 완성본(core/visual.py)과 같은 식을 쓴다."""

    def __init__(self, media: Media) -> None:
        self.media = media
        self.items: list[dict] = []
        self.slots: list[V.Slot] = []
        self.starts: list[int] = []
        self.total = 60.0
        self.card: QImage | None = None
        self.corner: QImage | None = None
        self.plain: QImage | None = None

    def set_plan(self, items, slots, total) -> None:
        self.items, self.slots, self.total = items, slots, total
        self.starts = V.slot_starts(slots) if slots else []

    def active(self, t: float) -> list[int]:
        f = t * FPS
        out = []
        for i, (st, sl) in enumerate(zip(self.starts, self.slots)):
            if st <= f < st + sl.frames:
                out.append(i)
            elif st > f:
                break
        if not out and self.slots:
            out = [len(self.slots) - 1]
        return out[-2:]

    def paint(self, p: QPainter, r: QRectF, t: float) -> None:
        p.save()
        p.setClipRect(r)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.fillRect(r, QColor("#000000"))
        if not self.slots:
            if self.plain is not None:
                p.drawImage(r, self.plain)
            else:
                g = QLinearGradient(r.topLeft(), r.bottomLeft())
                g.setColorAt(0, QColor("#22355E"))
                g.setColorAt(1, QColor("#0C1631"))
                p.fillRect(r, g)
        else:
            act = self.active(t)
            f = t * FPS
            if len(act) == 2:
                a, b = act
                sb = self.slots[b]
                prog = min(max((f - self.starts[b]) / max(1, sb.xfade), 0.0), 1.0)
                self._transition(p, r, sb.transition, prog,
                                 lambda rr: self._slot(p, rr, a, f),
                                 lambda rr: self._slot(p, rr, b, f))
            else:
                self._slot(p, r, act[0], f)
            self._overlays(p, r, t)
        # 처음엔 검은 화면에서 드러나고 끝엔 검게 닫힌다
        dark = 0.0
        if t < V.BLACK_IN:
            dark = 1 - t / V.BLACK_IN
        elif t > self.total - V.BLACK_OUT:
            dark = min(1.0, (t - (self.total - V.BLACK_OUT)) / V.BLACK_OUT)
        if dark > 0:
            p.fillRect(r, QColor(0, 0, 0, int(255 * dark)))
        p.restore()

    def _slot(self, p: QPainter, r: QRectF, i: int, f: float) -> None:
        sl = self.slots[i]
        local = f - self.starts[i]
        it = self.items[sl.item] if 0 <= sl.item < len(self.items) else None
        if it is None:
            return
        if it.get("kind") == "video":
            length = float(it.get("len") or 0)
            at = float(it.get("in") or 0) + local / FPS
            if length > 0.5 and at > length - 0.05:
                at = at % length
            img = self.media.proxy_frame(it, at) or self.media.thumb(it)
            if img is not None:
                p.drawImage(r, img)
            return
        img = self.media.photo(it)
        if img is None:
            thumb = self.media.thumb(it)
            if thumb is not None:
                p.drawImage(r, thumb)
            return
        x, y, size = V.motion_rect(sl.motion, sl.frames, local)
        src = QRectF(x * img.width(), y * img.height(), size * img.width(), size * img.height())
        p.drawImage(r, img, src)

    @staticmethod
    def _transition(p: QPainter, r: QRectF, kind: str, prog: float, draw_a, draw_b) -> None:
        w, h = r.width(), r.height()
        if kind in ("fadeblack", "fadewhite"):
            color = QColor(0, 0, 0) if kind == "fadeblack" else QColor(255, 255, 255)
            if prog < 0.5:
                draw_a(r)
                color.setAlphaF(prog * 2)
            else:
                draw_b(r)
                color.setAlphaF((1 - prog) * 2)
            p.fillRect(r, color)
        elif kind in ("slideleft", "slideright"):
            d = prog * w * (-1 if kind == "slideleft" else 1)
            draw_a(r.translated(d, 0))
            draw_b(r.translated(d + (w if kind == "slideleft" else -w), 0))
        elif kind in ("wipeleft", "wiperight"):
            draw_a(r)
            p.save()
            if kind == "wipeleft":
                edge = r.left() + w * (1 - prog)
                p.setClipRect(QRectF(edge, r.top(), r.right() - edge, h), Qt.IntersectClip)
            else:
                p.setClipRect(QRectF(r.left(), r.top(), w * prog, h), Qt.IntersectClip)
            draw_b(r)
            p.restore()
        elif kind == "circleopen":
            draw_a(r)
            radius = math.hypot(w / 2, h / 2) * prog
            path = QPainterPath()
            path.addEllipse(r.center(), radius, radius)
            p.save()
            p.setClipPath(path, Qt.IntersectClip)
            draw_b(r)
            p.restore()
        else:                                  # fade (겹쳐 넘기기)
            draw_a(r)
            p.save()
            p.setOpacity(prog)
            draw_b(r)
            p.restore()

    def _overlays(self, p: QPainter, r: QRectF, t: float) -> None:
        k = r.width() / V.WIDTH
        if self.card is not None and t < V.CARD_OUT + V.CARD_FADE:
            a = min(max((t - V.CARD_IN) / V.CARD_FADE, 0.0), 1.0)
            if t > V.CARD_OUT:
                a = min(a, max(0.0, 1 - (t - V.CARD_OUT) / V.CARD_FADE))
            if a > 0:
                p.save()
                p.setOpacity(a)
                p.drawImage(r, self.card)
                p.restore()
        if self.corner is not None and t > V.CORNER_IN:
            a = min(1.0, (t - V.CORNER_IN) / V.CORNER_FADE)
            p.save()
            p.setOpacity(a)
            p.drawImage(QRectF(r.left() + V.CORNER_X * k, r.top() + V.CORNER_Y * k,
                               self.corner.width() * k, self.corner.height() * k), self.corner)
            p.restore()


class Monitor(QWidget):
    """미리 보기 화면 (FilmCraft 의 Program monitor). 16:9 로 가운데에 그린다."""

    def __init__(self, renderer: Renderer, parent=None) -> None:
        super().__init__(parent)
        self.renderer = renderer
        self.t = 0.0
        self.note = ""
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def frame_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        fw = min(w, h * 16 / 9)
        fh = fw * 9 / 16
        return QRectF((w - fw) / 2, (h - fh) / 2, fw, fh)

    def paintEvent(self, _e):           # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b0b0b"))
        r = self.frame_rect()
        self.renderer.paint(p, r, self.t)
        if self.note:
            p.setPen(QColor(255, 255, 255, 200))
            f = p.font()
            f.setPointSize(9)
            p.setFont(f)
            p.drawText(r.adjusted(8, 0, -8, -6), Qt.AlignBottom | Qt.AlignRight, self.note)
        p.end()


# ====================================================================== 타임라인
RULER_H, TEXT_H, VIDEO_H, AUDIO_H = 22, 16, 72, 44
TL_H = RULER_H + TEXT_H + VIDEO_H + AUDIO_H + 6


class Timeline(QWidget):
    """눈금 · 글자 · 장면 · 설교 소리 (FilmCraft 의 Timeline 을 한 줄짜리로 줄인 것).

    장면을 끌면 순서가, 오른쪽 끝을 끌면 길이가 바뀐다. 빈 곳이나 눈금을 누르면 그 시각으로.
    """

    seek = Signal(float)
    selected = Signal(int)
    move_item = Signal(int, int)          # (옮길 장면, 들어갈 자리)
    resize_item = Signal(int, float, bool)  # (장면, 새 길이, 끝났나)
    drop_items = Signal(list, int)        # (넣을 장면들, 들어갈 자리)
    delete_item = Signal(int)
    view_changed = Signal()

    def __init__(self, media: Media, parent=None) -> None:
        super().__init__(parent)
        self.media = media
        self.items: list[dict] = []
        self.layout_: list[tuple[int, int, int]] = []
        self.slots: list[V.Slot] = []
        self.starts: list[int] = []
        self.total = 60.0
        self.peaks: list[float] = []
        self.peaks_per_s = 20
        self.playhead = 0.0
        self.sel = -1
        self.text_mode = "card"           # card | plain | none
        self.corner = True
        self.pps = 10.0                   # 1초에 몇 픽셀
        self.offset = 0.0                 # 왼쪽 끝 시각
        self._fit = True
        self._drag: str | None = None
        self._drag_i = -1
        self._press_x = 0.0
        self._insert_at = -1
        self.setMinimumHeight(TL_H)
        self.setMaximumHeight(TL_H)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        media.ready.connect(lambda _p: self.update())

    # -- 자료
    def set_plan(self, items, slots, total) -> None:
        self.items = items
        self.layout_ = V.item_layout(items)
        self.slots = slots
        self.starts = V.slot_starts(slots) if slots else []
        self.total = max(1.0, total)
        if self._fit:
            self.fit()
        self.update()

    def item_span(self, k: int) -> tuple[float, float, float]:
        """장면 k 의 (시작, 끝, 앞과 겹침) 초. 설교 끝에서 잘렸거나 늘어났으면 그만큼."""
        st, n, xf = self.layout_[k]
        for sl, s0 in zip(self.slots, self.starts):
            if sl.item == k and not sl.ghost:
                return s0 / FPS, (s0 + sl.frames) / FPS, sl.xfade / FPS
        return st / FPS, (st + n) / FPS, xf / FPS

    # -- 보는 범위
    def fit(self) -> None:
        self._fit = True
        end = max(self.total, self.layout_[-1][0] / FPS + self.layout_[-1][1] / FPS
                  if self.layout_ else 0)
        self.pps = max(0.02, (self.width() - 20) / max(1.0, end))
        self.offset = 0.0
        self.view_changed.emit()

    def zoom(self, factor: float, at_x: float | None = None) -> None:
        self._fit = False
        at_x = self.width() / 2 if at_x is None else at_x
        t = self.x_to_t(at_x)
        self.pps = min(200.0, max(0.02, self.pps * factor))
        self.offset = max(0.0, t - at_x / self.pps)
        self.view_changed.emit()
        self.update()

    def visible_seconds(self) -> float:
        return self.width() / self.pps

    def content_seconds(self) -> float:
        end = self.total
        if self.layout_:
            end = max(end, (self.layout_[-1][0] + self.layout_[-1][1]) / FPS)
        return end + 5

    def set_offset(self, off: float) -> None:
        self.offset = max(0.0, off)
        self.update()

    def resizeEvent(self, e):           # noqa: N802
        if self._fit:
            self.fit()
        super().resizeEvent(e)

    def t_to_x(self, t: float) -> float:
        return (t - self.offset) * self.pps

    def x_to_t(self, x: float) -> float:
        return self.offset + x / self.pps

    def follow(self, t: float) -> None:
        """재생 중 재생 선이 화면 밖으로 나가면 따라간다."""
        x = self.t_to_x(t)
        if x < 0 or x > self.width() - 30:
            self.offset = max(0.0, t - 20 / self.pps)
            self.view_changed.emit()

    # -- 그리기
    def paintEvent(self, _e):           # noqa: N802
        t_ = theme_mod.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        w = self.width()
        p.fillRect(self.rect(), QColor(t_.panel_bg))
        y_text = RULER_H
        y_vid = RULER_H + TEXT_H + 2
        y_aud = y_vid + VIDEO_H + 4
        small = QFont(self.font())
        small.setPointSize(8)
        p.setFont(small)

        # 설교가 끝난 뒤 (장면이 넘쳐도 영상에는 안 나온다)
        xe = self.t_to_x(self.total)
        if xe < w:
            p.fillRect(QRectF(xe, RULER_H, w - xe, self.height() - RULER_H),
                       QColor(0, 0, 0, 90))

        # 눈금
        p.fillRect(QRectF(0, 0, w, RULER_H), QColor(t_.header_bg))
        step = self._tick_step()
        t0 = math.floor(self.offset / step) * step
        p.setPen(QColor(t_.text_dim))
        t = t0
        while self.t_to_x(t) < w:
            x = self.t_to_x(t)
            p.drawLine(QPointF(x, RULER_H - 6), QPointF(x, RULER_H))
            p.drawText(QPointF(x + 3, RULER_H - 8), _fmt(t))
            t += step

        # 글자 줄
        if self.text_mode == "plain":
            self._lane_bar(p, 0, self.total, y_text, QColor("#2b3f6b"), "곤색 바탕에 제목·본문")
        elif self.text_mode == "card":
            self._lane_bar(p, V.CARD_IN, V.CARD_OUT + V.CARD_FADE, y_text, QColor("#6b5a2b"),
                           "제목 카드")
            if self.corner:
                self._lane_bar(p, V.CORNER_IN, self.total, y_text, QColor("#4a4a4a"),
                               "왼쪽 위 작은 제목")

        # 장면 줄
        p.fillRect(QRectF(0, y_vid, w, VIDEO_H), QColor(t_.field_bg))
        if not self.items:
            p.setPen(QColor(t_.text_faint))
            p.drawText(QRectF(0, y_vid, w, VIDEO_H), Qt.AlignCenter,
                       "왼쪽 목록에서 사진·영상을 끌어 여기에 놓으세요   ·   "
                       "비워 두면 곤색 바탕에 제목·본문만 띄웁니다")
        for sl, s0 in zip(self.slots, self.starts):     # 되풀이한 칸 (흐리게)
            if sl.ghost:
                self._block(p, sl.item, s0 / FPS, (s0 + sl.frames) / FPS, sl.xfade / FPS,
                            y_vid, ghost=True)
        for k in range(len(self.items)):
            a, b, xf = self.item_span(k)
            self._block(p, k, a, b, xf, y_vid, ghost=False)
        if self._insert_at >= 0:
            x = self._insert_x(self._insert_at)
            p.fillRect(QRectF(x - 2, y_vid - 2, 4, VIDEO_H + 4), QColor(t_.accent))

        # 소리 줄
        p.fillRect(QRectF(0, y_aud, w, AUDIO_H), QColor(t_.field_bg))
        if self.peaks:
            p.setPen(QColor("#3fae6a"))
            mid = y_aud + AUDIO_H / 2
            n = len(self.peaks)
            for x in range(w):
                ta, tb = self.x_to_t(x), self.x_to_t(x + 1)
                ia, ib = int(ta * self.peaks_per_s), int(tb * self.peaks_per_s) + 1
                if ia >= n:
                    break
                if ib <= 0:
                    continue
                v = max(self.peaks[max(0, ia):min(n, ib)] or [0.0])
                hh = v * (AUDIO_H / 2 - 3)
                p.drawLine(QPointF(x, mid - hh), QPointF(x, mid + hh))
        else:
            p.setPen(QColor(t_.text_faint))
            p.drawText(QRectF(6, y_aud, w, AUDIO_H), Qt.AlignVCenter | Qt.AlignLeft,
                       "설교 소리 (구간을 정하면 파형이 나옵니다)")

        # 재생 선
        x = self.t_to_x(self.playhead)
        if -2 <= x <= w + 2:
            p.setPen(QPen(QColor("#ff4d4d"), 1.5))
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
            tri = QPainterPath()
            tri.moveTo(x - 6, 0)
            tri.lineTo(x + 6, 0)
            tri.lineTo(x, 8)
            tri.closeSubpath()
            p.fillPath(tri, QColor("#ff4d4d"))
        p.end()

    def _tick_step(self) -> float:
        for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800):
            if s * self.pps >= 70:
                return s
        return 3600

    def _lane_bar(self, p, a, b, y, color, label) -> None:
        xa, xb = self.t_to_x(a), self.t_to_x(b)
        if xb < 0 or xa > self.width():
            return
        r = QRectF(xa, y + 2, max(2.0, xb - xa), TEXT_H - 4)
        p.fillRect(r, color)
        p.setPen(QColor("#e8e8e8"))
        p.drawText(r.adjusted(4, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, label)

    def _block(self, p, k, a, b, xf, y, ghost) -> None:
        if not (0 <= k < len(self.items)):
            return
        t_ = theme_mod.current()
        xa, xb = self.t_to_x(a), self.t_to_x(b)
        if xb < 0 or xa > self.width():
            return
        it = self.items[k]
        r = QRectF(xa, y, max(3.0, xb - xa), VIDEO_H)
        p.save()
        p.setClipRect(r)
        img = self.media.thumb(it)
        tw = VIDEO_H * 16 / 9
        if img is not None and r.width() > 6:
            x = r.left()
            if x < 0:                           # 왼쪽이 화면 밖이면 보이는 칸부터
                x += math.floor(-x / tw) * tw
            while x < min(r.right(), self.width()):
                p.drawImage(QRectF(x, y, tw, VIDEO_H), img)
                x += tw
        else:
            p.fillRect(r, QColor("#40506a" if it.get("kind") == "video" else "#505050"))
        if ghost:
            p.fillRect(r, QColor(20, 20, 20, 150))
        # 이름 띠
        band = QRectF(r.left(), y, r.width(), 15)
        p.fillRect(band, QColor(0, 0, 0, 150))
        p.setPen(QColor("#f0f0f0") if not ghost else QColor("#a0a0a0"))
        name = Path(it["path"]).name
        label = (f"{'↻ ' if ghost else ''}{name}  ·  {_short(b - a)}"
                 + ("  · 영상" if it.get("kind") == "video" else ""))
        p.drawText(band.adjusted(4, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, label)
        p.restore()
        # 넘김 표시 — 앞 장면과 겹치는 곳
        if xf > 0 and not ghost:
            xr = self.t_to_x(a + xf)
            tr = QRectF(xa, y + VIDEO_H - 16, max(6.0, xr - xa), 14)
            p.fillRect(tr, QColor(t_.accent).darker(130))
            p.setPen(QColor("#ffffff"))
            key = it.get("trans") or "dissolve"
            short = {"dissolve": "겹침", "black": "검게", "white": "하얗게", "push_left": "밀기◀",
                     "push_right": "밀기▶", "wipe_left": "닦기◀", "wipe_right": "닦기▶",
                     "circle": "원"}.get(key, "")
            if tr.width() > 26:
                p.drawText(tr, Qt.AlignCenter, short)
        # 테두리
        sel = (k == self.sel) and not ghost
        p.setPen(QPen(QColor(t_.accent) if sel else QColor(0, 0, 0, 160), 2.5 if sel else 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
        if sel:   # 길이 손잡이
            p.fillRect(QRectF(r.right() - 5, y + 20, 5, VIDEO_H - 40), QColor(t_.accent))

    def _insert_x(self, at: int) -> float:
        if not self.items:
            return 4.0
        if at >= len(self.items):
            return self.t_to_x(self.item_span(len(self.items) - 1)[1])
        return self.t_to_x(self.item_span(at)[0])

    # -- 누르기
    def _hit(self, x: float, y: float) -> tuple[int, str]:
        y_vid = RULER_H + TEXT_H + 2
        if not (y_vid <= y <= y_vid + VIDEO_H):
            return -1, ""
        t = self.x_to_t(x)
        # 오른쪽 끝(길이 손잡이)을 먼저 본다 — 다음 장면과 겹치는 곳 안에 있어서
        # 장면 몸통부터 보면 다음 장면이 잡힌다.
        for k in range(len(self.items)):
            _, b, _ = self.item_span(k)
            if abs(self.t_to_x(b) - x) <= 7:
                return k, "edge"
        found = -1
        for k in range(len(self.items)):
            a, b, _ = self.item_span(k)
            if a <= t <= b:
                found = k            # 겹치는 곳에서는 뒤(들어오는) 장면
        return (found, "body") if found >= 0 else (-1, "")

    def _insert_index(self, x: float) -> int:
        t = self.x_to_t(x)
        for k in range(len(self.items)):
            a, b, _ = self.item_span(k)
            if t < (a + b) / 2:
                return k
        return len(self.items)

    def mousePressEvent(self, e):       # noqa: N802
        self.setFocus()
        x, y = e.position().x(), e.position().y()
        k, part = self._hit(x, y)
        self._press_x = x
        if e.button() != Qt.LeftButton:
            return
        if k >= 0:
            self.sel = k
            self.selected.emit(k)
            self._drag = "resize" if part == "edge" else "maybe_move"
            self._drag_i = k
        else:
            self._drag = "seek"
            self.seek.emit(max(0.0, min(self.total, self.x_to_t(x))))
        self.update()

    def mouseMoveEvent(self, e):        # noqa: N802
        x, y = e.position().x(), e.position().y()
        if self._drag is None:
            k, part = self._hit(x, y)
            self.setCursor(Qt.SizeHorCursor if part == "edge" else
                           Qt.OpenHandCursor if part == "body" else Qt.ArrowCursor)
            return
        if self._drag == "seek":
            self.seek.emit(max(0.0, min(self.total, self.x_to_t(x))))
        elif self._drag == "resize":
            a, _, _ = self.item_span(self._drag_i)
            dur = round(max(2.0, self.x_to_t(x) - a) * 2) / 2
            self.resize_item.emit(self._drag_i, dur, False)
        elif self._drag in ("maybe_move", "move"):
            if self._drag == "maybe_move" and abs(x - self._press_x) > 6:
                self._drag = "move"
                self.setCursor(Qt.ClosedHandCursor)
            if self._drag == "move":
                self._insert_at = self._insert_index(x)
                self.update()

    def mouseReleaseEvent(self, e):     # noqa: N802
        if self._drag == "move" and self._insert_at >= 0:
            self.move_item.emit(self._drag_i, self._insert_at)
        elif self._drag == "resize":
            a, _, _ = self.item_span(self._drag_i)
            dur = round(max(2.0, self.x_to_t(e.position().x()) - a) * 2) / 2
            self.resize_item.emit(self._drag_i, dur, True)
        self._drag = None
        self._insert_at = -1
        self.setCursor(Qt.ArrowCursor)
        self.update()

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        k, _ = self._hit(e.position().x(), e.position().y())
        if k >= 0:
            self.seek.emit(self.item_span(k)[0] + self.item_span(k)[2] + 0.05)

    def wheelEvent(self, e):            # noqa: N802
        d = e.angleDelta().y() or e.angleDelta().x()
        if e.modifiers() & Qt.ControlModifier:
            self.zoom(1.25 if d > 0 else 0.8, e.position().x())
        else:
            self._fit = False
            self.set_offset(self.offset - d / 120 * 60 / self.pps)
            self.view_changed.emit()
        e.accept()

    def keyPressEvent(self, e):         # noqa: N802
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.sel >= 0:
            self.delete_item.emit(self.sel)
        else:
            super().keyPressEvent(e)

    # -- 목록에서 끌어 놓기
    def dragEnterEvent(self, e):        # noqa: N802
        if e.mimeData().hasFormat(MIME) or e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):         # noqa: N802
        self._insert_at = self._insert_index(e.position().x())
        self.update()
        e.acceptProposedAction()

    def dragLeaveEvent(self, _e):       # noqa: N802
        self._insert_at = -1
        self.update()

    def dropEvent(self, e):             # noqa: N802
        at = self._insert_index(e.position().x())
        self._insert_at = -1
        md = e.mimeData()
        if md.hasFormat(MIME):
            items = json.loads(bytes(md.data(MIME)).decode("utf-8"))
        else:
            items = [{"__file__": u.toLocalFile()} for u in md.urls() if u.isLocalFile()]
        self.drop_items.emit(items, at)
        e.acceptProposedAction()
        self.update()


# ====================================================================== 편집기
class VisualPanel(QWidget):
    """[음성 설교] 칸 전체. app.py 는 이 위젯 하나만 다룬다."""

    make_clicked = Signal()
    title_edited = Signal(str)
    verse_edited = Signal(str)
    changed = Signal()                  # 기억해 둘 설정(음악 등)이 바뀌었다
    items_changed = Signal()            # 타임라인이 바뀌었다 — 설교 작업에 저장할 때
    range_requested = Signal()          # [설교 구간 정하기] — [설교 찾기] 로 돌아가게
    _peaks_ready = Signal(object, object)
    _texts_ready = Signal(object, object, object, object)

    def __init__(self, cfg_getter, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg_getter
        t = theme_mod.current()
        self._dim = f"color:{t.text_dim}; font-size:{t.font_small_pt}pt;"
        self._warn = f"color:{t.warn}; font-size:{t.font_small_pt}pt;"
        self.media = Media(lambda: self._cfg().cache_dir, self)
        self.renderer = Renderer(self.media)
        self.items: list[dict] = []
        self.ranges: list[tuple[float, float]] = []
        self.media_path: Path | None = None
        self.total = 60.0
        self._undo: list[list[dict]] = []
        self._redo: list[list[dict]] = []
        self._last_push = ("", 0.0)
        self._player: QMediaPlayer | None = None
        self._audio: QAudioOutput | None = None
        self._playing = False
        self._anchor_t = 0.0
        self._clock = QElapsedTimer()
        self._range_i = 0
        self._peaks_key = None
        self._text_key = None
        self._resize_snap = False
        self._scroll_lock = False

        self._build()
        self._peaks_ready.connect(self._on_peaks)
        self._texts_ready.connect(self._on_texts)
        self.media.ready.connect(lambda _p: self.monitor.update())
        self._tick = QTimer(self)
        self._tick.setInterval(33)
        self._tick.timeout.connect(self._on_tick)
        self._text_timer = QTimer(self)
        self._text_timer.setSingleShot(True)
        self._text_timer.setInterval(500)
        self._text_timer.timeout.connect(self._make_texts)
        self._load_bin()
        self._replan()
        self._make_texts()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.media.close)

    # ------------------------------------------------------------------ 짜임
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)
        top = QSplitter(Qt.Horizontal)
        top.setChildrenCollapsible(False)
        outer.addWidget(top, 1)

        # ---- 왼쪽: 사진·영상 목록
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(4)
        head = QLabel("사진·영상 목록")
        head.setStyleSheet("font-weight:600;")
        lv.addWidget(head)
        self.bin = MediaBin(self.media)
        self.bin.add_requested.connect(self._add_files)
        self.bin.itemDoubleClicked.connect(lambda row: self._append([row.data(Qt.UserRole)]))
        lv.addWidget(self.bin, 1)
        r1 = QHBoxLayout()
        self.btn_add = QPushButton("넣기…")
        self.btn_add.setToolTip("사진·영상 파일을 골라 목록에 넣습니다 (여러 개 고를 수 있습니다).")
        self.btn_add.clicked.connect(self._pick_files)
        self.btn_add_folder = QPushButton("폴더째…")
        self.btn_add_folder.setToolTip("폴더 안의 사진·영상을 이름 순서대로 모두 넣습니다.")
        self.btn_add_folder.clicked.connect(self._pick_folder)
        self.btn_bin_del = QPushButton("빼기")
        self.btn_bin_del.setToolTip("고른 것을 목록에서 뺍니다 (파일은 지우지 않습니다).")
        self.btn_bin_del.clicked.connect(self._remove_from_bin)
        for b in (self.btn_add, self.btn_add_folder, self.btn_bin_del):
            r1.addWidget(b)
        lv.addLayout(r1)
        r2 = QHBoxLayout()
        self.btn_append = QPushButton("타임라인에 ↓")
        self.btn_append.setToolTip("고른 것을 타임라인 끝에 붙입니다.")
        self.btn_append.clicked.connect(lambda: self._append(self.bin.chosen()))
        self.btn_all = QToolButton()
        self.btn_all.setText("모두 넣기 ▾")
        self.btn_all.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.btn_all)
        menu.addAction("목록 순서대로 모두 넣기", lambda: self._fill_all(False))
        menu.addAction("섞어서 모두 넣기", lambda: self._fill_all(True))
        menu.addSeparator()
        menu.addAction("타임라인 비우기", self._clear_timeline)
        self.btn_all.setMenu(menu)
        r2.addWidget(self.btn_append, 1)
        r2.addWidget(self.btn_all)
        lv.addLayout(r2)
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("사진 기본 길이"))
        self.default_box = QDoubleSpinBox()
        self.default_box.setRange(2, 600)
        self.default_box.setDecimals(0)
        self.default_box.setSuffix(" 초")
        self.default_box.setValue(V.PHOTO_SECONDS)
        self.default_box.setToolTip("목록에서 타임라인으로 넣을 때 사진 한 장의 길이입니다.\n"
                                    "넣은 뒤에는 장면마다 따로 바꿀 수 있습니다.")
        self.default_box.valueChanged.connect(lambda *_: self.changed.emit())
        r3.addWidget(self.default_box, 1)
        lv.addLayout(r3)
        left.setMinimumWidth(220)
        top.addWidget(left)

        # ---- 가운데: 미리 보기
        mid = QWidget()
        mv = QVBoxLayout(mid)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(4)
        self.monitor = Monitor(self.renderer)
        mv.addWidget(self.monitor, 1)
        tr = QHBoxLayout()
        tr.setSpacing(4)
        self.btn_home = QPushButton("⏮")
        self.btn_home.setToolTip("처음으로")
        self.btn_back = QPushButton("◀ 5초")
        self.btn_play = QPushButton("▶ 재생")
        self.btn_play.setProperty("primary", True)
        self.btn_play.setToolTip("소리와 함께 미리 봅니다 (Space)")
        self.btn_fwd = QPushButton("5초 ▶")
        self.btn_prev_cut = QPushButton("|◀ 장면")
        self.btn_prev_cut.setToolTip("앞 장면이 시작하는 곳으로")
        self.btn_next_cut = QPushButton("장면 ▶|")
        self.btn_next_cut.setToolTip("다음 장면이 시작하는 곳으로")
        self.btn_home.clicked.connect(lambda: self.seek(0.0))
        self.btn_back.clicked.connect(lambda: self.step(-5))
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_fwd.clicked.connect(lambda: self.step(5))
        self.btn_prev_cut.clicked.connect(lambda: self._jump_cut(-1))
        self.btn_next_cut.clicked.connect(lambda: self._jump_cut(1))
        for b in (self.btn_home, self.btn_prev_cut, self.btn_back):
            tr.addWidget(b)
        tr.addWidget(self.btn_play, 1)
        for b in (self.btn_fwd, self.btn_next_cut):
            tr.addWidget(b)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("font-family:Consolas,'D2Coding',monospace;")
        tr.addWidget(self.time_label)
        mv.addLayout(tr)
        top.addWidget(mid)

        # ---- 오른쪽: 고른 장면 · 글자 · 음악 · 만들기
        side_body = QWidget()
        sv = QVBoxLayout(side_body)
        sv.setContentsMargins(0, 0, 4, 0)
        sv.setSpacing(6)

        g1 = QGroupBox("고른 장면")
        f1 = QFormLayout(g1)
        f1.setVerticalSpacing(4)
        self.sel_name = QLabel("타임라인에서 장면을 누르세요")
        self.sel_name.setWordWrap(True)
        self.sel_name.setStyleSheet(self._dim)
        f1.addRow(self.sel_name)
        self.dur_box = QDoubleSpinBox()
        self.dur_box.setRange(2, 3600)
        self.dur_box.setDecimals(1)
        self.dur_box.setSingleStep(0.5)
        self.dur_box.setSuffix(" 초")
        self.dur_box.setToolTip("이 장면을 보여 주는 시간입니다 (넘기는 시간 포함).\n"
                                "타임라인에서 장면 오른쪽 끝을 끌어도 바뀝니다.")
        f1.addRow("길이", self.dur_box)
        self.motion_box = QComboBox()
        for key, (label, _n) in V.MOTION_CHOICES.items():
            self.motion_box.addItem(label, key)
        self.motion_box.setToolTip("사진이 천천히 다가가거나 물러나거나 옆으로 흐르는 모양입니다.\n"
                                   "영상은 움직이지 않고 제 화면 그대로 씁니다.")
        f1.addRow("움직임", self.motion_box)
        trow = QHBoxLayout()
        self.trans_box = QComboBox()
        for key, (label, _n) in V.TRANSITIONS.items():
            self.trans_box.addItem(label, key)
        self.trans_box.setToolTip("앞 장면에서 이 장면으로 넘어오는 모양입니다.")
        self.trans_len = QDoubleSpinBox()
        self.trans_len.setRange(V.TRANS_MIN, V.TRANS_MAX)
        self.trans_len.setDecimals(1)
        self.trans_len.setSingleStep(0.1)
        self.trans_len.setSuffix(" 초")
        trow.addWidget(self.trans_box, 1)
        trow.addWidget(self.trans_len)
        f1.addRow("넘기기", trow)
        self.in_box = QDoubleSpinBox()
        self.in_box.setRange(0, 0)
        self.in_box.setDecimals(1)
        self.in_box.setSingleStep(0.5)
        self.in_box.setSuffix(" 초부터")
        self.in_box.setToolTip("영상의 어디부터 쓸지 정합니다. 장면이 영상보다 길면 처음부터 되풀이합니다.")
        f1.addRow("영상 시작", self.in_box)
        brow = QHBoxLayout()
        self.btn_apply_all = QPushButton("모든 장면에 같게")
        self.btn_apply_all.setToolTip("이 장면의 움직임·넘기기(와 사진이면 길이)를 모든 장면에 똑같이 씁니다.")
        self.btn_apply_all.clicked.connect(self._apply_all)
        self.btn_del = QPushButton("빼기")
        self.btn_del.setToolTip("타임라인에서 이 장면을 뺍니다 (Delete).")
        self.btn_del.clicked.connect(lambda: self._delete(self.timeline.sel))
        brow.addWidget(self.btn_apply_all, 1)
        brow.addWidget(self.btn_del)
        f1.addRow(brow)
        self.dur_box.valueChanged.connect(lambda v: self._edit_sel("dur", float(v)))
        self.motion_box.currentIndexChanged.connect(
            lambda _i: self._edit_sel("motion", self.motion_box.currentData()))
        self.trans_box.currentIndexChanged.connect(
            lambda _i: self._edit_sel("trans", self.trans_box.currentData()))
        self.trans_len.valueChanged.connect(lambda v: self._edit_sel("trans_s", float(v)))
        self.in_box.valueChanged.connect(lambda v: self._edit_sel("in", float(v)))
        sv.addWidget(g1)

        g2 = QGroupBox("화면 글자")
        f2 = QFormLayout(g2)
        f2.setVerticalSpacing(4)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("예: 우리는 무엇을 믿고 도전하나")
        self.title_edit.setToolTip("오른쪽 [설교 제목] 칸과 같은 글자입니다. 처음 8초 동안 가운데에\n"
                                   "크게 나오고, 썸네일과 파일 이름에도 쓰입니다.")
        self.title_edit.textEdited.connect(self.title_edited)
        self.title_edit.textChanged.connect(lambda *_: self._text_timer.start())
        f2.addRow("설교 제목", self.title_edit)
        self.verse_edit = QLineEdit()
        self.verse_edit.setPlaceholderText("예: 창세기 12:1-4")
        self.verse_edit.textEdited.connect(self.verse_edited)
        self.verse_edit.textChanged.connect(lambda *_: self._text_timer.start())
        f2.addRow("본문", self.verse_edit)
        self.corner_check = QCheckBox("8초 뒤에도 왼쪽 위에 작게")
        self.corner_check.setChecked(True)
        self.corner_check.toggled.connect(lambda *_: (self._text_timer.start(), self.changed.emit()))
        f2.addRow("", self.corner_check)
        sv.addWidget(g2)

        g3 = QGroupBox("배경 음악 · 남는 시간")
        f3 = QFormLayout(g3)
        f3.setVerticalSpacing(4)
        mrow = QHBoxLayout()
        self.music_check = QCheckBox("넣기")
        self.music_edit = QLineEdit()
        self.music_edit.setReadOnly(True)
        self.music_edit.setPlaceholderText("음악 파일")
        self.btn_music = QPushButton("고르기")
        self.btn_music.clicked.connect(self._pick_music)
        mrow.addWidget(self.music_check)
        mrow.addWidget(self.music_edit, 1)
        mrow.addWidget(self.btn_music)
        f3.addRow(mrow)
        self.level_box = QComboBox()
        for key, label in V.MUSIC_LABELS.items():
            self.level_box.addItem(label, key)
        self.level_box.setCurrentIndex(max(0, self.level_box.findData("low")))
        self.level_box.setToolTip("목사님이 말씀하실 때는 15dB 더 줄였다가 한참 쉬실 때만 살짝\n"
                                  "올라옵니다 (미리 보기에서는 음악이 나오지 않습니다).")
        f3.addRow("음악 크기", self.level_box)
        self.fill_box = QComboBox()
        self.fill_box.addItem("처음 장면부터 되풀이", "repeat")
        self.fill_box.addItem("마지막 장면을 끝까지", "hold")
        self.fill_box.setToolTip("타임라인의 장면이 설교보다 짧을 때 남는 시간을 어떻게 채울지.")
        self.fill_box.currentIndexChanged.connect(lambda *_: (self._replan(), self.changed.emit()))
        f3.addRow("장면이 모자라면", self.fill_box)
        self.music_check.toggled.connect(self._sync_music)
        for sig in (self.music_check.toggled, self.level_box.currentIndexChanged):
            sig.connect(lambda *_: self.changed.emit())
        sv.addWidget(g3)

        self.intro_note = QLabel("")
        self.intro_note.setStyleSheet(self._dim)
        self.intro_note.setWordWrap(True)
        sv.addWidget(self.intro_note)
        sv.addStretch(1)
        self.btn_make = QPushButton("음성 설교 영상 만들기")
        self.btn_make.setProperty("primary", True)
        self.btn_make.setMinimumHeight(38)
        self.btn_make.setToolTip("타임라인대로 설교 구간 소리에 화면을 입혀 가로 영상(1920x1080)을\n"
                                 "만듭니다. 썸네일 그림과 제목·설명 글도 함께 나옵니다.")
        self.btn_make.clicked.connect(self.make_clicked)
        sv.addWidget(self.btn_make)
        side = QScrollArea()
        side.setWidgetResizable(True)
        side.setFrameShape(QFrame.NoFrame)
        side.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side.setWidget(side_body)
        side.setMinimumWidth(300)
        top.addWidget(side)
        top.setStretchFactor(0, 2)
        top.setStretchFactor(1, 5)
        top.setStretchFactor(2, 3)
        top.setSizes([250, 700, 330])

        # ---- 아래: 타임라인
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self.tl_info = QLabel("")
        self.tl_info.setStyleSheet(self._dim)
        bar.addWidget(self.tl_info, 1)
        self.btn_range = QPushButton("← 설교 구간부터 정하기")
        self.btn_range.setToolTip("설교 구간이 아직 없습니다. [설교 찾기] 로 가서 구간을 정하세요.\n"
                                  "타임라인은 그 길이에 맞춰집니다.")
        self.btn_range.setStyleSheet(f"color:{theme_mod.current().warn}; font-weight:600;")
        self.btn_range.clicked.connect(self.range_requested)
        self.btn_range.setFixedHeight(24)
        bar.addWidget(self.btn_range)
        self.btn_undo = QPushButton("되돌리기")
        self.btn_undo.setToolTip("타임라인에서 방금 한 일을 되돌립니다 (Ctrl+Z).")
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo = QPushButton("다시")
        self.btn_redo.setToolTip("되돌린 것을 다시 합니다 (Ctrl+Y).")
        self.btn_redo.clicked.connect(self.redo)
        self.btn_zoom_out = QPushButton("－")
        self.btn_zoom_out.setToolTip("타임라인을 넓게 봅니다 (Ctrl+휠)")
        self.btn_zoom_in = QPushButton("＋")
        self.btn_zoom_in.setToolTip("타임라인을 자세히 봅니다 (Ctrl+휠)")
        self.btn_fit = QPushButton("전체")
        self.btn_fit.setToolTip("설교 전체가 한눈에 보이게")
        for b in (self.btn_undo, self.btn_redo, self.btn_zoom_out, self.btn_zoom_in, self.btn_fit):
            b.setFixedHeight(24)
            bar.addWidget(b)
        outer.addLayout(bar)
        self.timeline = Timeline(self.media)
        outer.addWidget(self.timeline)
        self.tl_scroll = QScrollBar(Qt.Horizontal)
        outer.addWidget(self.tl_scroll)
        self.btn_zoom_in.clicked.connect(lambda: self.timeline.zoom(1.6))
        self.btn_zoom_out.clicked.connect(lambda: self.timeline.zoom(1 / 1.6))
        self.btn_fit.clicked.connect(lambda: (self.timeline.fit(), self.timeline.update()))
        self.timeline.seek.connect(self.seek)
        self.timeline.selected.connect(self._show_sel)
        self.timeline.move_item.connect(self._move)
        self.timeline.resize_item.connect(self._resize)
        self.timeline.drop_items.connect(self._drop)
        self.timeline.delete_item.connect(self._delete)
        self.timeline.view_changed.connect(self._sync_scroll)
        self.tl_scroll.valueChanged.connect(
            lambda v: self.timeline.set_offset(v / 10.0) if not self._scroll_lock else None)
        self._show_sel(-1)
        self._sync_music(False)
        self.set_intro("")

    # ------------------------------------------------------------------ 밖에서 쓰는 것
    def set_session(self, media_path: Path | None, ranges: list[tuple[float, float]],
                    items: list[dict] | None) -> None:
        """다루는 설교가 바뀌었을 때 — 소리 파일, 설교 구간, 저장해 둔 타임라인."""
        self.stop()
        self.media_path = Path(media_path) if media_path else None
        self.ranges = [(float(a), float(b)) for a, b in ranges if b > a]
        self.items = copy.deepcopy(items or [])
        self._undo.clear()
        self._redo.clear()
        self.timeline.sel = -1
        self.timeline.peaks = []
        self._replan()
        self._show_sel(-1)
        # 0초는 검은 화면에서 드러나기 시작하는 곳이라, 제목 카드가 보이는 데서 연다
        self.seek(min(3.0, self.total))
        self._load_peaks()

    def set_ranges(self, ranges: list[tuple[float, float]]) -> None:
        """설교 구간만 바뀌었을 때 (타임라인은 그대로)."""
        new = [(float(a), float(b)) for a, b in ranges if b > a]
        if new == self.ranges:
            return
        self.stop()
        self.ranges = new
        self.timeline.peaks = []
        self._replan()
        self._load_peaks()

    def set_options(self, opts: V.Options, music_path: str = "") -> None:
        """기억해 둔 값으로 채운다 (글자·음악·남는 시간·기본 길이)."""
        widgets = (self.default_box, self.corner_check, self.music_check, self.level_box,
                   self.fill_box)
        for w in widgets:
            w.blockSignals(True)
        self.default_box.setValue(float(opts.photo_seconds or V.PHOTO_SECONDS))
        self.title_edit.setText(opts.title)
        self.verse_edit.setText(opts.verse)
        self.corner_check.setChecked(bool(opts.corner))
        self.music_edit.setText(opts.music or music_path)
        self.music_check.setChecked(bool(opts.music))
        self.level_box.setCurrentIndex(max(0, self.level_box.findData(opts.music_level or "low")))
        self.fill_box.setCurrentIndex(max(0, self.fill_box.findData(opts.fill or "repeat")))
        for w in widgets:
            w.blockSignals(False)
        self._sync_music(self.music_check.isChecked())
        self._replan()
        self._text_timer.start()

    def set_title(self, text: str) -> None:
        if self.title_edit.text() != text:
            self.title_edit.setText(text)

    def set_verse(self, text: str) -> None:
        if self.verse_edit.text() != text:
            self.verse_edit.setText(text)

    def set_intro(self, intro: str) -> None:
        """'시작 영상' 은 설교 앞에 붙이는 짧은 영상(교회 로고·인사말)이다."""
        self.intro_note.setText(
            "앞에 붙일 시작 영상(교회 로고·인사말): "
            + (Path(intro).name if intro else "없음 — 바로 설교부터 시작합니다")
            + "  ·  [고급 설정] 에서 고르거나 뺍니다. 미리 보기에는 나오지 않습니다.")

    def options(self) -> V.Options:
        return V.Options(
            folder="",
            photo_seconds=float(self.default_box.value()),
            title=" ".join(self.title_edit.text().split()),
            verse=" ".join(self.verse_edit.text().split()),
            corner=self.corner_check.isChecked(),
            music=self.music_edit.text().strip() if self.music_check.isChecked() else "",
            music_level=str(self.level_box.currentData() or "low"),
            items=copy.deepcopy(self.items),
            fill=str(self.fill_box.currentData() or "repeat"),
        )

    def problem(self) -> str:
        if self.music_check.isChecked() and not Path(self.music_edit.text()).is_file():
            return ("배경 음악을 넣으려면 음악 파일을 골라 주세요.\n"
                    "넣지 않으려면 [배경 음악] 의 [넣기] 를 끄세요.")
        gone = [Path(it["path"]).name for it in self.items if not Path(it["path"]).is_file()]
        if gone:
            return ("타임라인에 있는 파일을 찾지 못했습니다 (옮기거나 지웠나요?):\n  "
                    + "\n  ".join(gone[:6]) + ("\n  …" if len(gone) > 6 else "")
                    + "\n\n타임라인에서 빼거나 파일을 제자리에 돌려놓아 주세요.")
        return ""

    # ------------------------------------------------------------------ 계획 다시 짜기
    def _replan(self) -> None:
        self.total = (sum(b - a for a, b in self.ranges) + V.TAIL_PAD) if self.ranges else \
            max(60.0, (V.item_layout(self.items)[-1][0] + V.item_layout(self.items)[-1][1]) / FPS
                if self.items else 60.0)
        frames = int(math.ceil(self.total * FPS))
        fill = str(self.fill_box.currentData() or "repeat")
        for it in self.items:             # 재생할 때 끊기지 않게 미리 준비해 둔다
            if it.get("kind") == "video":
                self.media.proxy_frame(it, 0.0)
            else:
                self.media.photo(it)
        slots = V.slots_from_items(self.items, frames, fill)
        self.renderer.set_plan(self.items, slots, self.total)
        self.timeline.set_plan(self.items, slots, self.total)
        self.timeline.text_mode = "card" if self.items else "plain"
        self.timeline.corner = self.corner_check.isChecked()
        self._sync_scroll()
        used = sum(1 for s in slots if not s.ghost)
        ghosts = len(slots) - used
        if not self.items:
            info = (f"설교 {_fmt(self.total)}  ·  장면이 없어 짙은 곤색 바탕에 제목·본문을 띄웁니다")
        else:
            lay = V.item_layout(self.items)
            span = (lay[-1][0] + lay[-1][1]) / FPS
            info = f"장면 {len(self.items)}개 · {_fmt(span)}  /  설교 {_fmt(self.total)}"
            if ghosts and fill == "repeat":
                info += f"  —  남는 시간은 처음 장면부터 되풀이 (흐린 칸 {ghosts}개)"
            elif span < self.total - 0.5 and fill == "hold":
                info += "  —  남는 시간은 마지막 장면을 늘입니다"
            elif span > self.total + 0.5:
                info += "  —  설교 끝을 넘는 장면은 영상에 나오지 않습니다 (어둡게 표시)"
        if not self.ranges:
            info += "   ·   설교 구간을 정하면 그 길이에 맞춥니다"
        self.btn_range.setVisible(not self.ranges)
        self.tl_info.setText(info)
        self.btn_undo.setEnabled(bool(self._undo))
        self.btn_redo.setEnabled(bool(self._redo))
        self._update_time()
        self.monitor.update()

    def _commit(self) -> None:
        self._replan()
        self.items_changed.emit()

    def _push_undo(self, tag: str = "") -> None:
        now = time.monotonic()
        if tag and self._last_push[0] == tag and now - self._last_push[1] < 1.5:
            self._last_push = (tag, now)
            return                     # 칸을 연달아 돌리는 것은 한 번으로 친다
        self._last_push = (tag, now)
        self._undo.append(copy.deepcopy(self.items))
        del self._undo[:-100]
        self._redo.clear()

    def undo(self) -> None:
        if not self._undo:
            return
        self._redo.append(copy.deepcopy(self.items))
        self.items = self._undo.pop()
        self._last_push = ("", 0.0)
        self._after_restore()

    def redo(self) -> None:
        if not self._redo:
            return
        self._undo.append(copy.deepcopy(self.items))
        self.items = self._redo.pop()
        self._last_push = ("", 0.0)
        self._after_restore()

    def _after_restore(self) -> None:
        sel = self.timeline.sel if self.timeline.sel < len(self.items) else -1
        self.timeline.sel = sel
        self._commit()
        self._show_sel(sel)

    # ------------------------------------------------------------------ 목록
    def _load_bin(self) -> None:
        st = QSettings(ORG, APP)
        try:
            items = json.loads(str(st.value("visual_bin", "") or "[]"))
        except ValueError:
            items = []
        if not items:
            # 예전 판에서 고른 폴더가 있으면 그 안의 것을 목록에 올려 둔다
            folder = str(st.value("visual_folder", "") or "")
            photos, videos = V.list_media(folder)
            if photos or videos:
                items = [x for x in (V.new_item(p) for p in
                                     sorted(photos + videos, key=V._natural)) if x]
        self.bin.set_items([it for it in items if isinstance(it, dict) and "path" in it])

    def _save_bin(self) -> None:
        QSettings(ORG, APP).setValue(
            "visual_bin", json.dumps(self.bin.all_items(), ensure_ascii=False))

    def _add_files(self, paths: list[str]) -> list[dict]:
        have = {it["path"] for it in self.bin.all_items()}
        added: list[dict] = []
        skipped: list[str] = []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for p in paths:
                if Path(p).is_dir():
                    ph, vi = V.list_media(p)
                    sub = [str(x) for x in sorted(ph + vi, key=V._natural)]
                else:
                    sub = [p]
                for q in sub:
                    if q in have:
                        added.append(next(it for it in self.bin.all_items() if it["path"] == q))
                        continue
                    it = V.new_item(q, seconds=self.default_box.value())
                    if it is None:
                        skipped.append(Path(q).name)
                        continue
                    have.add(q)
                    self.bin.add_item(it)
                    added.append(it)
        finally:
            QApplication.restoreOverrideCursor()
        self._save_bin()
        if skipped:
            self.monitor.note = "쓸 수 없어 건너뜀: " + ", ".join(skipped[:4]) + \
                ("…" if len(skipped) > 4 else "")
            QTimer.singleShot(6000, self._clear_note)
            self.monitor.update()
        return added

    def _clear_note(self) -> None:
        self.monitor.note = ""
        self.monitor.update()

    def _pick_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(V.IMAGE_EXT | V.VIDEO_EXT))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "사진·영상 넣기", self._last_dir(), f"사진·영상 ({exts});;모든 파일 (*.*)")
        if paths:
            self._remember_dir(str(Path(paths[0]).parent))
            self._add_files(paths)

    def _pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "사진·영상 폴더", self._last_dir())
        if path:
            self._remember_dir(path)
            self._add_files([path])

    def _last_dir(self) -> str:
        st = QSettings(ORG, APP)
        return str(st.value("visual_last_dir", "") or st.value("visual_folder", "") or
                   str(Path.home()))

    def _remember_dir(self, path: str) -> None:
        QSettings(ORG, APP).setValue("visual_last_dir", path)

    def _remove_from_bin(self) -> None:
        for row in self.bin.selectedItems():
            self.bin.takeItem(self.bin.row(row))
        self._save_bin()

    def _fresh(self, it: dict) -> dict:
        """목록의 것을 타임라인에 놓을 새 장면으로 (사진은 지금의 기본 길이로)."""
        new = copy.deepcopy(it)
        if new.get("kind") != "video":
            new["dur"] = float(self.default_box.value())
        new.setdefault("motion", "auto")
        new.setdefault("trans", "dissolve")
        new.setdefault("trans_s", 1.0)
        new.setdefault("in", 0.0)
        return new

    def _append(self, items: list[dict]) -> None:
        if items:
            self._insert(items, len(self.items))

    def _insert(self, items: list[dict], at: int) -> None:
        self._push_undo()
        at = max(0, min(at, len(self.items)))
        new = [self._fresh(it) for it in items]
        self.items[at:at] = new
        self.timeline.sel = at
        self._commit()
        self._show_sel(at)

    def _drop(self, items: list, at: int) -> None:
        files = [x["__file__"] for x in items if "__file__" in x]
        if files:
            items = self._add_files(files)
        self._insert([x for x in items if "path" in x], at)

    def _fill_all(self, shuffle: bool) -> None:
        all_items = self.bin.all_items()
        if not all_items:
            return
        if shuffle:
            random.shuffle(all_items)
        self._push_undo()
        self.items = [self._fresh(it) for it in all_items]
        self.timeline.sel = -1
        self._commit()
        self._show_sel(-1)

    def _clear_timeline(self) -> None:
        if not self.items:
            return
        self._push_undo()
        self.items = []
        self.timeline.sel = -1
        self._commit()
        self._show_sel(-1)

    # ------------------------------------------------------------------ 타임라인 편집
    def _move(self, k: int, at: int) -> None:
        if not (0 <= k < len(self.items)) or at in (k, k + 1):
            return
        self._push_undo()
        it = self.items.pop(k)
        if at > k:
            at -= 1
        self.items.insert(at, it)
        self.timeline.sel = at
        self._commit()
        self._show_sel(at)

    def _resize(self, k: int, dur: float, done: bool) -> None:
        if not (0 <= k < len(self.items)):
            return
        if not self._resize_snap:
            self._push_undo()
            self._resize_snap = True
        self.items[k]["dur"] = float(dur)
        if done:
            self._resize_snap = False
            self._commit()
        else:
            self._replan()
        self._show_sel(k)

    def _delete(self, k: int) -> None:
        if not (0 <= k < len(self.items)):
            return
        self._push_undo()
        self.items.pop(k)
        sel = min(k, len(self.items) - 1)
        self.timeline.sel = sel
        self._commit()
        self._show_sel(sel)

    def _show_sel(self, k: int) -> None:
        on = 0 <= k < len(self.items)
        boxes = (self.dur_box, self.motion_box, self.trans_box, self.trans_len, self.in_box,
                 self.btn_apply_all, self.btn_del)
        for w in boxes:
            w.blockSignals(True)
            w.setEnabled(on)
        if on:
            it = self.items[k]
            video = it.get("kind") == "video"
            length = float(it.get("len") or 0)
            self.sel_name.setText(f"{k + 1}번째 · {Path(it['path']).name}"
                                  + (f"  (영상 {_fmt(length)})" if video else "  (사진)"))
            self.dur_box.setValue(float(it.get("dur") or V.PHOTO_SECONDS))
            self.motion_box.setCurrentIndex(max(0, self.motion_box.findData(it.get("motion") or "auto")))
            self.motion_box.setEnabled(not video)
            self.trans_box.setCurrentIndex(max(0, self.trans_box.findData(it.get("trans") or "dissolve")))
            self.trans_len.setValue(float(it.get("trans_s") or 1.0))
            first = k == 0
            self.trans_box.setEnabled(not first)
            self.trans_len.setEnabled(not first and (it.get("trans") or "dissolve") != "cut")
            if first:
                self.trans_box.setToolTip("첫 장면은 검은 화면에서 천천히 드러납니다.")
            self.in_box.setRange(0, max(0.0, length - 1.0))
            self.in_box.setValue(float(it.get("in") or 0))
            self.in_box.setEnabled(video)
        else:
            self.sel_name.setText("타임라인에서 장면을 누르세요" if self.items else
                                  "왼쪽 목록에서 사진·영상을 타임라인으로 끌어 놓으세요")
        for w in boxes:
            w.blockSignals(False)
        self.timeline.update()

    def _edit_sel(self, key: str, value) -> None:
        k = self.timeline.sel
        if not (0 <= k < len(self.items)) or self.items[k].get(key) == value:
            return
        self._push_undo(f"{k}:{key}")
        self.items[k][key] = value
        if key in ("in", "dur") and self.items[k].get("kind") == "video":
            self.media.forget_proxy(self.items[k]["path"])
        if key == "trans":
            self.trans_len.setEnabled(value != "cut")
        self._commit()

    def _apply_all(self) -> None:
        """고른 장면의 넘기기를 모든 장면에, 사진이면 움직임·길이도 모든 사진에."""
        k = self.timeline.sel
        if not (0 <= k < len(self.items)):
            return
        src = self.items[k]
        photo = src.get("kind") != "video"
        self._push_undo()
        for j, it in enumerate(self.items):
            if j and k:                         # 첫 장면에는 넘기기가 없다
                it["trans"] = src.get("trans", "dissolve")
                it["trans_s"] = src.get("trans_s", 1.0)
            if photo and it.get("kind") != "video":
                it["motion"] = src.get("motion", "auto")
                it["dur"] = src.get("dur", V.PHOTO_SECONDS)
        self._commit()

    # ------------------------------------------------------------------ 글자 그림
    def _make_texts(self) -> None:
        title = " ".join(self.title_edit.text().split())
        verse = " ".join(self.verse_edit.text().split())
        corner = self.corner_check.isChecked()
        key = (title, verse, corner)
        if key == self._text_key:
            return
        self._text_key = key
        self.timeline.corner = corner
        self.timeline.update()

        def work():
            from core.shorts import find_font
            card = cor = plain = None
            try:
                font = find_font()
                with tempfile.TemporaryDirectory() as tmp:
                    tmpdir = Path(tmp)
                    if font:
                        shutil.copyfile(font, tmpdir / "font.ttf")
                        if title or verse:
                            c = V.make_card(tmpdir, title, verse)
                            card = QImage(str(c)) if c else None
                            if corner:
                                c2 = V.make_corner(tmpdir, title, verse)
                                cor = QImage(str(c2)) if c2 else None
                    pl = V.make_plain(tmpdir, title, verse)
                    img = QImage(str(pl))
                    plain = img.scaled(PREVIEW_W, PREVIEW_H, Qt.IgnoreAspectRatio,
                                       Qt.SmoothTransformation) if not img.isNull() else None
            except Exception:
                pass
            self._texts_ready.emit(key, card, cor, plain)

        threading.Thread(target=work, daemon=True).start()

    def _on_texts(self, key, card, cor, plain) -> None:
        if key != self._text_key:
            return
        self.renderer.card = card if card is not None and not card.isNull() else None
        self.renderer.corner = cor if cor is not None and not cor.isNull() else None
        self.renderer.plain = plain
        self.monitor.update()

    # ------------------------------------------------------------------ 파형
    def _load_peaks(self) -> None:
        if not self.media_path or not self.ranges or not self.media_path.exists():
            return
        key = (str(self.media_path), tuple(self.ranges))
        self._peaks_key = key
        src, ranges = self.media_path, list(self.ranges)

        def work():
            try:
                peaks = V.speech_peaks(src, ranges, self.timeline.peaks_per_s)
            except Exception:
                peaks = []
            self._peaks_ready.emit(key, peaks)

        threading.Thread(target=work, daemon=True).start()

    def _on_peaks(self, key, peaks) -> None:
        if key == self._peaks_key:
            self.timeline.peaks = peaks
            self.timeline.update()

    # ------------------------------------------------------------------ 재생
    def _out_to_src(self, t: float) -> tuple[int, float] | None:
        acc = 0.0
        for i, (a, b) in enumerate(self.ranges):
            if t < acc + (b - a):
                return i, a + (t - acc)
            acc += b - a
        return None

    def _range_offset(self, i: int) -> float:
        return sum(b - a for a, b in self.ranges[:i])

    def _ensure_player(self) -> bool:
        if not self.media_path or not self.media_path.exists() or not self.ranges:
            return False
        if self._player is None:
            try:
                self._player = QMediaPlayer(self)
                self._audio = QAudioOutput(self)
                self._audio.setVolume(0.9)
                self._player.setAudioOutput(self._audio)
            except Exception:
                self._player = None
                return False
        want = QUrl.fromLocalFile(self.media_path.resolve().as_posix())
        if self._player.source() != want:
            self._player.setSource(want)
        return True

    def toggle_play(self) -> None:
        if self._playing:
            self.stop()
        else:
            self.play()

    def play(self) -> None:
        if self.timeline.playhead >= self.total - 0.1:
            self.seek(0.0)
        self._playing = True
        self.btn_play.setText("❚❚ 멈춤")
        self._anchor_t = self.timeline.playhead
        self._clock.start()
        if self._ensure_player():
            hit = self._out_to_src(self._anchor_t)
            if hit:
                self._range_i = hit[0]
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

    def _on_tick(self) -> None:
        t = self._anchor_t + self._clock.elapsed() / 1000.0
        if self._player is not None and self.ranges and \
                self._player.playbackState() == QMediaPlayer.PlayingState:
            i = self._range_i
            a, b = self.ranges[i]
            pos = self._player.position() / 1000.0
            if pos >= b - 0.02:
                # 이 구간이 끝났다 — 다음 구간으로 건너뛴다 (중간의 찬양·광고는 건너뜀)
                if i + 1 < len(self.ranges):
                    self._range_i = i + 1
                    self._player.setPosition(int(self.ranges[i + 1][0] * 1000))
                    self._anchor_t = self._range_offset(i + 1)
                    self._clock.restart()
                    t = self._anchor_t
                else:
                    self._player.pause()
            elif a - 0.5 <= pos < b:
                heard = self._range_offset(i) + (pos - a)
                if abs(heard - t) > 0.25:      # 소리를 따라간다
                    self._anchor_t = heard
                    self._clock.restart()
                    t = heard
        if t >= self.total:
            t = self.total
            self.stop()
        self.timeline.playhead = t
        self.timeline.follow(t)
        self.timeline.update()
        self.monitor.t = t
        self.monitor.update()
        self._update_time()

    def seek(self, t: float) -> None:
        t = max(0.0, min(self.total, float(t)))
        self.timeline.playhead = t
        self.monitor.t = t
        if self._playing:
            self._anchor_t = t
            self._clock.restart()
            hit = self._out_to_src(t)
            if self._player is not None and hit:
                self._range_i = hit[0]
                self._player.setPosition(int(hit[1] * 1000))
        self.timeline.update()
        self.monitor.update()
        self._update_time()

    def step(self, seconds: float) -> None:
        self.seek(self.timeline.playhead + seconds)

    def _jump_cut(self, direction: int) -> None:
        cuts = sorted({s / FPS for s in self.renderer.starts} | {0.0})
        now = self.timeline.playhead
        if direction > 0:
            nxt = [c for c in cuts if c > now + 0.05]
            self.seek(nxt[0] if nxt else self.total)
        else:
            prv = [c for c in cuts if c < now - 0.05]
            self.seek(prv[-1] if prv else 0.0)

    def _update_time(self) -> None:
        self.time_label.setText(f"{_fmt(self.timeline.playhead, True)} / {_fmt(self.total)}")

    def _sync_scroll(self) -> None:
        tl = self.timeline
        span = tl.content_seconds()
        vis = tl.visible_seconds()
        self._scroll_lock = True
        self.tl_scroll.setRange(0, max(0, int((span - vis) * 10)))
        self.tl_scroll.setPageStep(max(1, int(vis * 10)))
        self.tl_scroll.setValue(int(tl.offset * 10))
        self._scroll_lock = False
        self.tl_scroll.setVisible(span > vis + 0.5)

    def hideEvent(self, e):             # noqa: N802
        self.stop()                     # 다른 칸으로 가면 소리를 멈춘다
        super().hideEvent(e)

    # ------------------------------------------------------------------ 음악
    def _pick_music(self) -> None:
        start = str(Path(self.music_edit.text()).parent) if self.music_edit.text() else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "배경 음악", start, f"음악 ({V.MUSIC_EXT});;모든 파일 (*.*)")
        if path:
            self.music_edit.setText(path)
            self.music_check.setChecked(True)
            self.changed.emit()

    def _sync_music(self, on: bool) -> None:
        for w in (self.music_edit, self.btn_music, self.level_box):
            w.setEnabled(on)
