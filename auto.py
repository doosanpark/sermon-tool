#!/usr/bin/env python3
"""자동 처리 — 채널을 감시해 새 예배 영상을 알아서 잘라놓는다.

  python auto.py setup "https://www.youtube.com/@채널이름"   최초 1회 설정
  python auto.py once                                        새 영상 1편 처리
  python auto.py watch                                       주기적으로 감시
  python auto.py status                                      처리 이력 확인
  python auto.py install-task                                매주 자동 실행 등록

사람이 할 일은 결과를 확인하는 것뿐이다. 신뢰도가 기준 아래면 자르지 않고
"확인 필요"로 남겨 두므로, GUI 에서 [이전 작업 불러오기] 로 열어 손보면 된다.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from core import export as export_mod
from core import fetch, transcribe
from core.config import Config
from core.detect import apply_service_result, detect, format_detection
from core.diarize import diarize
from core.voiceprint import Embedder, Voiceprint, analyze_media
from core.marker import Marker
from core.refine import refine_benediction, refine_sermon
from core.util import ToolError, ffprobe_duration, hms

ROOT = Path(__file__).resolve().parent

DEFAULTS = {
    "channel": "",
    "min_confidence": 0.9,     # 이 값 이상이면 사람 확인 없이 내보낸다
    "with_stt": False,         # 자막까지 만들지 (CPU 에서 20~40분 더)
    "auto_export": True,
    "include_benediction": True,
    "check_count": 5,          # 채널 최근 몇 편을 훑을지
    "min_duration_min": 30,    # 이보다 짧은 영상은 예배가 아니라고 보고 건너뜀
    "watch_interval_min": 60,
}


# ------------------------------------------------------------------ 설정/상태
def config_path(cfg: Config) -> Path:
    return cfg.work_dir / "auto_config.json"


def state_path(cfg: Config) -> Path:
    return cfg.work_dir / "auto_state.json"


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(default)
    merged = dict(default)
    merged.update(data)
    return merged


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def log_line(cfg: Config, text: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {text}"
    print(line)
    path = cfg.work_dir / "auto_log.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ------------------------------------------------------------------ 처리 본체
def process_one(video: dict, cfg: Config, opts: dict) -> dict:
    """영상 한 편을 끝까지 처리하고 결과 요약을 돌려준다."""
    vid = video["id"]
    url = video.get("url") or f"https://www.youtube.com/watch?v={vid}"

    meta = fetch.info(url)
    if meta.get("live_status") == "is_live":
        return {"video_id": vid, "status": "live", "title": meta["title"],
                "note": "아직 방송 중 — 끝난 뒤 다시 시도합니다"}

    duration = meta["duration"]
    if duration and duration < opts["min_duration_min"] * 60:
        return {"video_id": vid, "status": "skipped", "title": meta["title"],
                "note": f"{duration/60:.0f}분짜리 — 예배 영상이 아닌 듯"}

    log_line(cfg, f"처리 시작: {meta['title']} ({duration/60:.0f}분)")

    wav = cfg.audio_path(vid)
    if not wav.exists():
        log_line(cfg, "  오디오 받는 중")
        fetch.download_audio(meta["webpage_url"], wav)
    duration = duration or ffprobe_duration(wav)

    cfg.include_benediction = bool(opts["include_benediction"])
    marker = Marker(video_id=vid, url=meta["webpage_url"], title=meta["title"],
                    upload_date=meta["upload_date"], duration=duration)

    if cfg.use_voiceprint and cfg.voiceprint_path.exists():
        log_line(cfg, "  성문 대조로 구간 찾는 중 (화자분리보다 60배 빠름)")
        vp = Voiceprint.load(cfg.voiceprint_path)
        result = analyze_media(wav, vp, Embedder(threads=4), hop=cfg.vp_hop,
                               threshold=cfg.vp_threshold, progress=lambda s: log_line(cfg, s))
        if not result.sermon:
            return {"video_id": vid, "status": "review", "title": meta["title"],
                    "note": "성문으로 설교를 찾지 못했습니다 — GUI 에서 확인하세요"}
        apply_service_result(marker, result, cfg)
        for line in result.reasons:
            print(f"  · {line}")
    else:
        log_line(cfg, "  화자 분리 (성문이 없어 느린 경로)")
        segments = diarize(wav, cfg, cache_path=cfg.diar_path(vid))
        det = detect(segments, duration, cfg)
        print(format_detection(det))
        marker.sermon_ai_start, marker.sermon_ai_end = det.sermon.start, det.sermon.end
        if cfg.include_benediction and det.benediction:
            marker.bene_ai_start = det.benediction.start
            marker.bene_ai_end = det.benediction.end
        marker.confidence = det.confidence
        marker.bene_confidence = det.bene_confidence
        marker.reasons = det.reasons
        marker.pastor_speaker = det.pastor
        marker.pastor_ratio = round(det.pastor_ratio, 3)

    transcript: list[dict] = []
    if opts["with_stt"]:
        log_line(cfg, "  자막 생성 (오래 걸립니다)")
        for start, end, label in marker.ranges():
            transcript.extend(transcribe.transcribe_range(wav, start, end, cfg, label))
        transcribe.save_transcript(cfg.transcript_path(vid), transcript)
        if transcript:
            r = marker.sermon_range()
            if r:
                s, e, _ = refine_sermon(transcript, r[0], r[1], cfg)
                marker.sermon_ai_start, marker.sermon_ai_end = s, e
            r = marker.bene_range()
            if r:
                s, e, _ = refine_benediction(transcript, r[0], r[1], cfg)
                marker.bene_ai_start, marker.bene_ai_end = s, e
            marker.refined = True

    marker.sync_clips_from_detection()
    marker.save(cfg.marker_path(vid))

    result = {
        "video_id": vid,
        "title": meta["title"],
        "upload_date": meta["upload_date"],
        "confidence": marker.confidence,
        "sermon": [hms(s) for s in (marker.sermon_range() or (0, 0))],
        "processed_at": datetime.now().isoformat(timespec="seconds"),
    }

    if marker.confidence < opts["min_confidence"]:
        result["status"] = "review"
        result["note"] = (f"신뢰도 {marker.confidence} < 기준 {opts['min_confidence']} — "
                          "자르지 않았습니다. GUI 에서 확인하세요")
        log_line(cfg, f"  확인 필요 (신뢰도 {marker.confidence})")
        return result

    if not opts["auto_export"]:
        result["status"] = "detected"
        result["note"] = "구간만 찾아 두었습니다 (자동 자르기 꺼짐)"
        return result

    log_line(cfg, "  설교 구간만 내보내는 중")
    out = export_mod.export(marker, cfg, transcript)
    result["status"] = "done"
    result["video"] = str(out.get("video", ""))
    if "srt" in out:
        result["srt"] = str(out["srt"])
    log_line(cfg, f"  완료: {out.get('video')}")
    return result


def run_once(cfg: Config, opts: dict, dry_run: bool = False) -> list[dict]:
    if not opts["channel"]:
        raise ToolError('채널이 설정되지 않았습니다.  python auto.py setup "채널URL"')

    state = load_json(state_path(cfg), {"processed": {}})
    processed = state["processed"]

    log_line(cfg, f"채널 확인: {opts['channel']}")
    videos = fetch.channel_videos(opts["channel"], opts["check_count"])
    pending = [v for v in videos if v["id"] not in processed]

    if not pending:
        log_line(cfg, "새 영상이 없습니다.")
        return []

    log_line(cfg, f"새 영상 {len(pending)}편")
    if dry_run:
        for v in pending:
            print(f"  {v['id']}  {v['title']}")
        return []

    results = []
    for video in reversed(pending):        # 오래된 것부터
        try:
            result = process_one(video, cfg, opts)
        except ToolError as exc:
            result = {"video_id": video["id"], "title": video.get("title", ""),
                      "status": "error", "note": str(exc)[:400]}
            log_line(cfg, f"  실패: {exc}")
        except Exception as exc:  # 자동 실행이므로 한 편 실패로 멈추지 않는다
            result = {"video_id": video["id"], "title": video.get("title", ""),
                      "status": "error", "note": f"{type(exc).__name__}: {exc}"[:400]}
            log_line(cfg, f"  실패: {exc}")

        results.append(result)
        # 방송 중이면 다음 실행에서 다시 시도해야 하므로 기록하지 않는다
        if result.get("status") != "live":
            processed[video["id"]] = result
            save_json(state_path(cfg), state)
    return results


# ------------------------------------------------------------------ 명령
def cmd_setup(args, cfg: Config, opts: dict) -> int:
    opts["channel"] = args.channel
    if args.min_confidence is not None:
        opts["min_confidence"] = args.min_confidence
    if args.stt:
        opts["with_stt"] = True
    save_json(config_path(cfg), opts)
    print("설정을 저장했습니다:", config_path(cfg))
    for k, v in opts.items():
        print(f"  {k}: {v}")
    print('\n이제  python auto.py once  로 시험해 보세요.')
    return 0


def cmd_once(args, cfg: Config, opts: dict) -> int:
    results = run_once(cfg, opts, dry_run=args.dry_run)
    if results:
        print("\n=== 결과 ===")
        for r in results:
            print(f"  [{r['status']}] {r.get('title','')}  {r.get('note','')}")
    return 0


def cmd_watch(args, cfg: Config, opts: dict) -> int:
    interval = (args.interval or opts["watch_interval_min"]) * 60
    log_line(cfg, f"감시 시작 — {interval/60:.0f}분마다 확인합니다. Ctrl+C 로 중단.")
    while True:
        try:
            run_once(cfg, opts)
        except ToolError as exc:
            log_line(cfg, f"확인 실패: {exc}")
        except KeyboardInterrupt:
            log_line(cfg, "감시를 중단했습니다.")
            return 0
        time.sleep(interval)


def cmd_status(args, cfg: Config, opts: dict) -> int:
    print("설정:", config_path(cfg))
    for k, v in opts.items():
        print(f"  {k}: {v}")
    state = load_json(state_path(cfg), {"processed": {}})
    items = sorted(state["processed"].values(),
                   key=lambda r: r.get("processed_at", ""), reverse=True)
    print(f"\n처리 이력 {len(items)}건")
    for r in items[:20]:
        span = " ~ ".join(r.get("sermon", [])) if r.get("sermon") else ""
        print(f"  [{r.get('status')}] {r.get('upload_date','')} {r.get('title','')[:40]}"
              f"  {span}  신뢰도 {r.get('confidence','')}")
        if r.get("note"):
            print(f"      {r['note']}")
    return 0


def cmd_install_task(args, cfg: Config, opts: dict) -> int:
    """윈도우 작업 스케줄러에 매주 등록한다."""
    python = sys.executable
    if python.lower().endswith("python.exe"):
        python = python[:-len("python.exe")] + "pythonw.exe"  # 창 없이 실행
    command = f'"{python}" "{ROOT / "auto.py"}" once'
    task = args.name
    cmd = ["schtasks", "/Create", "/TN", task, "/TR", command,
           "/SC", "WEEKLY", "/D", args.day, "/ST", args.at, "/F"]
    print("등록할 작업:")
    print("  이름:", task)
    print("  일정:", args.day, args.at, "매주")
    print("  실행:", command)
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        print("\nschtasks 를 찾을 수 없습니다 (윈도우가 아닌 환경).")
        return 1
    except subprocess.CalledProcessError as exc:
        print("\n등록 실패. 관리자 권한 명령 프롬프트에서 다시 시도하세요.")
        return exc.returncode
    print("\n등록했습니다. 해제하려면:  schtasks /Delete /TN", task, "/F")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="새 예배 영상을 자동으로 찾아 설교만 잘라낸다")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="채널 등록")
    p.add_argument("channel", help='예: "https://www.youtube.com/@채널이름"')
    p.add_argument("--min-confidence", type=float)
    p.add_argument("--stt", action="store_true", help="자막까지 만들기")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("once", help="새 영상 처리 (한 번)")
    p.add_argument("--dry-run", action="store_true", help="무엇을 처리할지만 보여주기")
    p.set_defaults(func=cmd_once)

    p = sub.add_parser("watch", help="주기적으로 감시")
    p.add_argument("--interval", type=int, help="분 단위. 기본 60")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("status", help="설정과 처리 이력")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("install-task", help="매주 자동 실행 등록 (윈도우)")
    p.add_argument("--name", default="SermonTool-Weekly")
    p.add_argument("--day", default="SUN", help="MON..SUN. 기본 SUN")
    p.add_argument("--at", default="14:00", help="HH:MM. 기본 14:00")
    p.set_defaults(func=cmd_install_task)

    args = ap.parse_args()
    cfg = Config()
    cfg.ensure_dirs()
    opts = load_json(config_path(cfg), DEFAULTS)
    return args.func(args, cfg, opts)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ToolError as exc:
        print(f"\n[오류] {exc}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
