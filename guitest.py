#!/usr/bin/env python3
"""편집기 화면을 실제로 만들어 [3분 요약] 탭을 눌러 본다.

    python guitest.py                 # 아무 마커나 하나 골라 확인
    python guitest.py ZEKoljLiXOw     # 영상 지정

selftest.py 는 화면 없이 로직만 본다. 이 검사는 PySide6 로 창을 진짜 만들어
버튼과 목록이 이어져 있는지까지 본다 — 화면을 띄우지는 않는다(offscreen).

이 검사가 실제로 잡은 것: [이전 작업 불러오기] 로 돌아왔을 때 눈금이
'2분 22초' 라고 떴는데 실제로 나오는 영상은 2분 55초였다. 불러올 때 쉼을
다시 찾지 않아 스냅으로 벌어지는 33초를 빼고 세고 있었다.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:
    print("PySide6 가 없어 화면 검사를 건너뜁니다.  pip install PySide6")
    raise SystemExit(0)

# 화면이 없으므로 팝업은 전부 자동으로 닫는다
for _name in ("information", "warning", "critical"):
    setattr(QMessageBox, _name, staticmethod(lambda *a, **k: QMessageBox.Ok))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)


def pick_video(cfg) -> str | None:
    want = sys.argv[1] if len(sys.argv) > 1 else None
    if want:
        return want
    for p in sorted(cfg.marker_dir.glob("*.json")):
        if cfg.transcript_path(p.stem).exists():
            return p.stem
    return None


def main() -> int:
    app = QApplication(sys.argv[:1])
    import app as appmod
    from core import export as export_mod
    from core import highlight as hl
    from core import transcribe
    from core.marker import Marker

    win = appmod.MainWindow()
    names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    assert any("요약" in n for n in names), names
    print(f"창 OK      {win.windowTitle()}  ·  탭 {len(names)}개")

    cfg = win._config()
    vid = pick_video(cfg)
    if not vid:
        print("자막까지 만들어 둔 영상이 없어 나머지는 건너뜁니다.")
        return 0

    marker = Marker.load(cfg.marker_path(vid))
    transcript = transcribe.load_transcript(cfg.transcript_path(vid))
    win._apply_marker(marker, transcript)
    print(f"불러오기 OK  {vid}  ·  자막 {len(transcript)}문장  ·  목록 {win.sum_list.count()}줄")

    if not win.sum_marks:
        print("아직 요약을 고른 적이 없어 [요약 골라줘] 부터 확인합니다.")
        rng = marker.sermon_range()
        from core import pauses as pz
        wav = cfg.audio_path(vid)
        found = pz.find_pauses(wav, rng[0], rng[1]) if wav.exists() else []
        snap = pz.make_snap(found) if found else None
        win.sum_marks, win.sum_outline = hl.plan(
            transcript, rng[0], rng[1], win._summary_target(), snap=snap)
        win.sum_pauses = found
        win._save_summary_picks()
        win._fill_summary_list()
    print(f"고르기 OK    {win.sum_meter.text()}  ·  {win.sum_hint.text()}")

    # 눈금이 말하는 길이와 실제로 내보낼 길이가 같아야 한다.
    # 컷을 쉼에 맞추면 구간이 벌어지므로, 한쪽만 스냅을 빼면 여기서 걸린다.
    if win.sum_pauses:
        shown = hl.total_seconds(win.sum_marks, win._summary_snap())
        real = sum(e - s for s, e in export_mod.summary_runs(
            win.marker, cfg, transcript, cfg.audio_path(vid)))
        assert abs(shown - real) < 0.01, f"화면 {shown:.2f}초 ≠ 결과 {real:.2f}초"
        print(f"눈금 OK      화면 {shown:.1f}초 = 내보낼 길이 {real:.1f}초")
    else:
        print("눈금 —       소리 파일이 없어 건너뜀")

    # 문장 하나를 빼 보고, 되돌려 본다 (실제 클릭과 같은 경로)
    before = win.sum_meter.text()
    rows = [i for i in range(win.sum_list.count())
            if win.sum_list.item(i).checkState() == Qt.Checked]
    assert rows, "고른 문장이 하나도 없다"
    win.sum_list.item(rows[0]).setCheckState(Qt.Unchecked)
    app.processEvents()
    assert win.sum_meter.text() != before, "문장을 빼도 길이가 안 바뀐다"
    idx = win.sum_list.item(rows[0]).data(Qt.UserRole + 1)
    assert not win.sum_marks[idx].chosen
    print(f"빼기 OK      {win.sum_meter.text()}")

    win.on_summary_undo()
    app.processEvents()
    assert win.sum_meter.text() == before, "되돌렸는데 길이가 다르다"
    print(f"되돌리기 OK  {win.sum_meter.text()}")

    # 요약 선택이 설교 구간을 오염시키면 [설교 영상 만들기] 가 망가진다
    labels = [lab for _, _, lab in win.marker.ranges()]
    assert "summary" not in labels, labels
    print(f"구간 OK      설교 내보내기 구간은 그대로 {labels}")

    # 고르는 방법 — 화면 선택이 설정으로 이어지는지
    for label, mode in appmod.MainWindow.PICK_CHOICES.items():
        win.pick_mode_box.setCurrentText(label)
        app.processEvents()
        c = win._config()
        assert c.use_embedding == (mode in ("embed", "llm")), label
        assert c.use_llm == (mode == "llm"), label
    print(f"방법 OK      선택 {win.pick_mode_box.count()}가지가 설정으로 이어짐 · "
          f"지금 '{win.ai_status.text()}'")

    # 저장·복원 — QSettings 에서 다른 설정을 지우지 않는지
    sub_before = win.subtitle_box.currentText()
    win.pick_mode_box.setCurrentText(list(appmod.MainWindow.PICK_CHOICES)[-1])
    app.processEvents()
    again = appmod.MainWindow()
    assert again.pick_mode_box.currentText() == list(appmod.MainWindow.PICK_CHOICES)[-1]
    assert again.subtitle_box.currentText() == sub_before, "자막 설정이 지워졌다"
    print("저장 OK      방법이 되살아나고 다른 설정도 그대로")

    # 영상과 mp3 가 정확히 같은 구간을 써야 한다. 갈라지면 mp3 만 다른 내용이 된다.
    if win.sum_pauses:
        from core import export as ex
        runs = ex.summary_runs(win.marker, cfg, transcript, cfg.audio_path(vid))
        shown = hl.total_seconds(win.sum_marks, win._summary_snap())
        assert abs(sum(e - s for s, e in runs) - shown) < 0.01
        print(f"mp3 OK       영상과 같은 구간 {len(runs)}개를 씁니다 ({shown:.1f}초)")
    assert hasattr(win, "btn_sum_mp3"), "3분 요약 mp3 버튼이 없다"

    # 작업이 도는 동안에는 구간을 못 만져야 한다. 만지면 작업이 끝날 때
    # _apply_marker 가 구간을 새로 넣으면서 그 편집이 소리 없이 사라진다.
    guard = {"구간 막대": win.range_bar, "확대 막대": win.zoom_bar,
             "구간 목록": win.clip_list, "구간 추가": win.btn_add,
             "시작 조정": win.btn_edge_start, "요약 골라줘": win.btn_sum_pick,
             "요약 목록": win.sum_list, "고르는 방법": win.pick_mode_box,
             "요약 mp3": win.btn_sum_mp3}
    win._set_busy(True)
    open_ = [n for n, x in guard.items() if x.isEnabled()]
    assert not open_, f"작업 중에도 만질 수 있다: {open_}"
    win._set_busy(False)
    shut = [n for n, x in guard.items() if not x.isEnabled()]
    assert not shut, f"작업이 끝났는데 잠긴 채다: {shut}"
    print(f"잠금 OK      작업 중 {len(guard)}곳이 잠기고 끝나면 풀림")

    print("\n화면 검사 전체 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
