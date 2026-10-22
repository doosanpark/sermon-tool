"""맨 위 머리띠와 패널 테두리 (FilmCraft 화면 구조).

FilmCraft(MIT/Apache-2.0, ArtCraft 팀)의 `crates/ui-egui/src/header.rs` 와
`dock_ui.rs` 의 짜임새·치수를 옮겼다. Rust/egui 코드를 베낀 것이 아니라
치수와 그리는 방식을 보고 PySide6 로 다시 썼다.

가져온 치수:
    머리띠 높이 38, 양옆 여백 14, 모드 탭 사이 24, 고른 탭 밑줄 두께 2,
    오른쪽 단추 28x28 에 38 간격, 패널 제목 띠 32, 초점 테두리 1.

여기서 바꾼 것:
    * 패널 떼어내기(도킹)는 넣지 않았다. 쓰시는 분들이 패널을 끌어다 놓고
      되돌리지 못하면 화면이 망가진 것처럼 보이기 때문이다.
    * 글자가 큰 테마에서는 머리띠 높이를 글자에 맞춰 늘린다. 38 로 못박으면
      [큰 글자] 화면에서 모드 탭이 잘린다.
    * 모드 탭(설교 찾기·요약·만들기)은 아래 탭 셋과 하나씩 짝이 된다.
      위에서 눌러도 아래에서 눌러도 똑같이 움직이고, 어느 쪽도 숨기지 않는다.

색은 전부 theme.py 에서 읽는다. 이 파일에 색을 글자로 박지 않는다.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QApplication, QFrame, QVBoxLayout, QWidget

import theme as theme_mod

# FilmCraft header.rs 의 치수
PAD = 14          # 양옆 여백
TAB_GAP = 24      # 모드 탭 사이
UNDERLINE = 2     # 고른 탭 밑줄 두께
BTN = 28          # 오른쪽 단추 한 변
BTN_PITCH = 38    # 오른쪽 단추 사이 간격
STRIP_H = 32      # 패널 제목 띠 높이 (dock_ui.rs 의 tab_h)
LOG_LABEL = "기록"  # 오른쪽 [기록] 단추 — 진행 로그를 아래 칸에 띄운다


def _c(hexcolor: str, alpha: int = 255) -> QColor:
    col = QColor(hexcolor)
    col.setAlpha(alpha)
    return col


class HeaderBar(QWidget):
    """창 맨 위 띠 — 왼쪽에 이름과 모드 탭, 가운데 지금 영상, 오른쪽에 단추.

    mode_clicked 는 누른 모드의 번호(0부터)를 보낸다.
    """

    mode_clicked = Signal(int)
    fullscreen_clicked = Signal()
    log_clicked = Signal()

    def __init__(self, app_name: str, modes: list[str], parent=None):
        super().__init__(parent)
        self.app_name = app_name
        self.modes = list(modes)
        self.current = 0
        self.title = ""
        self.progress = ""      # 작업 중일 때 가운데에 띄우는 글
        self.corner = ""
        self.busy = False       # 작업이 도는 동안은 못 누르게 잠근다
        self._hot = -1          # 마우스가 올라간 모드 탭
        self._hot_btn = False   # 마우스가 올라간 전체화면 단추
        self._hot_log = False   # 마우스가 올라간 [기록] 단추
        self._tab_rects: list[QRect] = []
        self._btn_rect = QRect()
        self._log_rect = QRect()
        self.setMouseTracking(True)
        self.setFixedHeight(self._height())

    # ------------------------------------------------------------ 바깥에서 쓰는 것
    def set_mode(self, index: int) -> None:
        """-1 이면 어느 모드도 고르지 않은 상태 ([기록] 을 보고 있을 때)."""
        if -1 <= index < len(self.modes) and index != self.current:
            self.current = index
            self.update()

    def set_title(self, text: str) -> None:
        if text != self.title:
            self.title = text
            self.update()

    def set_busy(self, on: bool) -> None:
        """작업이 도는 동안 모드 탭과 [기록] 을 잠근다.

        잠그지 않으면 눌릴 것처럼 보이고 실제로 눌려서, 돌고 있는 작업과
        화면이 어긋난다. 전체 화면 단추는 그대로 둔다 — 눌러도 탈이 없다.
        """
        if on != self.busy:
            self.busy = on
            self.setCursor(Qt.ArrowCursor)
            self._hot, self._hot_log = -1, False
            self.update()

    def set_progress(self, text: str) -> None:
        """지금 도는 작업 이름. 빈 글이면 다시 영상 이름을 보여 준다.

        진행 로그는 [만들기] 칸에 있어서 다른 모드에 있으면 안 보인다. 오래
        걸리는 일이 도는지 어느 모드에서든 알 수 있어야 하므로 여기에 띄운다.
        """
        if text != self.progress:
            self.progress = text
            self.update()

    def set_corner(self, text: str) -> None:
        """오른쪽 끝에 작게 적는 글 (지금 화면 모양 이름)."""
        if text != self.corner:
            self.corner = text
            self.update()

    def refresh_theme(self) -> None:
        self.setFixedHeight(self._height())
        self.update()

    # ------------------------------------------------------------ 안에서 쓰는 것
    def _t(self):
        return theme_mod.current()

    def _height(self) -> int:
        """38 을 기본으로 하되, 글자가 커지면 잘리지 않게 늘린다."""
        t = self._t()
        f = QFont(self.font())
        f.setPointSize(t.font_big_pt)
        return max(38, QFontMetrics(f).height() + 18)

    def _fonts(self):
        t = self._t()
        tab = QFont(self.font())
        tab.setPointSize(t.font_big_pt)
        small = QFont(self.font())
        small.setPointSize(t.font_small_pt)
        return tab, small

    def _layout(self) -> int:
        """모드 탭과 단추 자리를 정한다. 왼쪽 덩어리가 끝나는 x 를 돌려준다."""
        t = self._t()
        tab_f, small_f = self._fonts()
        tab_fm, small_fm = QFontMetrics(tab_f), QFontMetrics(small_f)

        x = PAD + small_fm.horizontalAdvance(self.app_name) + 18
        self._tab_rects = []
        for name in self.modes:
            w = tab_fm.horizontalAdvance(name)
            self._tab_rects.append(QRect(x - 6, 6, w + 12, self.height() - 12))
            x += w + TAB_GAP
        left_end = x

        cy = self.height() // 2
        self._btn_rect = QRect(self.width() - PAD - BTN, cy - BTN // 2, BTN, BTN)
        log_w = small_fm.horizontalAdvance(LOG_LABEL) + 18
        self._log_rect = QRect(self._btn_rect.left() - 8 - log_w, cy - BTN // 2, log_w, BTN)
        _ = t
        return left_end

    # ------------------------------------------------------------ 그리기
    def paintEvent(self, event):  # noqa: N802
        t = self._t()
        left_end = self._layout()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), _c(t.header_bg))
        p.setPen(QPen(_c(t.separator), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

        tab_f, small_f = self._fonts()
        tab_fm, small_fm = QFontMetrics(tab_f), QFontMetrics(small_f)
        cy = self.height() // 2

        # 왼쪽: 프로그램 이름
        p.setFont(small_f)
        p.setPen(_c(t.text_faint))
        p.drawText(QRect(PAD, 0, small_fm.horizontalAdvance(self.app_name) + 2, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self.app_name)

        # 모드 탭 — 고른 것은 밝은 글자에 밑줄
        p.setFont(tab_f)
        for i, (name, r) in enumerate(zip(self.modes, self._tab_rects)):
            on = i == self.current
            if self.busy:
                col = t.text_faint                      # 잠긴 동안은 흐리게
            else:
                col = t.tab_text_active if on or i == self._hot else t.tab_text
            p.setPen(_c(col))
            p.drawText(r, Qt.AlignVCenter | Qt.AlignHCenter, name)
            if on:
                w = tab_fm.horizontalAdvance(name)
                y = self.height() - 7
                p.fillRect(QRect(r.center().x() - w // 2, y, w, UNDERLINE),
                           _c(t.separator if self.busy else t.accent))

        # 오른쪽 끝: 전체 화면 단추
        if self._hot_btn:
            p.fillRect(self._btn_rect, _c(t.hover))
        self._paint_fullscreen(p, self._btn_rect.adjusted(7, 7, -7, -7),
                               _c(t.text if self._hot_btn else t.text_dim))

        # 그 옆: [기록] 단추 — 진행 로그는 모드가 아니라 가끔 들여다보는 것이다.
        # 로그를 보고 있는 동안에는(current == -1) 눌린 것처럼 칠한다.
        log_on = self.current == -1
        if (log_on or self._hot_log) and not self.busy:
            p.fillRect(self._log_rect, _c(t.pill_active_bg if log_on else t.hover))
        elif log_on:
            p.fillRect(self._log_rect, _c(t.pill_active_bg))
        p.setFont(small_f)
        p.setPen(_c(t.text_faint if self.busy
                    else (t.text if log_on or self._hot_log else t.text_dim)))
        p.drawText(self._log_rect, Qt.AlignCenter, LOG_LABEL)

        # 그 옆: 지금 화면 모양 이름
        rx = self._log_rect.left()
        corner_w = 0
        if self.corner:
            p.setPen(_c(t.text_dim))
            corner_w = small_fm.horizontalAdvance(self.corner) + 12
            p.drawText(QRect(rx - corner_w, 0, corner_w - 6, self.height()),
                       Qt.AlignVCenter | Qt.AlignRight, self.corner)

        # 가운데: 작업이 돌면 진행 상황, 아니면 지금 영상 이름.
        # 진행 로그가 [기록] 안에 있어 다른 모드에서는 안 보이므로, 돌고 있다는
        # 사실만은 어디서든 보이게 여기에 띄운다.
        middle = self.progress or self.title
        left = left_end + 12
        right = rx - corner_w - 12
        if middle and right - left > 60:
            p.setFont(tab_f)
            p.setPen(_c(t.hot_text if self.progress else t.tab_text_active))
            box = QRect(left, 0, right - left, self.height())
            shown = tab_fm.elidedText(middle, Qt.ElideMiddle, box.width())
            p.drawText(box, Qt.AlignVCenter | Qt.AlignHCenter, shown)
        _ = cy
        p.end()

    @staticmethod
    def _paint_fullscreen(p: QPainter, r: QRect, col: QColor) -> None:
        """전체 화면 그림 — 네 귀퉁이 꺾쇠. 직접 그린 단순한 모양이다."""
        p.setPen(QPen(col, 1.6))
        n = max(3, r.width() // 3)
        for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
            x = r.left() if dx == 0 else r.right()
            y = r.top() if dy == 0 else r.bottom()
            sx = n if dx == 0 else -n
            sy = n if dy == 0 else -n
            p.drawLine(x, y, x + sx, y)
            p.drawLine(x, y, x, y + sy)

    # ------------------------------------------------------------ 마우스
    def mouseMoveEvent(self, event):  # noqa: N802
        pos = event.position().toPoint()
        if self.busy:                                   # 잠긴 동안은 손가락 모양도 안 준다
            if self._hot != -1 or self._hot_log:
                self._hot, self._hot_log = -1, False
                self.update()
            self.setCursor(Qt.ArrowCursor)
            return
        hot = -1
        for i, r in enumerate(self._tab_rects):
            if r.contains(pos):
                hot = i
                break
        hot_btn = self._btn_rect.contains(pos)
        hot_log = self._log_rect.contains(pos)
        if hot != self._hot or hot_btn != self._hot_btn or hot_log != self._hot_log:
            self._hot, self._hot_btn, self._hot_log = hot, hot_btn, hot_log
            self.setCursor(Qt.PointingHandCursor if hot >= 0 or hot_btn or hot_log
                           else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, event):  # noqa: N802
        if self._hot != -1 or self._hot_btn or self._hot_log:
            self._hot, self._hot_btn, self._hot_log = -1, False, False
            self.update()

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        if self.busy:                                   # 작업 중에는 전체 화면만 받는다
            if self._btn_rect.contains(pos):
                self.fullscreen_clicked.emit()
            return
        for i, r in enumerate(self._tab_rects):
            if r.contains(pos):
                self.set_mode(i)
                self.mode_clicked.emit(i)
                return
        if self._log_rect.contains(pos):
            self.log_clicked.emit()
            return
        if self._btn_rect.contains(pos):
            self.fullscreen_clicked.emit()


class Panel(QFrame):
    """제목 띠가 붙은 패널. 지금 쓰고 있는 패널에는 파란 테두리가 생긴다.

    FilmCraft 는 패널마다 위에 띠를 두고, 쓰고 있는 패널을 1px 파란 선으로
    두른다. 어느 칸을 만지고 있는지 한눈에 보이게 하는 장치다.
    """

    _all: list["Panel"] = []
    _hooked = False

    def __init__(self, title: str | None, body: QWidget, parent=None):
        """title 이 None 이면 제목 띠 없이 테두리만 두른다.

        안에 이미 제목이 있는 칸(묶음 상자)에 띠를 또 올리면 같은 말이 두 번
        적힌다. 그런 칸에는 초점 테두리만 준다.
        """
        super().__init__(parent)
        self.title = title
        self.focused = False
        self.setFrameShape(QFrame.NoFrame)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        self.strip = _Strip(title, self) if title else None
        if self.strip is not None:
            lay.addWidget(self.strip)
        lay.addWidget(body, 1)
        self.body = body

        Panel._all.append(self)
        Panel._hook()

    def set_title(self, text: str) -> None:
        self.title = text
        if self.strip is not None:
            self.strip.title = text
            self.strip.update()

    # --- 어느 패널을 쓰고 있는지 따라다니기 ---
    @classmethod
    def _hook(cls) -> None:
        app = QApplication.instance()
        if app is not None and not cls._hooked:
            app.focusChanged.connect(cls._on_focus_changed)
            cls._hooked = True

    @classmethod
    def _on_focus_changed(cls, _old, new) -> None:
        alive = []
        for panel in cls._all:
            try:
                panel._update_focus(new)
                alive.append(panel)
            except RuntimeError:          # 이미 지워진 위젯
                continue
        cls._all = alive

    def _update_focus(self, widget) -> None:
        on = bool(widget) and (widget is self or self.isAncestorOf(widget))
        if on != self.focused:
            self.focused = on
            self.update()

    def paintEvent(self, event):  # noqa: N802
        t = theme_mod.current()
        p = QPainter(self)
        p.fillRect(self.rect(), _c(t.panel_bg))
        p.setPen(QPen(_c(t.focus if self.focused else t.separator), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.end()


class _Strip(QWidget):
    """패널 맨 위 제목 띠 (FilmCraft dock_ui.rs 의 탭 띠, 높이 32)."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.setFixedHeight(self._height())

    def _height(self) -> int:
        t = theme_mod.current()
        f = QFont(self.font())
        f.setPointSize(t.font_small_pt)
        return max(STRIP_H, QFontMetrics(f).height() + 14)

    def refresh_theme(self) -> None:
        self.setFixedHeight(self._height())
        self.update()

    def paintEvent(self, event):  # noqa: N802
        t = theme_mod.current()
        f = QFont(self.font())
        f.setPointSize(t.font_small_pt)
        p = QPainter(self)
        p.fillRect(self.rect(), _c(t.panel_bg))
        p.setPen(QPen(_c(t.separator), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.setFont(f)
        p.setPen(_c(t.tab_text_active))
        p.drawText(self.rect().adjusted(10, 0, -10, -1),
                   Qt.AlignVCenter | Qt.AlignLeft, self.title)
        p.end()
