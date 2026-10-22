"""화면 색·크기를 한곳에 모은 것 (디자인 토큰).

FilmCraft(MIT/Apache-2.0, ArtCraft 팀)의 `crates/ui-egui/src/theme.rs` 를 옮겼다.
이름과 색 값은 그 파일의 것을 그대로 쓰고, Rust/egui 코드를 PySide6 에 맞게 다시 썼다.

왜 토큰인가:
    예전에는 색이 app.py·ui_summary.py 서른 곳에 글자로 박혀 있었다("color:#555").
    한 곳을 고치면 나머지가 어긋나고, 어두운 화면을 넣을 수도 없었다. 모든 위젯이
    여기서 색을 읽으면 테마를 바꿔도 화면 전체가 같이 바뀐다.

세 가지 테마 (FilmCraft 와 같은 구성):
    dark    FilmCraft 기본값. 영상 편집 프로그램의 어두운 화면 — 영상 색이 제대로 보인다.
    light   밝은 화면. 예전 설교 편집기 화면에 가깝다.
    big     밝은 화면 + 글자를 크게. 눈이 불편한 분을 위해 **이 프로그램에서 더한 것**이다.

글자 크기는 FilmCraft 값(12~13px)보다 키웠다. FilmCraft 는 영상 편집자가 쓰는
프로그램이라 한 화면에 많이 담는 쪽을 골랐지만, 이 프로그램은 교회에서 쓰므로
읽기 쉬운 쪽이 먼저다.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

# 테마 이름 — 화면의 [화면 모양] 상자와 같은 순서
THEMES = {
    "dark": "어두운 화면 (영상 편집용)",
    "light": "밝은 화면",
    "big": "밝은 화면 · 큰 글자",
}


@dataclass(frozen=True)
class Tokens:
    name: str = "dark"

    # --- 바탕 ---
    app_bg: str = "#000000"          # 창 바탕 (패널 사이 틈)
    header_bg: str = "#1d1d1d"       # 맨 위 띠
    panel_bg: str = "#1d1d1d"        # 패널 안쪽
    tab_bg: str = "#1d1d1d"          # 탭 띠
    tab_text: str = "#b0b0b0"
    tab_text_active: str = "#d1d1d1"

    # --- 글자 ---
    text: str = "#d1d1d1"
    text_dim: str = "#b0b0b0"        # 덧붙이는 설명
    text_faint: str = "#6e6e6e"      # 흐린 안내
    text_on_accent: str = "#ffffff"

    # --- 강조 ---
    focus: str = "#5794ec"           # 지금 보고 있는 패널의 테두리
    accent: str = "#2f6bdf"          # 주요 단추
    accent_hover: str = "#3f7ce8"
    accent_pressed: str = "#2558b8"
    hot_text: str = "#4096f3"         # 숫자·시각 (FilmCraft 의 scrubby 값 색)
    timecode: str = "#4096f3"

    # --- 누름·입력 ---
    hover: str = "#2c2c2c"
    pressed: str = "#4b4b4b"
    field_bg: str = "#0e0e0e"
    field_border: str = "#303030"
    separator: str = "#303030"
    row_alt: str = "#212121"
    row_selected: str = "#333333"
    pill_active_bg: str = "#3a3a3a"
    button_bg: str = "#2a2a2a"
    button_border: str = "#4b4b4b"
    button_hover: str = "#2a2a2a"
    disabled_text: str = "#6e6e6e"
    disabled_bg: str = "#232323"

    # --- 자리 (타임라인·구간 바) ---
    tl_bg: str = "#1d1d1d"
    tl_ruler_tick: str = "#8d8d8d"
    tl_ruler_text: str = "#b0b0b0"
    playhead: str = "#5895ec"
    sermon_clip: str = "#2f6bdf"      # 설교 구간
    bene_clip: str = "#2d9d78"        # 축도 구간 (FilmCraft render_green)
    clip_text: str = "#ffffff"
    clip_selected_border: str = "#ebebeb"
    control_well: str = "#101010"     # 오목한 바탕 (정밀 조정 바)
    control_border: str = "#505050"
    control_handle: str = "#e0e0e0"

    # --- 알림 ---
    ok: str = "#2d9d78"              # FilmCraft render_green
    warn: str = "#f0a24f"
    danger: str = "#dc513d"
    media_bg: str = "#000000"        # 영상이 나오는 자리

    # --- 크기 ---
    radius: int = 0                  # 패널은 각지게 (FilmCraft 와 같다)
    radius_sm: int = 4               # 단추·입력칸
    gap: int = 4
    font_pt: int = 10                # 기본 글자 (FilmCraft 12px ≒ 9pt 보다 크게)
    font_small_pt: int = 9
    font_big_pt: int = 11
    font_mono_pt: int = 10
    row_h: int = 26                  # 목록 한 줄 높이 (마우스로 누르기 좋게)

    @property
    def dark(self) -> bool:
        return self.name == "dark"


_DARK = Tokens()

_LIGHT = replace(
    _DARK,
    name="light",
    app_bg="#b4b4b4",
    header_bg="#d6d6d6",
    panel_bg="#e8e8e8",
    tab_bg="#e8e8e8",
    tab_text="#5a5a5a",
    tab_text_active="#141414",
    text="#222222",
    text_dim="#5a5a5a",
    text_faint="#8c8c8c",
    hover="#d2d2d2",
    pressed="#c4c4c4",
    field_bg="#fafafa",
    field_border="#aaaaaa",
    separator="#c8c8c8",
    row_alt="#e0e0e0",
    row_selected="#aac8f0",
    pill_active_bg="#c4c4c4",
    button_bg="#dcdcdc",
    button_border="#aaaaaa",
    button_hover="#d2d2d2",
    disabled_text="#969aa0",
    disabled_bg="#d8d8d8",
    tl_bg="#d2d2d2",
    tl_ruler_tick="#8c8c8c",
    tl_ruler_text="#505050",
    control_well="#c4c4c4",
    control_border="#969696",
    control_handle="#323232",
    hot_text="#1c4e8a",
    timecode="#1c4e8a",
    ok="#1f5e56",
    warn="#a8763e",
    danger="#9e4b3c",
)

# 큰 글자: 색은 밝은 화면과 같고 크기만 키운다. 줄 높이도 같이 키워야 글자가 잘리지 않는다.
_BIG = replace(_LIGHT, name="big", font_pt=12, font_small_pt=11, font_big_pt=14,
               font_mono_pt=12, row_h=32)

_ALL = {"dark": _DARK, "light": _LIGHT, "big": _BIG}


def tokens(name: str = "dark") -> Tokens:
    return _ALL.get(name, _DARK)


# 지금 쓰고 있는 테마. 직접 그리는 위젯(머리띠·패널 테두리·구간 막대)은
# 그릴 때마다 current() 를 읽는다. 그래야 [화면 모양] 을 바꾸면 다시 켜지
# 않아도 같이 바뀐다 — 만들 때 색을 한 번만 받아 두면 그대로 남는다.
_current = _DARK


def current() -> Tokens:
    return _current


def use(name: str) -> Tokens:
    """테마를 바꾸고 그 토큰을 돌려준다. app.apply_theme 이 부른다."""
    global _current
    _current = tokens(name)
    return _current


def stylesheet(t: Tokens) -> str:
    """프로그램 전체에 입히는 Qt 스타일시트.

    FilmCraft 의 모양새를 따른다 — 패널은 각지고(radius 0), 단추와 입력칸만 살짝
    둥글고(4), 지금 고른 줄은 강조색으로 채운다. 글자 크기는 토큰에서 온다.
    """
    return f"""
    QWidget {{
        background: {t.panel_bg};
        color: {t.text};
        font-size: {t.font_pt}pt;
    }}
    QWidget:disabled {{ color: {t.disabled_text}; }}
    QLabel {{ background: transparent; }}

    /* 패널 묶음 — FilmCraft 처럼 각지게, 제목은 흐리게 */
    QGroupBox {{
        background: {t.panel_bg};
        border: 1px solid {t.separator};
        border-radius: {t.radius}px;
        margin-top: 10px;
        padding-top: 8px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {t.text_dim};
        background: transparent;
    }}

    QPushButton {{
        background: {t.button_bg};
        color: {t.text};
        border: 1px solid {t.button_border};
        border-radius: {t.radius_sm}px;
        padding: 5px 12px;
    }}
    QPushButton:hover {{ background: {t.hover}; }}
    QPushButton:pressed {{ background: {t.pressed}; }}
    QPushButton:disabled {{
        background: {t.disabled_bg};
        color: {t.disabled_text};
        border-color: {t.separator};
    }}
    /* 주요 단추 (영상 만들기 등) — 강조색으로 채운다 */
    QPushButton[primary="true"] {{
        background: {t.accent};
        color: {t.text_on_accent};
        border: 1px solid {t.accent};
        font-weight: 600;
    }}
    QPushButton[primary="true"]:hover {{
        background: {t.accent_hover}; border-color: {t.accent_hover};
    }}
    QPushButton[primary="true"]:pressed {{
        background: {t.accent_pressed}; border-color: {t.accent_pressed};
    }}
    QPushButton[primary="true"]:disabled {{
        background: {t.disabled_bg}; color: {t.disabled_text}; border-color: {t.separator};
    }}
    /* 글자만 있는 단추 ([고급 설정] 처럼 펼치는 것) */
    QPushButton[flat="true"] {{
        background: transparent;
        border: none;
        color: {t.text_dim};
        text-align: left;
        padding: 2px 2px;
        font-size: {t.font_small_pt}pt;
    }}
    QPushButton[flat="true"]:hover {{ color: {t.text}; }}

    QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
        background: {t.field_bg};
        color: {t.text};
        border: 1px solid {t.field_border};
        border-radius: {t.radius_sm}px;
        padding: 4px 6px;
        selection-background-color: {t.accent};
        selection-color: {t.text_on_accent};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {t.focus}; }}

    QComboBox {{
        background: {t.field_bg};
        color: {t.text};
        border: 1px solid {t.field_border};
        border-radius: {t.radius_sm}px;
        padding: 4px 8px;
    }}
    QComboBox:hover {{ border-color: {t.control_border}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox QAbstractItemView {{
        background: {t.field_bg};
        color: {t.text};
        border: 1px solid {t.field_border};
        selection-background-color: {t.accent};
        selection-color: {t.text_on_accent};
        outline: none;
    }}

    QListWidget, QTreeWidget, QTableWidget {{
        background: {t.field_bg};
        color: {t.text};
        border: 1px solid {t.field_border};
        border-radius: {t.radius_sm}px;
        alternate-background-color: {t.row_alt};
        outline: none;
    }}
    QListWidget::item {{ padding: 3px 6px; min-height: {t.row_h - 10}px; }}
    QListWidget::item:hover {{ background: {t.hover}; }}
    QListWidget::item:selected {{
        background: {t.row_selected};
        color: {t.text};
    }}

    QCheckBox, QRadioButton {{ background: transparent; spacing: 6px; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
    QCheckBox::indicator {{
        background: {t.field_bg};
        border: 1px solid {t.control_border};
        border-radius: 3px;
    }}
    QCheckBox::indicator:checked {{
        background: {t.accent};
        border-color: {t.accent};
        image: none;
    }}
    QCheckBox::indicator:disabled {{ background: {t.disabled_bg}; border-color: {t.separator}; }}

    QTabWidget::pane {{
        background: {t.panel_bg};
        border: 1px solid {t.separator};
        border-radius: {t.radius}px;
        top: -1px;
    }}
    QTabBar {{ background: {t.tab_bg}; }}
    /* 탭 띠 — FilmCraft 의 패널 탭 띠(높이 32)와 같은 두께로 */
    QTabBar::tab {{
        background: {t.tab_bg};
        color: {t.tab_text};
        border: none;
        border-bottom: 2px solid transparent;
        padding: 9px 16px;
        margin-right: 2px;
    }}
    QTabBar::tab:hover {{ color: {t.tab_text_active}; background: {t.hover}; }}
    /* 고른 탭은 아래에 강조색 밑줄 — FilmCraft 헤더의 모드 탭과 같은 방식 */
    QTabBar::tab:selected {{
        color: {t.tab_text_active};
        border-bottom: 2px solid {t.accent};
        font-weight: 600;
    }}

    QSplitter::handle {{ background: {t.app_bg}; }}
    QSplitter::handle:horizontal {{ width: {t.gap}px; }}
    QSplitter::handle:vertical {{ height: {t.gap}px; }}

    QScrollArea {{ background: {t.panel_bg}; border: none; }}
    QScrollBar:vertical {{ background: {t.panel_bg}; width: 12px; margin: 0; }}
    QScrollBar::handle:vertical {{
        background: {t.control_border}; border-radius: 4px; min-height: 28px; margin: 2px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {t.text_faint}; }}
    QScrollBar:horizontal {{ background: {t.panel_bg}; height: 12px; margin: 0; }}
    QScrollBar::handle:horizontal {{
        background: {t.control_border}; border-radius: 4px; min-width: 28px; margin: 2px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QSlider::groove:horizontal {{
        background: {t.control_well};
        border: 1px solid {t.field_border};
        height: 6px;
        border-radius: 3px;
    }}
    QSlider::sub-page:horizontal {{ background: {t.accent}; border-radius: 3px; }}
    QSlider::handle:horizontal {{
        background: {t.control_handle};
        border: 1px solid {t.control_border};
        width: 12px;
        margin: -5px 0;
        border-radius: 6px;
    }}

    QProgressBar {{
        background: {t.control_well};
        border: 1px solid {t.field_border};
        border-radius: {t.radius_sm}px;
        text-align: center;
        color: {t.text};
    }}
    QProgressBar::chunk {{ background: {t.accent}; border-radius: 3px; }}

    QToolTip {{
        background: {t.header_bg};
        color: {t.text};
        border: 1px solid {t.control_border};
        padding: 5px;
    }}
    QMessageBox {{ background: {t.panel_bg}; }}
    QMenu {{
        background: {t.header_bg};
        color: {t.text};
        border: 1px solid {t.separator};
    }}
    QMenu::item:selected {{ background: {t.accent}; color: {t.text_on_accent}; }}
    """
