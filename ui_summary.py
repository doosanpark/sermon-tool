"""편집기의 [3분 요약] 탭.

`app.py` 에서 떼어 냈다. 이 탭 하나가 삼백 줄이라 편집기 본체와 섞여 있으면
어느 쪽을 고치는지 알기 어렵다.

`MainWindow` 에 섞어 쓰는 조각(mixin)이다. 창을 따로 만들지 않고 메서드만
빌려주므로 `self.marker` · `self._run` · `self.log` 같은 본체의 것들을 그대로 쓴다.
`class MainWindow(SummaryTab, QWidget)` 순서를 지켜야 한다 — 앞에 와야 본체의
같은 이름을 덮어쓰지 않는다.

편집 방법은 타임라인 막대 끌기가 아니라 **자막을 읽으며 문장 켜고 끄기** 다.
초를 다루게 하는 순간 이 화면은 편집 프로그램이 되고, 컴퓨터에 익숙하지
않은 분들은 손을 못 댄다. 컷 자리는 프로그램이 목사님 숨 고르는 자리로
알아서 맞춘다.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal, QUrl as _QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)


class PreviewWindow(QWidget):
    """3분 요약 미리보기 창. 영상을 크게 보여 주고, 아래에 지금 자막을 큰 글씨로 띄운다.

    [3분 요약] 탭에서는 목록을 넓히느라 영상 칸이 작아진다. 자막 글자를 확인하려면
    영상과 자막을 함께 크게 봐야 해서 따로 띄운다. 원하는 자리(다른 모니터 포함)에 둔다.
    재생기는 하나만 쓴다 — 미리보기 동안 화면 출력만 이 창으로 옮겼다가 닫으면 돌려놓는다.
    """
    closed = Signal()
    edit_requested = Signal()
    stop_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("요약 미리보기")
        lay = QVBoxLayout(self)
        self.video = QVideoWidget()
        self.video.setMinimumSize(480, 270)
        t = theme_mod.tokens(_theme_name())
        self.video.setStyleSheet(f"background:{t.media_bg};")
        lay.addWidget(self.video, 1)
        self.caption = QLabel("")
        self.caption.setWordWrap(True)
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setMinimumHeight(64)
        # 미리보기 자막은 영상 위에 크게 — 글자가 틀렸는지 눈으로 바로 잡으려는 칸이다
        self.caption.setStyleSheet(
            f"background:{t.media_bg}; color:#ffffff;"
            f"font-size:{t.font_pt + 6}pt; font-weight:600; padding:8px;")
        lay.addWidget(self.caption)
        row = QHBoxLayout()
        self.where = QLabel("")
        self.where.setStyleSheet(f"color:{t.text_dim};")
        row.addWidget(self.where, 1)
        self.btn_edit = QPushButton("이 자막 고치기")
        self.btn_edit.setToolTip("잠시 멈추고 지금 나온 자막의 글자를 고칩니다.")
        self.btn_stop = QPushButton("■ 미리보기 멈추기")
        for b in (self.btn_edit, self.btn_stop):
            b.setMinimumHeight(34)
            row.addWidget(b)
        lay.addLayout(row)
        self.btn_edit.clicked.connect(self.edit_requested)
        self.btn_stop.clicked.connect(self.stop_requested)
        self.resize(760, 560)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 이름)
        self.closed.emit()
        super().closeEvent(event)

import theme as theme_mod
from core import blocks as blk
from core import embed as embed_mod
from core import export as export_mod
from core import highlight as hl_mod
from core import llm as llm_mod
from core import pauses as pause_mod
from core.util import ToolError, hms


def _theme_name() -> str:
    """지금 쓰는 화면 모양. app.py 를 들여오지 않는다 — 서로 들여오면 순환이 된다."""
    from PySide6.QtCore import QSettings
    name = str(QSettings("창의진교회", "SermonTool").value("theme", "dark"))
    return name if name in theme_mod.THEMES else "dark"


class SummaryTab:
    """[3분 요약] 탭의 화면과 동작. MainWindow 에 섞어 쓴다."""

    PICK_CHOICES = {
        "규칙만 (빠름)": "rule",
        "뜻까지 보기 (권장)": "embed",
        "뜻 + AI 로 고르기 (가장 잘 고름)": "llm",
    }

    # ================= 3분 요약 =================
    # 편집 방법은 타임라인 막대 끌기가 아니라 **자막을 읽으며 문장 켜고 끄기** 다.
    # 초를 다루게 하는 순간 이 화면은 편집 프로그램이 되고, 컴퓨터에 익숙하지
    # 않은 분들은 손을 못 댄다. 컷 자리는 프로그램이 목사님 숨 고르는 자리로
    # 알아서 맞춘다.

    def _build_summary_tab(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(6)

        top = QHBoxLayout()
        self.sum_meter = QLabel("설교 구간을 찾고 자막을 만든 뒤 [요약 골라줘] 를 누르세요")
        t = theme_mod.tokens(_theme_name())
        self.sum_meter.setStyleSheet(
            f"font-size:{t.font_big_pt}pt; font-weight:600; color:{t.text};")
        top.addWidget(self.sum_meter, 1)
        self.sum_bar = QProgressBar()
        self.sum_bar.setRange(0, 130)
        self.sum_bar.setValue(0)
        self.sum_bar.setFixedWidth(170)
        self.sum_bar.setTextVisible(False)
        self.sum_bar.setToolTip("가운데 눈금이 목표 길이입니다")
        top.addWidget(self.sum_bar)
        lay.addLayout(top)

        # 형식의 칸들 (세 덩어리면 문제 제기 · 핵심 · 적용). 칸마다 [다른 후보] 로
        # 바꿔 가며 고른다. 초를 다루지 않고 '이 대목 / 저 대목' 만 고르게 하려는 것이다.
        self.sum_slot_box = QWidget()
        self.sum_slot_lay = QVBoxLayout(self.sum_slot_box)
        self.sum_slot_lay.setContentsMargins(0, 0, 0, 0)
        self.sum_slot_lay.setSpacing(2)
        self.sum_slot_box.setVisible(False)
        lay.addWidget(self.sum_slot_box)

        self.sum_list = QListWidget()
        self.sum_list.setToolTip(
            "네모를 누르면 그 문장을 요약에 넣고 뺍니다.\n"
            "글을 한 번 누르면 그 자리로 이동하고, 두 번 누르면 틀린 글자를 고칠 수 있습니다.\n"
            "고친 글자는 자막 파일과 영상에 넣는 자막에 그대로 들어갑니다.")
        self.sum_list.setEditTriggers(QListWidget.DoubleClicked | QListWidget.EditKeyPressed)
        self.sum_list.itemChanged.connect(self._on_summary_toggled)
        self.sum_list.itemClicked.connect(
            lambda it: self._seek(float(it.data(Qt.UserRole))))
        self.sum_list.setMinimumHeight(80)       # 문장 서너 줄은 늘 보이게
        lay.addWidget(self.sum_list, 1)

        # 확인하고 고치기 — 미리보기로 이어서 들으며 지금 나오는 자막을 보고, 틀리면 바로 고친다
        check = QHBoxLayout()
        self.btn_sum_preview = QPushButton("▶ 요약 미리보기")
        self.btn_sum_preview.setToolTip(
            "고른 대목들을 실제 영상처럼 차례로 이어서 틀어 줍니다.\n"
            "지금 나오는 자막이 아래 목록에서 파랗게 표시되니, 틀린 글자가 있으면 바로 고치세요.")
        self.btn_sum_preview.clicked.connect(self.on_summary_preview)
        self.btn_sum_hear = QPushButton("이 문장 듣기")
        self.btn_sum_hear.setToolTip("목록에서 고른 문장 하나만 들려줍니다.")
        self.btn_sum_hear.clicked.connect(self.on_summary_hear)
        self.btn_sum_edit = QPushButton("글자 고치기")
        self.btn_sum_edit.setToolTip("목록에서 고른 문장의 글자를 고칩니다. (문장을 두 번 눌러도 됩니다)")
        self.btn_sum_edit.clicked.connect(self.on_summary_edit)
        for b in (self.btn_sum_preview, self.btn_sum_hear, self.btn_sum_edit):
            b.setMinimumHeight(28)
            check.addWidget(b)
        self.sum_now = QLabel("")
        self.sum_now.setStyleSheet(
            f"color:{t.hot_text}; font-size:{t.font_big_pt}pt; font-weight:600;")
        self.sum_now.setMinimumWidth(10)
        check.addWidget(self.sum_now, 1)
        lay.addLayout(check)

        how = QHBoxLayout()
        how.addWidget(QLabel("형식"))
        self.sum_format_box = QComboBox()
        self.sum_format_box.addItems(list(blk.FORMATS.values()))
        self.sum_format_box.setToolTip(
            "두 덩어리   핵심 → 적용. 1분 40초 안팎. 릴스·쇼츠용 기본 형식입니다.\n"
            "세 덩어리   문제 제기 · 핵심 · 적용을 각각 30~60초씩 이어 붙입니다.\n"
            "한 대목     가장 힘 있는 한 대목을 1분 안팎으로 끊지 않고 보여 줍니다.\n"
            "문장 모음   예전 방식. 좋은 문장을 여기저기서 골라 잇습니다.\n"
            "바꾸면 바로 다시 골라 줍니다 — 회의 때 같은 설교로 견주어 보세요.")
        self.sum_format_box.currentTextChanged.connect(self._on_sum_format)
        how.addWidget(self.sum_format_box)
        how.addSpacing(10)
        how.addWidget(QLabel("고르는 방법"))
        self.pick_mode_box = QComboBox()
        self.pick_mode_box.addItems(list(self.PICK_CHOICES))
        self.pick_mode_box.setToolTip(
            "규칙만        낱말만 봅니다. 아무것도 받지 않아도 됩니다.\n"
            "뜻까지 보기   설교 전체를 대표하는 문장을 찾아냅니다. 모델 120MB.\n"
            "뜻 + AI      AI 가 설교를 5분씩 나눠 읽고 대목의 역할을 매깁니다.\n"
            "             모델 2GB, 이 PC 에서 10분 안팎 걸릴 수 있습니다.")
        self.pick_mode_box.currentTextChanged.connect(self._on_pick_mode)
        how.addWidget(self.pick_mode_box)
        self.ai_status = QLabel("")
        self.ai_status.setStyleSheet(f"color:{t.text_dim};")
        how.addWidget(self.ai_status, 1)
        self.btn_ai_get = QPushButton("모델 받기")
        self.btn_ai_get.setToolTip("이 방법에 필요한 모델을 내려받습니다. 한 번만 받으면 됩니다.")
        self.btn_ai_get.clicked.connect(self.on_get_models)
        how.addWidget(self.btn_ai_get)
        lay.addLayout(how)

        row = QHBoxLayout()
        self.btn_sum_pick = QPushButton("요약 골라줘")
        self.btn_sum_pick.setToolTip("프로그램이 처음부터 다시 고릅니다. 손댄 것은 사라집니다.")
        self.btn_sum_undo = QPushButton("되돌리기")
        self.btn_sum_make = QPushButton("요약 영상 만들기")
        self.btn_sum_make.setProperty("primary", True)
        self.btn_sum_mp3 = QPushButton("요약 mp3")
        self.btn_sum_mp3.setToolTip(
            "영상과 똑같은 구간을 소리만 뽑습니다.\n"
            "화면을 안 건드려서 몇 초면 끝나고, 파일이 작아 주고받기 좋습니다.")
        self.btn_sum_pick.clicked.connect(self.on_summary_pick)
        self.btn_sum_undo.clicked.connect(self.on_summary_undo)
        self.btn_sum_make.clicked.connect(self.on_summary_export)
        self.btn_sum_mp3.clicked.connect(self.on_summary_mp3)
        for b in (self.btn_sum_pick, self.btn_sum_undo,
                  self.btn_sum_make, self.btn_sum_mp3):
            b.setMinimumHeight(30)               # 좁은 화면에서도 글자가 잘리지 않게
            row.addWidget(b)
        row.addStretch(1)
        self.sum_hint = QLabel("")
        self.sum_hint.setStyleSheet(
            f"color:{t.text_faint}; font-size:{t.font_small_pt}pt;")
        row.addWidget(self.sum_hint)
        lay.addLayout(row)

        # 그래도 칸이 모자라면 잘라 먹지 말고 스크롤로 보여 준다 (노트북 화면).
        scroll = QScrollArea()
        scroll.setWidget(box)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return scroll

    # -------- 고르는 방법 --------
    def _pick_mode(self) -> str:
        if not hasattr(self, "pick_mode_box"):
            return "embed"
        return self.PICK_CHOICES.get(self.pick_mode_box.currentText(), "embed")

    def _missing_for(self, mode: str) -> list[str]:
        """그 방법을 쓰려면 아직 무엇이 없는지."""
        cfg = self._config()
        out: list[str] = []
        if mode in ("embed", "llm") and not embed_mod.is_ready(cfg):
            out.append("뜻 보기 모델 (120MB)")
        if mode == "llm":
            if not llm_mod.find_server():
                out.append("llama-server 실행파일")
            elif not llm_mod.model_path(cfg).exists():
                out.append(f"AI 모델 ({cfg.llm_model_file}, 약 2GB)")
        return out

    def _on_pick_mode(self, *_a) -> None:
        self._save_prefs()
        self._update_ai_status()

    def _update_ai_status(self) -> None:
        if not hasattr(self, "ai_status"):
            return
        mode = self._pick_mode()
        missing = self._missing_for(mode)
        can_get = any("실행파일" not in m for m in missing)
        # 받을 것이 없으면 단추를 아예 숨긴다. 회색으로 꺼진 채 남아 있으면
        # "모델이 없나?" 하고 헷갈린다 — 실제로 그런 물음을 받았다 (2026-10-20).
        self.btn_ai_get.setVisible(bool(missing) and can_get)
        self.btn_ai_get.setEnabled(bool(missing) and can_get)
        if not missing:
            tuned = mode == "llm" and llm_mod.is_tuned(self._config())
            self.ai_status.setText("준비됐습니다 — 이 교회 설교로 학습한 AI 를 씁니다" if tuned
                                   else "준비됐습니다")
            self.ai_status.setStyleSheet(f"color:{theme_mod.tokens(_theme_name()).ok};")
        elif not can_get:
            self.ai_status.setText("llama-server 실행파일이 없습니다 — bin\\llama\\ 를 확인하세요")
            self.ai_status.setStyleSheet(f"color:{theme_mod.tokens(_theme_name()).danger};")
        else:
            self.ai_status.setText("아직 없음: " + ", ".join(missing)
                                   + "  →  [모델 받기]")
            self.ai_status.setStyleSheet(f"color:{theme_mod.tokens(_theme_name()).warn};")

    def on_get_models(self) -> None:
        """지금 고른 방법에 필요한 모델을 받는다. 한 번만 받으면 된다."""
        mode = self._pick_mode()
        missing = self._missing_for(mode)
        if not missing:
            QMessageBox.information(self, "이미 준비됐습니다",
                                    "받을 것이 없습니다. 바로 [요약 골라줘] 를 누르세요.")
            return
        ok = QMessageBox.question(
            self, "모델을 받을까요?",
            "다음을 내려받습니다. 한 번만 받으면 그 뒤로는 인터넷 없이 씁니다.\n\n  "
            + "\n  ".join(missing)
            + "\n\n계속할까요?")
        if ok != QMessageBox.Yes:
            return

        cfg = self._config()
        want_llm = (mode == "llm")

        def job():
            got = []
            if not embed_mod.is_ready(cfg):
                embed_mod.download(cfg, progress=lambda t: print(f"  {t}"))
                got.append("뜻 보기 모델")
            if want_llm and not llm_mod.model_path(cfg).exists():
                llm_mod.download(cfg, progress=lambda t: print(f"  {t}"))
                got.append("AI 모델")
            return got

        def done(got):
            self._update_ai_status()
            QMessageBox.information(
                self, "다 받았습니다",
                ("받은 것: " + ", ".join(got) if got else "받을 것이 없었습니다")
                + "\n\n[요약 골라줘] 를 누르면 이 방법으로 고릅니다.")

        self._run(job, done,
                  title="모델 받기",
                  detail=" · ".join(missing),
                  done_message="모델을 받았습니다",
                  next_step="[요약] 탭에서 [요약 골라줘] 를 누르세요")

    # -------- 형식 · 칸 --------
    def _sum_format(self) -> str:
        if not hasattr(self, "sum_format_box"):
            return "two"
        text = self.sum_format_box.currentText()
        for key, label in blk.FORMATS.items():
            if label == text:
                return key
        return "two"

    def _reset_summary_blocks(self) -> None:
        self.sum_blocks = []
        self.sum_slots = []
        self.sum_initial = []
        self.sum_theme = ""
        if hasattr(self, "sum_slot_box"):
            self._fill_slots()

    def _ensure_blocks(self) -> None:
        """덩어리가 없으면(저장해 둔 요약을 불러온 경우 등) 규칙으로 바로 만든다. 1초 안."""
        if getattr(self, "sum_blocks", None) or not self.sum_marks:
            return
        blk.mark_exits(self.sum_marks)
        self.sum_blocks = blk.segment(self.sum_marks, self.sum_pauses)
        blk.label_rules(self.sum_blocks, self.sum_marks)

    def _on_sum_format(self, *_a) -> None:
        """형식을 바꾸면 이미 읽어 둔 것으로 바로 다시 고른다. AI 를 다시 돌리지 않는다."""
        self._save_prefs()
        if not self.sum_marks:
            return
        fmt = self._sum_format()
        target = self._summary_target()
        snap = self._summary_snap()
        self._summary_snapshot()
        if fmt == "sentences":
            hl_mod.auto_pick(self.sum_marks, self.sum_outline, target, snap=snap)
            self.sum_slots = []
        else:
            self._ensure_blocks()
            self.sum_slots = blk.assemble(fmt, self.sum_blocks, target)
            blk.apply_slots(self.sum_slots, self.sum_blocks, self.sum_marks)
        self.sum_initial = [m.chosen for m in self.sum_marks]
        self._save_summary_picks()
        self._fill_summary_list()
        self._fill_slots()
        self.log(f"요약 형식을 '{blk.format_name(fmt)}' 로 바꿔 다시 골랐습니다 — {self.sum_meter.text()}")

    def _fill_slots(self) -> None:
        """칸 목록을 다시 그린다. 칸마다 [다른 후보] · [들어보기]."""
        lay = self.sum_slot_lay
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()                  # 지워지기 전까지 옛 줄이 겹쳐 보이지 않게
                w.setParent(None)
                w.deleteLater()
        slots = getattr(self, "sum_slots", []) or []
        self.sum_slot_box.setVisible(bool(slots))
        if not slots:
            return
        theme = getattr(self, "sum_theme", "")
        if theme:
            t = QLabel(f"AI 가 본 설교 주제: {theme}")
            tk = theme_mod.tokens(_theme_name())
            t.setStyleSheet(f"color:{tk.ok}; font-size:{tk.font_pt}pt;")
            t.setToolTip("설교 제목이나 화면 위 한 줄을 정할 때 참고하세요. 영상에 자동으로 들어가지는 않습니다.")
            lay.addWidget(t)
        by_index = {b.index: b for b in getattr(self, "sum_blocks", [])}
        for k, slot in enumerate(slots):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            c = slot.chosen
            if c:
                text = (f"<b>{slot.name}</b>  {hms(c.start)} · {int(c.end - c.start)}초  "
                        f"<span style='color:{theme_mod.tokens(_theme_name()).text_faint}'>"
                        f"(후보 {slot.cur + 1}/{len(slot.cands)})</span>  "
                        f"{c.preview}…")
                full = " ".join(by_index[i].text for i in range(c.first, c.last + 1) if i in by_index)
            else:
                text, full = f"<b>{slot.name}</b>  맞는 대목을 찾지 못했습니다", ""
            label = QLabel(text)
            label.setTextFormat(Qt.RichText)
            label.setToolTip(full[:600])
            label.setMinimumWidth(10)
            h.addWidget(label, 1)
            nxt = QPushButton("다른 후보 ▶")
            nxt.setToolTip("이 칸을 다른 대목으로 바꿉니다. 다른 칸과 겹치는 대목은 건너뜁니다.")
            nxt.setEnabled(len(slot.cands) > 1)
            nxt.clicked.connect(lambda _=False, i=k: self._on_slot_next(i))
            play = QPushButton("들어보기")
            play.setToolTip("이 칸에 들어간 대목을 처음부터 끝까지 들려줍니다.")
            play.setEnabled(c is not None)
            play.clicked.connect(lambda _=False, i=k: self._on_slot_play(i))
            for b in (nxt, play):
                b.setFixedHeight(26)
                h.addWidget(b)
            lay.addWidget(row)

    def _on_slot_next(self, k: int) -> None:
        if not (0 <= k < len(self.sum_slots)):
            return
        self._summary_snapshot()
        if not blk.next_cand(self.sum_slots, k):
            self.sum_undo.pop()
            QMessageBox.information(self, "다른 후보가 없습니다",
                                    "이 칸에 넣을 만한 다른 대목을 찾지 못했습니다.")
            return
        blk.apply_slots(self.sum_slots, self.sum_blocks, self.sum_marks)
        self._save_summary_picks()
        self._fill_summary_list()
        self._fill_slots()

    def _on_slot_play(self, k: int) -> None:
        if 0 <= k < len(self.sum_slots) and self.sum_slots[k].chosen:
            c = self.sum_slots[k].chosen
            self._play_span(c.start, c.end + 0.4)

    def _save_summary_label(self) -> None:
        """영상·mp3 를 만드는 순간의 선택을 학습 자료로 남긴다. 실패해도 영상은 만든다."""
        try:
            self._ensure_blocks()
            path = blk.save_label(self._config().work_dir, self.marker, self.sum_blocks,
                                  self.sum_marks, self._sum_format(), self._summary_target(),
                                  getattr(self, "sum_initial", None), self.sum_slots,
                                  getattr(self, "sum_theme", ""))
            self.log(f"이번 요약을 학습 자료로 남겼습니다: labels\\{path.name}")
        except Exception as exc:                      # 자료 저장 실패가 영상 만들기를 막으면 안 된다
            self.log(f"학습 자료를 남기지 못했습니다 ({type(exc).__name__}) — 영상 만들기는 그대로 합니다")

    # -------- 상태 --------
    def _summary_snap(self):
        """구간을 쉼에 맞춰 주는 함수. 쉼은 한 번만 찾아 두고 계속 쓴다."""
        return pause_mod.make_snap(self.sum_pauses) if self.sum_pauses else None

    def _summary_target(self) -> float:
        base = float(getattr(self._config(), "summary_target", 180.0))
        fmt = self._sum_format()
        if fmt == "one":
            return min(base, 75.0)        # 한 대목은 1분 안팎이 목표다
        if fmt == "two":
            return min(base, 100.0)       # 핵심 → 적용, 1분 40초 안팎 (작가 검토 2026-10-19)
        if fmt == "three":
            # 유튜브 쇼츠는 3분까지다. 딱 3분을 목표로 잡으면 구간을 쉼에 맞추며
            # 앞뒤로 벌어질 때 넘길 수 있고, 1초만 넘겨도 쇼츠가 아니라 일반
            # 영상으로 올라간다. 10초를 여유로 둔다 (2026-10-20 사용자 결정).
            return min(base, 170.0)
        return base

    def _summary_snapshot(self) -> None:
        self.sum_undo.append([m.chosen for m in self.sum_marks])
        del self.sum_undo[:-30]

    def _fill_summary_list(self, keep_preview: bool = False) -> None:
        """문장 목록을 다시 그린다. 색이 곧 갈래다."""
        if not keep_preview:
            self._stop_preview()           # 고른 것이 바뀌면 미리보기 순서도 달라진다
        self.sum_list.blockSignals(True)
        self.sum_list.clear()
        for m in self.sum_marks:
            item = QListWidgetItem(f"[{hms(m.start)}]  {m.text}")
            item.setData(Qt.UserRole, m.start)
            item.setData(Qt.UserRole + 1, m.index)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
            item.setCheckState(Qt.Checked if m.chosen else Qt.Unchecked)
            if m.kind == "point":
                f = item.font()
                f.setBold(True)
                item.setFont(f)
                item.setToolTip(m.why or "목사님이 번호로 선언하신 대목입니다")
            elif m.kind in ("reading", "aside"):
                item.setForeground(QColor(theme_mod.tokens(_theme_name()).text_faint))
                item.setToolTip("성경을 읽는 부분입니다 — 요약에는 잘 안 맞습니다")
            elif m.why:
                item.setToolTip(f"고른 이유: {m.why}")
            self._paint_summary_item(item, m)
            self.sum_list.addItem(item)
        self.sum_list.blockSignals(False)
        self._update_summary_meter()
        # 고른 문장이 보이도록 첫 번째 고른 문장으로 내려간다
        for row, m in enumerate(self.sum_marks):
            if m.chosen:
                self.sum_list.scrollToItem(self.sum_list.item(row),
                                           QListWidget.PositionAtTop)
                break

    def _paint_summary_item(self, item: QListWidgetItem, m) -> None:
        """고른 문장에 색을 깔아 한눈에 보이게. 번호로 선언하신 대목은 다른 색."""
        if m.chosen:
            t = theme_mod.tokens(_theme_name())
            base = QColor(t.warn if m.kind == "point" else t.accent)
            # 글자가 읽혀야 하므로 바탕색은 옅게 깐다 (어두운 화면에서는 더 옅게)
            base.setAlpha(60 if t.dark else 45)
            item.setBackground(base)
        else:
            item.setBackground(QColor(0, 0, 0, 0))

    def _update_summary_meter(self) -> None:
        if not self.sum_marks:
            self.sum_meter.setText("설교 구간을 찾고 자막을 만든 뒤 [요약 골라줘] 를 누르세요")
            self.sum_bar.setValue(0)
            self.sum_hint.setText("")
            return
        target = self._summary_target()
        snap = self._summary_snap()
        total = hl_mod.total_seconds(self.sum_marks, snap)
        self.sum_meter.setText(
            hl_mod.summarize(self.sum_marks, self.sum_outline, target, snap))
        self.sum_bar.setValue(int(min(130, total / max(target, 1.0) * 100)))
        pieces = len(hl_mod.runs(self.sum_marks, snap))
        self.sum_hint.setText(
            f"이어붙일 조각 {pieces}개" + ("" if self.sum_pauses else "  (쉼 정보 없음)"))

    # -------- 조작 --------
    def _on_summary_toggled(self, item: QListWidgetItem) -> None:
        """네모를 눌렀거나(넣고 빼기) 글자를 고쳤을 때. 둘 다 같은 신호로 온다."""
        idx = item.data(Qt.UserRole + 1)
        if idx is None or not (0 <= idx < len(self.sum_marks)):
            return
        m = self.sum_marks[idx]
        text = _strip_time(item.text())
        if text and text != m.text:
            self._apply_text_edit(m, text, item)
            return
        if not text:                                   # 다 지웠으면 원래 글로 되돌린다
            self._set_item_text(item, m)
            return
        want = item.checkState() == Qt.Checked
        if want == m.chosen:
            return
        self._stop_preview()
        self._summary_snapshot()
        m.chosen = want
        self.sum_list.blockSignals(True)
        self._paint_summary_item(item, m)
        self.sum_list.blockSignals(False)
        self._save_summary_picks()
        self._update_summary_meter()

    def _on_summary_play(self, item: QListWidgetItem) -> None:
        idx = item.data(Qt.UserRole + 1)
        if idx is None or not (0 <= idx < len(self.sum_marks)):
            return
        m = self.sum_marks[idx]
        self._play_span(m.start, m.end + 0.4)

    # -------- 자막 확인 · 고치기 --------
    def _set_item_text(self, item: QListWidgetItem, m) -> None:
        self.sum_list.blockSignals(True)
        item.setText(f"[{hms(m.start)}]  {m.text}")
        self.sum_list.blockSignals(False)

    def _selected_mark(self):
        row = self.sum_list.currentRow()
        if not self.sum_marks or not (0 <= row < len(self.sum_marks)):
            QMessageBox.information(self, "문장을 먼저 고르세요",
                                    "아래 목록에서 문장을 한 번 눌러 고른 뒤 다시 눌러 주세요.")
            return None, None
        return self.sum_marks[row], self.sum_list.item(row)

    def on_summary_hear(self) -> None:
        m, _item = self._selected_mark()
        if m is not None:
            self._stop_preview()
            self._play_span(m.start, m.end + 0.4)

    def on_summary_edit(self) -> None:
        m, item = self._selected_mark()
        if m is not None:
            self.sum_list.editItem(item)

    def _apply_text_edit(self, m, text: str, item: QListWidgetItem) -> None:
        """요약 목록에서 고친 글자를 자막 전체에 반영하고 저장한다.

        자막 파일(cache/영상.transcript.json)이 고쳐지므로 [자막] 탭, 설교 영상 자막,
        3분 요약 영상에 태우는 자막에 모두 그대로 들어간다.
        """
        text = " ".join(text.split())
        old = m.text
        m.text = text
        self._set_item_text(item, m)
        sub_idx = self._update_transcript_text(m.start, text)
        self._save_transcript_now()
        self.log(f"자막 고침  [{hms(m.start)}]  {old}  →  {text}")
        if sub_idx is None:
            self.log("  (자막 목록에서 같은 문장을 찾지 못해 요약 목록에만 고쳤습니다)")
        self._offer_rule(old, text)

    def _update_transcript_text(self, start: float, text: str) -> int | None:
        for i, seg in enumerate(self.transcript):
            if abs(float(seg["start"]) - start) < 0.05:
                seg["text"] = text
                if hasattr(self, "subtitle_list") and i < self.subtitle_list.count():
                    it = self.subtitle_list.item(i)
                    self.subtitle_list.blockSignals(True)
                    it.setText(f"[{hms(seg['start'])}] {text}")
                    self.subtitle_list.blockSignals(False)
                return i
        return None

    def _save_transcript_now(self) -> None:
        if not self.marker:
            return
        try:
            from core import transcribe
            transcribe.save_transcript(self._config().transcript_path(self.marker.video_id),
                                       self.transcript)
        except OSError as exc:
            self.log(f"자막 파일을 저장하지 못했습니다: {exc}")

    def _offer_rule(self, old: str, new: str) -> None:
        """한 낱말만 고쳤으면, 같은 말을 이 설교 전체에서 · 다음 설교에서도 고칠지 묻는다."""
        pair = word_change(old, new)
        if not pair:
            return
        wrong, right = pair
        others = sum(seg["text"].count(wrong) for seg in self.transcript)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("같은 말도 고칠까요?")
        more = (f"이 설교 자막에 '{wrong}' 이(가) {others}번 더 있습니다.\n" if others
                else "")
        box.setText(f"'{wrong}'  →  '{right}'  로 고치셨습니다.\n\n{more}"
                    "앞으로 자막을 만들 때도 늘 이렇게 고칠까요?")
        everywhere = box.addButton(
            "모두 고치고 다음부터도 고치기" if others else "다음부터도 고치기", QMessageBox.YesRole)
        box.addButton("이 문장만", QMessageBox.NoRole)
        box.setDefaultButton(everywhere)
        box.exec()
        if box.clickedButton() is not everywhere:
            return
        from core import fixwords
        path = fixwords.add_user_rule(self._config().work_dir, wrong, right)
        n = 0
        if others:
            for seg in self.transcript:
                if wrong in seg["text"]:
                    seg["text"] = seg["text"].replace(wrong, right)
                    n += 1
            for mk in self.sum_marks:
                mk.text = mk.text.replace(wrong, right)
            self._save_transcript_now()
            self._refill_subtitles_text()
            self._fill_summary_list(keep_preview=True)   # 글자만 바뀌었다 — 미리보기는 계속
        self.log(f"  '{wrong}' → '{right}'  사전에 넣었습니다 ({path.name})"
                 + (f" · 이 설교에서 {n}문장 더 고쳤습니다" if n else ""))

    def _refill_subtitles_text(self) -> None:
        if not hasattr(self, "subtitle_list"):
            return
        self.subtitle_list.blockSignals(True)
        for i, seg in enumerate(self.transcript):
            if i < self.subtitle_list.count():
                self.subtitle_list.item(i).setText(f"[{hms(seg['start'])}] {seg['text']}")
        self.subtitle_list.blockSignals(False)

    # -------- 미리보기 --------
    def _preview_window(self) -> PreviewWindow:
        if getattr(self, "_pv_win", None) is None:
            self._pv_win = PreviewWindow(self)
            self._pv_win.closed.connect(self._stop_preview)
            self._pv_win.stop_requested.connect(self.on_summary_preview)
            self._pv_win.edit_requested.connect(self._preview_edit)
        return self._pv_win

    def on_summary_preview(self) -> None:
        """고른 대목을 실제 영상 순서대로 이어서 튼다. 따로 뜬 창에 영상과 지금 자막을 크게."""
        if getattr(self, "_preview", None):
            self.player.pause()
            self.btn_play.setText("▶ 재생")
            self._stop_preview()
            return
        if not self.sum_marks or not any(m.chosen for m in self.sum_marks):
            QMessageBox.information(self, "고른 대목이 없습니다",
                                    "[요약 골라줘] 를 먼저 눌러 주세요.")
            return
        if not self.media_path or not Path(self.media_path).exists():
            QMessageBox.information(self, "영상이 필요합니다",
                                    "[영상 가져오기] 나 [내 컴퓨터에서 열기] 로 영상을 먼저 여세요.")
            return
        runs = hl_mod.runs(self.sum_marks, self._summary_snap())
        if not runs:
            return
        import time as _t
        win = self._preview_window()
        win.caption.setText("")
        win.where.setText(f"조각 1/{len(runs)}")
        win.show()
        win.raise_()
        self.player.setVideoOutput(win.video)        # 화면만 미리보기 창으로 옮긴다
        self._preview = {"runs": runs, "i": 0, "guard": _t.monotonic() + 1.2, "row": -1}
        self.btn_sum_preview.setText("■ 미리보기 멈추기")
        self.sum_now.setText(f"미리보기 중 — 조각 1/{len(runs)}")
        self.stop_at = None
        self._seek(runs[0][0])
        self.player.play()
        self.btn_play.setText("❚❚ 일시정지")

    def _stop_preview(self) -> None:
        if not getattr(self, "_preview", None):
            return
        self._preview = None
        self.player.setVideoOutput(self.video)        # 화면을 편집기로 돌려놓는다
        if hasattr(self, "btn_sum_preview"):
            self.btn_sum_preview.setText("▶ 요약 미리보기")
            self.sum_now.setText("")
        win = getattr(self, "_pv_win", None)
        if win is not None and win.isVisible():
            win.hide()

    def _preview_edit(self) -> None:
        """미리보기 창의 [이 자막 고치기]. 멈추고, 고치고, 이어서 튼다."""
        pv = getattr(self, "_preview", None)
        row = pv["row"] if pv else -1
        if not (0 <= row < len(self.sum_marks)):
            QMessageBox.information(self._pv_win, "아직 자막이 없습니다",
                                    "자막이 나온 뒤에 눌러 주세요.")
            return
        self.player.pause()
        m = self.sum_marks[row]
        text, ok = QInputDialog.getText(self._pv_win, "자막 고치기",
                                        f"[{hms(m.start)}] 틀린 글자를 고쳐 주세요:", text=m.text)
        text = " ".join((text or "").split())
        if ok and text and text != m.text:
            self._apply_text_edit(m, text, self.sum_list.item(row))
            self._pv_win.caption.setText(m.text)
        if getattr(self, "_preview", None):
            self.player.play()

    def _preview_tick(self, seconds: float) -> None:
        """재생 위치가 바뀔 때마다 불린다. 조각 끝이면 다음 조각으로, 마지막이면 멈춘다."""
        import time as _t
        pv = self._preview
        runs = pv["runs"]
        start, end = runs[pv["i"]]
        if _t.monotonic() >= pv["guard"] and (seconds < start - 1.5 or seconds > end + 3.0):
            self._stop_preview()            # 다른 곳을 누르셨다 — 미리보기를 놓아 드린다
            return
        if seconds >= end - 0.05 and _t.monotonic() >= pv["guard"] - 1.0:
            pv["i"] += 1
            if pv["i"] >= len(runs):
                self.player.pause()
                self.btn_play.setText("▶ 재생")
                self._stop_preview()
                self.log("미리보기를 끝까지 틀었습니다")
                return
            pv["guard"] = _t.monotonic() + 1.2
            self._seek(runs[pv["i"]][0])
            self.sum_now.setText(f"미리보기 중 — 조각 {pv['i'] + 1}/{len(runs)}")
            if getattr(self, "_pv_win", None):
                self._pv_win.where.setText(f"조각 {pv['i'] + 1}/{len(runs)}")
            return
        # 지금 나오는 문장을 창에 크게, 목록에도 표시한다 (틀린 글자를 바로 찾게)
        for row, m in enumerate(self.sum_marks):
            if m.chosen and m.start <= seconds < m.end:
                if row != pv["row"]:
                    pv["row"] = row
                    self.sum_list.setCurrentRow(row)
                    self.sum_list.scrollToItem(self.sum_list.item(row),
                                               QListWidget.PositionAtCenter)
                    self.sum_now.setText(f"조각 {pv['i'] + 1}/{len(runs)} · 지금 자막: {m.text}")
                    if getattr(self, "_pv_win", None):
                        self._pv_win.caption.setText(m.text)
                        self._pv_win.where.setText(
                            f"조각 {pv['i'] + 1}/{len(runs)} · [{hms(m.start)}]")
                break

    def _save_summary_picks(self) -> None:
        if not self.marker:
            return
        self.marker.set_summary_picks([m.start for m in self.sum_marks if m.chosen])
        self.marker.summary_format = self._sum_format()

    def on_summary_undo(self) -> None:
        if not self.sum_undo:
            QMessageBox.information(self, "되돌릴 것이 없습니다",
                                    "아직 문장을 넣거나 빼지 않았습니다.")
            return
        for m, was in zip(self.sum_marks, self.sum_undo.pop()):
            m.chosen = was
        self._save_summary_picks()
        self._fill_summary_list()
        if self.sum_slots:
            # 칸 표시는 되돌린 문장과 어긋날 수 있다 — 칸을 다시 고르려면 [다른 후보]
            self.sum_hint.setText(self.sum_hint.text() + "  · 문장을 되돌렸습니다")

    def _prepare_for_summary(self, has_range: bool) -> None:
        """요약에 필요한 사전 작업(설교 구간 · 자막)이 없으면 물어보고 대신 해 준다.

        끝나면 [요약 골라줘] 를 이어서 부른다. 자막 만들기는 CPU 에서 20~40분이라
        말없이 시작하지 않고 한 번 묻는다.
        """
        if hasattr(self, "stt_check") and not self.stt_check.isEnabled():
            QMessageBox.information(
                self, "자막 기능이 없습니다",
                "요약은 설교 자막을 읽어서 고르는데, 이 설치본에는 자막 엔진\n"
                "(faster-whisper) 이 없습니다. run.bat 으로 다시 실행해 주세요.")
            return
        if has_range:
            mins = sum(e - s for s, e, _ in self.marker.ranges()) / 60
            text = ("요약은 설교 자막을 읽어서 고르는데, 아직 자막이 없습니다.\n\n"
                    f"지금 정해 둔 구간({mins:.0f}분)의 자막을 만든 뒤\n"
                    "이어서 요약을 고를까요? 구간은 그대로 둡니다.\n\n"
                    "CPU 에서 20~40분쯤 걸립니다.")
        else:
            text = ("요약하려면 설교 구간과 자막이 먼저 있어야 합니다.\n\n"
                    "지금 [자동으로 찾기] 로 설교 구간을 찾고 자막까지 만든 뒤\n"
                    "이어서 요약을 고를까요?\n\n"
                    "CPU 에서 20~40분쯤 걸립니다.")
        ok = QMessageBox.question(self, "사전 작업이 필요합니다", text)
        if ok != QMessageBox.Yes:
            return

        def then():
            if self.transcript and self.marker and self.marker.sermon_range():
                self.on_summary_pick()
            else:
                self.log("  자막이 만들어지지 않아 요약을 고르지 못했습니다. 진행 로그를 확인하세요.")

        if has_range:
            self.transcribe_current(then=then)
        else:
            self.on_analyze(with_stt=True, then=then)

    def on_summary_pick(self) -> None:
        """프로그램이 처음부터 다시 고른다. 쉼 찾기가 몇 초 걸려 딴 실로 돌린다."""
        if not self.marker:
            QMessageBox.information(self, "영상을 먼저 여세요",
                                    "[영상 가져오기] 로 예배 영상을 먼저 여세요.")
            return
        rng = self.marker.sermon_range()
        if not rng or not self.transcript:
            self._prepare_for_summary(has_range=bool(rng))
            return
        if self.sum_marks and any(m.chosen for m in self.sum_marks):
            ok = QMessageBox.question(
                self, "다시 고를까요?",
                "지금 골라 둔 문장이 모두 사라지고 처음부터 다시 고릅니다.\n계속할까요?")
            if ok != QMessageBox.Yes:
                return

        cfg = self._config()
        wav = cfg.audio_path(self.marker.video_id)
        transcript = self.transcript
        target = self._summary_target()
        fmt = self._sum_format()

        def job():
            found = pause_mod.find_pauses(wav, rng[0], rng[1]) if wav.exists() else []
            if found:
                print(f"  {pause_mod.describe(found)}")
            else:
                print("  소리 파일이 없어 문장 경계 그대로 씁니다")
            snap = pause_mod.make_snap(found) if found else None

            # 임베딩과 AI 는 있으면 쓰고 없으면 건너뛴다. 어느 쪽이 빠져도
            # 규칙만으로 결과가 나온다 — 켜서 나빠지는 일은 없어야 한다.
            vectors_of, chooser, notes = hl_mod.make_helpers(cfg, log=print)
            for note in notes:
                print("  " + note)
            theme = ""
            try:
                # 1) 문장: 갈래 → 개요 → 규칙 점수 → (임베딩)
                marks = hl_mod.build_marks(transcript, rng[0], rng[1])
                if not marks:
                    raise ToolError("설교 구간에 자막이 없습니다.\n"
                                    "[자막도] 를 켜고 [자동으로 찾기] 를 다시 해 주세요.")
                outline = hl_mod.find_outline(marks, rng[0], rng[1])
                hl_mod.score_marks(marks, hl_mod.keywords(marks))
                if vectors_of is not None:
                    try:
                        vecs = vectors_of([m.text for m in marks])
                        touched = hl_mod.apply_embedding(
                            marks, vecs, getattr(cfg, "embed_weight", 1.6))
                        print(f"  뜻으로 다시 매긴 문장 {touched}개")
                    except Exception as exc:
                        print(f"  임베딩을 건너뜁니다 ({type(exc).__name__}: {exc})")
                    finally:
                        # 임베딩은 여기서 할 일이 끝났다. 300MB 안팎을 쥐고 있으니,
                        # 바로 뒤에서 AI 가 2.5GB 를 올리기 전에 내려놓는다.
                        vectors_of = vecs = None
                        try:
                            from core import embed as embed_mod
                            embed_mod.release_model()
                        except Exception:
                            pass
                blk.mark_exits(marks)

                # 2) 덩어리: 문단급 쉼에서 나누고 역할을 매긴다 (형식과 무관한 바닥층)
                blocks = blk.segment(marks, found)
                blk.label_rules(blocks, marks)
                ask = getattr(chooser, "ask", None)
                if ask is not None and fmt != "sentences":
                    print(f"  AI 가 설교를 구획별로 나눠 읽습니다 — 덩어리 {len(blocks)}개")
                    changed, theme = blk.label_with_ai(blocks, ask, log=lambda t: print("  " + t))
                    print(f"  AI 가 역할을 고친 덩어리 {changed}개")
                    if theme:
                        print(f"  AI 가 본 설교 주제: {theme}")
                print("  " + blk.describe(blocks))

                # 3) 형식대로 조립한다
                slots = []
                if fmt == "sentences":
                    hl_mod.auto_pick(marks, outline, target, snap=snap)
                    if chooser is not None:
                        print("  " + hl_mod.llm_revise(
                            marks, outline, chooser, target,
                            getattr(cfg, "llm_candidates", 90), log=print))
                        hl_mod.fit_to_target(marks, outline, target, snap=snap)
                    print("  " + (f"목사님이 번호로 선언하신 대목 {len(outline.points)}곳을 찾았습니다"
                                  if outline.found else
                                  "번호로 선언된 대목을 못 찾아 문장 점수만으로 골랐습니다"))
                else:
                    slots = blk.assemble(fmt, blocks, target)
                    blk.apply_slots(slots, blocks, marks)
                    for sl in slots:
                        c = sl.chosen
                        if c:
                            print(f"  [{sl.name}] {hms(c.start)} 부터 {int(c.end - c.start)}초 — {c.preview}…")
            finally:
                closer = getattr(chooser, "close", None)
                if closer:
                    closer()          # 띄운 llama-server 를 반드시 내린다
            return marks, outline, found, blocks, slots, theme

        def done(result):
            (self.sum_marks, self.sum_outline, self.sum_pauses,
             self.sum_blocks, self.sum_slots, self.sum_theme) = result
            self.sum_undo = []
            self.sum_initial = [m.chosen for m in self.sum_marks]
            self._save_summary_picks()
            self._fill_summary_list()
            self._fill_slots()
            self.tabs.setCurrentWidget(self.summary_tab)
            self.log(self.sum_meter.text())

        what = blk.format_name(fmt)      # 고르기 안내에는 설명까지 보여 준다
        self._run(job, done,
                  title=f"{blk.short_name(fmt)} 고르기",
                  detail=f"설교 {(rng[1] - rng[0]) / 60:.0f}분에서 "
                         f"{target / 60:.0f}분을 골라냅니다 · 형식: {what}",
                  done_message="요약할 대목을 골랐습니다",
                  next_step=("[요약] 탭에서 칸마다 [다른 후보] 로 바꿔 보거나 문장을 넣고 빼세요"
                             if fmt != "sentences" else
                             "[요약] 탭에서 문장을 넣고 빼며 다듬으세요"))

    def _summary_ready(self, what: str) -> Path | None:
        """영상·mp3 를 만들기 전 공통 확인. 준비됐으면 원본 경로를 돌려준다."""
        if not self.sum_marks or not any(m.chosen for m in self.sum_marks):
            QMessageBox.information(
                self, "고른 문장이 없습니다",
                "[요약 골라줘] 를 눌러 밑그림을 만든 뒤,\n"
                "문장을 넣고 빼서 다듬어 주세요.")
            return None
        src = self.media_path
        if not src or not Path(src).exists():
            QMessageBox.information(
                self, "영상이 필요합니다",
                f"요약 {what}는 받아 둔 영상에서 잘라 만듭니다.\n"
                "[영상 가져오기] 로 영상을 먼저 여세요.")
            return None
        if not self._ask_title():
            return None
        self._save_summary_picks()
        self.on_save()
        self._save_summary_label()        # 사람이 확정한 요약 → 학습 자료
        return Path(src)

    def on_summary_mp3(self) -> None:
        """요약을 소리만 mp3 로. 영상과 똑같은 구간을 쓴다."""
        # 화면 글자와 파일 이름이 같은 데서 나오게 한다 (2026-10-20:
        # 핵심요약을 골랐는데 화면은 "3분 요약" 이라고 했다).
        what = blk.short_name(self._sum_format())
        src = self._summary_ready("mp3")
        if not src:
            return
        cfg = self._config()
        marker = self.marker
        transcript = self.transcript
        wav = cfg.audio_path(marker.video_id)
        snap = self._summary_snap()
        total = hl_mod.total_seconds(self.sum_marks, snap)
        pieces = len(hl_mod.runs(self.sum_marks, snap))

        def job():
            return export_mod.export_summary_mp3(marker, cfg, src, transcript, wav)

        def done(result):
            lines = "\n".join(f"{k}: {v}" for k, v in result.items())
            self.log(lines)
            box = QMessageBox(self)
            box.setWindowTitle(f"{what} mp3 완료")
            box.setText(lines)
            open_btn = box.addButton("폴더 열기", QMessageBox.ActionRole)
            box.addButton("닫기", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is open_btn:
                folder = Path(result["mp3"]).parent
                QDesktopServices.openUrl(_QUrl.fromLocalFile(str(folder.resolve())))

        self._run(job, done,
                  title=f"{what} mp3 만들기",
                  detail=f"조각 {pieces}개 · {int(total // 60)}분 {int(total % 60):02d}초 "
                         f"· 소리만 뽑으므로 금방 끝납니다",
                  done_message=f"{what} mp3 를 만들었습니다",
                  next_step="[폴더 열기] 로 확인하세요")

    def on_summary_export(self) -> None:
        # 화면 글자와 파일 이름이 같은 데서 나오게 한다 (2026-10-20:
        # 핵심요약을 골랐는데 화면은 "3분 요약" 이라고 했다).
        what = blk.short_name(self._sum_format())
        src = self._summary_ready("영상")
        if not src:
            return
        cfg = self._config()
        marker = self.marker
        transcript = self.transcript
        wav = cfg.audio_path(marker.video_id)
        total = hl_mod.total_seconds(self.sum_marks, self._summary_snap())
        pieces = len(hl_mod.runs(self.sum_marks, self._summary_snap()))

        def job():
            return export_mod.export_summary(marker, cfg, src, transcript, wav)

        def done(result):
            lines = "\n".join(f"{k}: {v}" for k, v in result.items())
            self.log(lines)
            box = QMessageBox(self)
            box.setWindowTitle(f"{what} 완료")
            box.setText(lines)
            open_btn = box.addButton("폴더 열기", QMessageBox.ActionRole)
            box.addButton("닫기", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is open_btn:
                folder = Path(result["video"]).parent
                QDesktopServices.openUrl(_QUrl.fromLocalFile(str(folder.resolve())))

        self._run(job, done,
                  title=f"{what} 영상 만들기",
                  detail=f"조각 {pieces}개 · {int(total // 60)}분 {int(total % 60):02d}초 "
                         f"· 세로 화면 1080x1920",
                  done_message=f"{what} 영상을 만들었습니다",
                  next_step="[폴더 열기] 로 확인하세요")


# ------------------------------------------------------------------ 도우미
def _strip_time(text: str) -> str:
    """'[00:12:34]  글' 에서 시각 표시를 떼고 글만. 고치다 시각을 지워도 견딘다."""
    import re
    m = re.match(r"^\s*\[[^\]]*\]\s*(.*)$", text or "", re.S)
    return " ".join((m.group(1) if m else (text or "")).split())


def word_change(old: str, new: str) -> tuple[str, str] | None:
    """낱말 하나만 바뀌었으면 (틀린 말, 바른 말). 뒤에 붙은 같은 조사는 뗀다.

    '니에미아가 분노했습니다' → '느헤미야가 분노했습니다'  ⇒  ('니에미아', '느헤미야')
    여러 낱말을 고쳤거나 띄어쓰기가 바뀌었으면 None — 그때는 사전에 넣지 않는다.
    """
    a, b = old.split(), new.split()
    if len(a) != len(b):
        return None
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    if len(diff) != 1:
        return None
    x0, y0 = (t.strip(".,?!\"'") for t in diff[0])
    x, y = x0, y0
    k = 0
    while k < min(len(x), len(y)) - 1 and x[-1 - k] == y[-1 - k]:
        k += 1
    if k:
        x, y = x[:-k], y[:-k]
    if len(x) < 3:
        # 두 글자 말(경유 → 경외)만 떼어 사전에 넣으면 '경유지' 같은 맞는 말까지 바뀐다.
        # 그럴 땐 낱말 통째로 넣는다 (경유하면서 → 경외하면서).
        x, y = x0, y0
    if x in y:
        x, y = x0, y0                  # '창의진 → 창의진교회' 는 다시 돌리면 '창의진교회교회' 가 된다
    if len(x) < 2 or not y or x == y or x in y:
        return None
    return x, y
