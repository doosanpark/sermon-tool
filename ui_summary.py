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

from PySide6.QtCore import Qt, QUrl as _QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core import embed as embed_mod
from core import export as export_mod
from core import highlight as hl_mod
from core import llm as llm_mod
from core import pauses as pause_mod
from core.util import hms


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
        self.sum_meter.setStyleSheet("font-size:13px; font-weight:600; color:#1c1e21;")
        top.addWidget(self.sum_meter, 1)
        self.sum_bar = QProgressBar()
        self.sum_bar.setRange(0, 130)
        self.sum_bar.setValue(0)
        self.sum_bar.setFixedWidth(170)
        self.sum_bar.setTextVisible(False)
        self.sum_bar.setToolTip("가운데 눈금이 목표 길이입니다")
        top.addWidget(self.sum_bar)
        lay.addLayout(top)

        self.sum_list = QListWidget()
        self.sum_list.setToolTip(
            "네모를 누르면 그 문장을 요약에 넣고 뺍니다.\n"
            "글을 누르면 그 자리로 이동하고, 두 번 누르면 그 대목을 들려줍니다.\n"
            "위쪽 길이가 바로 바뀝니다 — 목표에 맞추기만 하면 됩니다.")
        self.sum_list.itemChanged.connect(self._on_summary_toggled)
        self.sum_list.itemClicked.connect(
            lambda it: self._seek(float(it.data(Qt.UserRole))))
        self.sum_list.itemDoubleClicked.connect(self._on_summary_play)
        self.sum_list.setMinimumHeight(90)       # 문장 서너 줄은 늘 보이게
        lay.addWidget(self.sum_list, 1)

        how = QHBoxLayout()
        how.addWidget(QLabel("고르는 방법"))
        self.pick_mode_box = QComboBox()
        self.pick_mode_box.addItems(list(self.PICK_CHOICES))
        self.pick_mode_box.setToolTip(
            "규칙만        낱말만 봅니다. 아무것도 받지 않아도 됩니다.\n"
            "뜻까지 보기   설교 전체를 대표하는 문장을 찾아냅니다. 모델 120MB.\n"
            "뜻 + AI      좁혀진 후보를 AI 가 읽고 고릅니다. 모델 2GB, 1~3분.")
        self.pick_mode_box.currentTextChanged.connect(self._on_pick_mode)
        how.addWidget(self.pick_mode_box)
        self.ai_status = QLabel("")
        self.ai_status.setStyleSheet("color:#555;")
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
        self.btn_sum_make = QPushButton("3분 요약 영상 만들기")
        self.btn_sum_make.setStyleSheet("font-weight:600; padding:5px 12px;")
        self.btn_sum_mp3 = QPushButton("3분 요약 mp3")
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
        self.sum_hint.setStyleSheet("color:#666; font-size:11px;")
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
        self.btn_ai_get.setEnabled(bool(missing) and can_get)
        if not missing:
            self.ai_status.setText("준비됐습니다")
            self.ai_status.setStyleSheet("color:#1f5e56;")
        elif not can_get:
            self.ai_status.setText("llama-server 실행파일이 없습니다 — bin\\llama\\ 를 확인하세요")
            self.ai_status.setStyleSheet("color:#9e4b3c;")
        else:
            self.ai_status.setText("아직 없음: " + ", ".join(missing)
                                   + "  →  [모델 받기]")
            self.ai_status.setStyleSheet("color:#a8763e;")

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
                  next_step="[3분 요약] 탭에서 [요약 골라줘] 를 누르세요")

    # -------- 상태 --------
    def _summary_snap(self):
        """구간을 쉼에 맞춰 주는 함수. 쉼은 한 번만 찾아 두고 계속 쓴다."""
        return pause_mod.make_snap(self.sum_pauses) if self.sum_pauses else None

    def _summary_target(self) -> float:
        return float(getattr(self._config(), "summary_target", 180.0))

    def _summary_snapshot(self) -> None:
        self.sum_undo.append([m.chosen for m in self.sum_marks])
        del self.sum_undo[:-30]

    def _fill_summary_list(self) -> None:
        """문장 목록을 다시 그린다. 색이 곧 갈래다."""
        self.sum_list.blockSignals(True)
        self.sum_list.clear()
        for m in self.sum_marks:
            item = QListWidgetItem(f"[{hms(m.start)}]  {m.text}")
            item.setData(Qt.UserRole, m.start)
            item.setData(Qt.UserRole + 1, m.index)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if m.chosen else Qt.Unchecked)
            if m.kind == "point":
                f = item.font()
                f.setBold(True)
                item.setFont(f)
                item.setToolTip(m.why or "목사님이 번호로 선언하신 대목입니다")
            elif m.kind in ("reading", "aside"):
                item.setForeground(QColor("#9aa3a0"))
                item.setToolTip("성경을 읽는 부분입니다 — 요약에는 잘 안 맞습니다")
            elif m.why:
                item.setToolTip(f"고른 이유: {m.why}")
            self._paint_summary_item(item, m)
            self.sum_list.addItem(item)
        self.sum_list.blockSignals(False)
        self._update_summary_meter()

    def _paint_summary_item(self, item: QListWidgetItem, m) -> None:
        if m.chosen:
            item.setBackground(QColor("#fdf0d9") if m.kind == "point" else QColor("#e3edea"))
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
        idx = item.data(Qt.UserRole + 1)
        if idx is None or not (0 <= idx < len(self.sum_marks)):
            return
        m = self.sum_marks[idx]
        want = item.checkState() == Qt.Checked
        if want == m.chosen:
            return
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

    def _save_summary_picks(self) -> None:
        if not self.marker:
            return
        self.marker.set_summary_picks([m.start for m in self.sum_marks if m.chosen])

    def on_summary_undo(self) -> None:
        if not self.sum_undo:
            QMessageBox.information(self, "되돌릴 것이 없습니다",
                                    "아직 문장을 넣거나 빼지 않았습니다.")
            return
        for m, was in zip(self.sum_marks, self.sum_undo.pop()):
            m.chosen = was
        self._save_summary_picks()
        self._fill_summary_list()

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
            try:
                marks, outline = hl_mod.plan(
                    transcript, rng[0], rng[1], target, snap=snap,
                    vectors_of=vectors_of, chooser=chooser,
                    weight=getattr(cfg, "embed_weight", 1.6),
                    limit=getattr(cfg, "llm_candidates", 90), log=print)
            finally:
                closer = getattr(chooser, "close", None)
                if closer:
                    closer()          # 띄운 llama-server 를 반드시 내린다

            print("  " + (f"목사님이 번호로 선언하신 대목 {len(outline.points)}곳을 찾았습니다"
                          if outline.found else
                          "번호로 선언된 대목을 못 찾아 문장 점수만으로 골랐습니다"))
            return marks, outline, found

        def done(result):
            self.sum_marks, self.sum_outline, self.sum_pauses = result
            self.sum_undo = []
            self._save_summary_picks()
            self._fill_summary_list()
            self.tabs.setCurrentWidget(self.summary_tab)
            self.log(self.sum_meter.text())

        self._run(job, done,
                  title="3분 요약 고르기",
                  detail=f"설교 {(rng[1] - rng[0]) / 60:.0f}분에서 "
                         f"{target / 60:.0f}분을 골라냅니다",
                  done_message="요약할 문장을 골랐습니다",
                  next_step="[3분 요약] 탭에서 문장을 넣고 빼며 다듬으세요")

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
        return Path(src)

    def on_summary_mp3(self) -> None:
        """3분 요약을 소리만 mp3 로. 영상과 똑같은 구간을 쓴다."""
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
            box.setWindowTitle("3분 요약 mp3 완료")
            box.setText(lines)
            open_btn = box.addButton("폴더 열기", QMessageBox.ActionRole)
            box.addButton("닫기", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is open_btn:
                folder = Path(result["mp3"]).parent
                QDesktopServices.openUrl(_QUrl.fromLocalFile(str(folder.resolve())))

        self._run(job, done,
                  title="3분 요약 mp3 만들기",
                  detail=f"조각 {pieces}개 · {int(total // 60)}분 {int(total % 60):02d}초 "
                         f"· 소리만 뽑으므로 금방 끝납니다",
                  done_message="3분 요약 mp3 를 만들었습니다",
                  next_step="[폴더 열기] 로 확인하세요")

    def on_summary_export(self) -> None:
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
            box.setWindowTitle("3분 요약 완료")
            box.setText(lines)
            open_btn = box.addButton("폴더 열기", QMessageBox.ActionRole)
            box.addButton("닫기", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is open_btn:
                folder = Path(result["video"]).parent
                QDesktopServices.openUrl(_QUrl.fromLocalFile(str(folder.resolve())))

        self._run(job, done,
                  title="3분 요약 영상 만들기",
                  detail=f"조각 {pieces}개 · {int(total // 60)}분 {int(total % 60):02d}초 "
                         f"· 세로 화면 1080x1920",
                  done_message="3분 요약 영상을 만들었습니다",
                  next_step="[폴더 열기] 로 확인하세요")
