#!/usr/bin/env python3
"""예배영상 설교/축도 자동추출 — CLI 진입점.

  python analyze.py doctor                        설치 상태 점검
  python analyze.py list   <채널URL> [-n 10]     최근 영상 목록
  python analyze.py analyze <영상URL>            구간 검출 + 마커 저장
  python analyze.py export  <영상URL|video_id>   확정 구간 다운로드 + 합본 + SRT
  python analyze.py run     <영상URL>            analyze + export 연속 실행
  python analyze.py show    <video_id>           저장된 마커 확인
  python analyze.py set     <video_id> --sermon 32:18-1:09:44 --bene 1:15:20-1:16:50
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import export as export_mod
from core import fetch, transcribe
from core.config import Config
from core.detect import detect, format_detection
from core.diarize import diarize
from core.marker import Marker
from core.refine import refine_benediction, refine_sermon
from core.util import ToolError, check_tools, ffprobe_duration, hms


# ---------------------------------------------------------------- helpers
def parse_ts(text: str) -> float:
    """'1:09:44' / '32:18' / '1938' -> 초"""
    parts = text.strip().split(":")
    if len(parts) == 1:
        return float(parts[0])
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def parse_range(text: str) -> tuple[float, float]:
    if "-" not in text:
        raise ValueError(f"구간 형식이 잘못됐습니다: {text} (예: 32:18-1:09:44)")
    a, b = text.split("-", 1)
    return parse_ts(a), parse_ts(b)


def build_config(args: argparse.Namespace) -> Config:
    cfg = Config()
    if getattr(args, "work_dir", None):
        cfg.work_dir = Path(args.work_dir)
    if getattr(args, "gap", None):
        cfg.gap_threshold = args.gap
    if getattr(args, "model", None):
        cfg.whisper_model = args.model
    if getattr(args, "device", None):
        cfg.device = args.device
        if args.device != "cpu":
            cfg.compute_type = "float16"
    cfg.ensure_dirs()
    return cfg


def resolve_marker(cfg: Config, target: str) -> Marker:
    vid = target
    if target.startswith("http"):
        vid = fetch.info(target)["id"]
    path = cfg.marker_path(vid)
    if not path.exists():
        raise ToolError(f"마커가 없습니다: {path}\n먼저 `python analyze.py analyze <URL>` 을 실행하세요.")
    return Marker.load(path)


# ---------------------------------------------------------------- commands
def cmd_doctor(args: argparse.Namespace) -> int:
    """실행 전 환경 점검.

    기본 경로(성문 검출)에 필요한 것만 '필수'로 본다. 화자 분리(torch · pyannote ·
    HF 토큰)는 성문이 없을 때만 쓰는 느린 폴백이라, 없어도 실패로 치지 않는다.
    (README "화자 분리로 되돌리지 말 것" 참조)
    """
    ok = True

    def _has(mod: str) -> bool:
        try:
            __import__(mod)
            return True
        except ImportError:
            return False

    print("=== 외부 실행파일 ===")
    for name, path in check_tools().items():
        mark = "OK " if path else "없음"
        print(f"  [{mark}] {name:<8} {path or ''}")
        ok = ok and bool(path)

    print("\n=== 파이썬 패키지 (필수) ===")
    for mod, hint in [
        ("numpy", "pip install numpy"),
        ("sherpa_onnx", "pip install sherpa-onnx"),
        ("yt_dlp", "pip install -U yt-dlp"),
        ("PySide6", "pip install PySide6   (GUI 를 쓸 때만)"),
    ]:
        if _has(mod):
            print(f"  [OK ] {mod}")
        else:
            print(f"  [없음] {mod:<16} → {hint}")
            ok = ok if mod == "PySide6" else False

    print("\n=== 성문 검출 (기본 방식) ===")
    cfg = build_config(args)
    from core.voiceprint import EMBED_MODEL, MODEL_DIR_CANDIDATES, find_model
    try:
        model = find_model()
        print(f"  [OK ] {EMBED_MODEL:<16} {model}")
    except ToolError:
        print(f"  [없음] {EMBED_MODEL:<16} → {MODEL_DIR_CANDIDATES[0]} 에 넣으세요")
        ok = False
    vp = cfg.voiceprint_path
    if vp.exists():
        print(f"  [OK ] voiceprint.npz   {vp}")
    else:
        print(f"  [없음] voiceprint.npz   → python enroll.py build  (목사님 성문 만들기)")
        ok = False

    print("\n=== 선택 기능 (없어도 됩니다) ===")
    print(f"  [{'OK ' if _has('faster_whisper') else '  -'}] faster_whisper   자막 만들기"
          + ("" if _has("faster_whisper") else "  → pip install faster-whisper"))
    fallback = _has("torch") and _has("pyannote.audio")
    print(f"  [{'OK ' if fallback else '  -'}] torch + pyannote 화자 분리 폴백 (성문이 있으면 쓰지 않음)")
    from core.config import read_hf_token
    token = read_hf_token()
    print(f"  [{'OK ' if token else '  -'}] HF_TOKEN         화자 분리 폴백에만 필요"
          + (f"  ({token[:6]}...)" if token else ""))
    if _has("torch"):
        try:
            import torch
            cuda = torch.cuda.is_available()
            print(f"  [{'OK ' if cuda else '  -'}] CUDA             "
                  + (torch.cuda.get_device_name(0) if cuda else "사용 불가 (CPU 로 동작)"))
        except Exception:
            pass

    print("\n" + ("모두 준비됐습니다." if ok else "위의 '없음' 항목을 먼저 해결하세요."))
    return 0 if ok else 1


def cmd_list(args: argparse.Namespace) -> int:
    for v in fetch.channel_videos(args.channel, args.limit):
        dur = f"{v['duration']/60:.0f}분" if v["duration"] else "?"
        print(f"{v['id']}  {dur:>6}  {v['title']}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    cfg = build_config(args)

    print("[1/5] 영상 정보 조회")
    meta = fetch.info(args.url)
    vid = meta["id"]
    print(f"      {meta['title']}  ({meta['duration']/60:.0f}분)")

    print("[2/5] 분석용 오디오 다운로드 (16kHz 모노)")
    wav = cfg.audio_path(vid)
    fetch.download_audio(args.url, wav)
    duration = meta["duration"] or ffprobe_duration(wav)

    print("[3/5] 화자 분리 — CPU에서는 오래 걸립니다. 결과는 캐시됩니다.")
    segments = diarize(wav, cfg, cache_path=cfg.diar_path(vid), force=args.force)

    print("[4/5] 설교 / 축도 구간 판정")
    det = detect(segments, duration, cfg)
    print(format_detection(det))

    marker = Marker(
        video_id=vid,
        url=meta["webpage_url"],
        title=meta["title"],
        upload_date=meta["upload_date"],
        duration=duration,
        sermon_ai_start=det.sermon.start,
        sermon_ai_end=det.sermon.end,
        bene_ai_start=det.benediction.start if det.benediction else None,
        bene_ai_end=det.benediction.end if det.benediction else None,
        confidence=det.confidence,
        bene_confidence=det.bene_confidence,
        reasons=det.reasons,
        pastor_speaker=det.pastor,
        pastor_ratio=round(det.pastor_ratio, 3),
    )

    if args.no_stt:
        print("[5/5] STT 생략 (--no-stt)")
    else:
        print(f"[5/5] 확정 구간만 STT (모델 {cfg.whisper_model}, {cfg.device}/{cfg.compute_type})")
        transcript: list[dict] = []
        for start, end, label in marker.ranges():
            print(f"      {label}: {hms(start)} ~ {hms(end)} 전사 중...")
            transcript.extend(transcribe.transcribe_range(wav, start, end, cfg, label))
        transcribe.save_transcript(cfg.transcript_path(vid), transcript)

        if transcript and not args.no_refine:
            notes: list[str] = []
            r = marker.sermon_range()
            if r:
                s, e, n = refine_sermon(transcript, r[0], r[1], cfg)
                marker.sermon_ai_start, marker.sermon_ai_end = s, e
                notes.extend(n)
            r = marker.bene_range()
            if r:
                s, e, n = refine_benediction(transcript, r[0], r[1], cfg)
                marker.bene_ai_start, marker.bene_ai_end = s, e
                notes.extend(n)
            marker.refined = True
            marker.reasons.extend(notes)
            for n in notes:
                print(f"      · {n}")

    path = marker.save(cfg.marker_path(vid))
    print(f"\n마커 저장: {path}")
    print("확인 후 필요하면 `python analyze.py set` 으로 수정하고, `export` 로 내보내세요.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    m = resolve_marker(cfg, args.target)
    print(f"{m.title}  [{m.video_id}]  {m.upload_date}")
    for start, end, label in m.ranges():
        print(f"  {label:<12} {hms(start)} ~ {hms(end)}  ({(end-start)/60:.1f}분)")
    print(f"  신뢰도  설교 {m.confidence} / 축도 {m.bene_confidence}"
          f"{'  (사람 수정됨)' if m.corrected else ''}")
    for r in m.reasons:
        print(f"  · {r}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    m = resolve_marker(cfg, args.target)
    if args.sermon:
        m.sermon_human_start, m.sermon_human_end = parse_range(args.sermon)
        m.corrected = True
    if args.bene:
        m.bene_human_start, m.bene_human_end = parse_range(args.bene)
        m.corrected = True
    if args.no_bene:
        m.bene_ai_start = m.bene_ai_end = None
        m.bene_human_start = m.bene_human_end = None
        m.corrected = True
    m.save(cfg.marker_path(m.video_id))
    return cmd_show(args)


def cmd_export(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    m = resolve_marker(cfg, args.target)
    print(f"내보내기: {m.title}")
    for start, end, label in m.ranges():
        print(f"  {label:<12} {hms(start)} ~ {hms(end)}")
    transcript = transcribe.load_transcript(cfg.transcript_path(m.video_id))
    result = export_mod.export(m, cfg, transcript, keep_parts=args.keep_parts)
    print("\n완료")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    rc = cmd_analyze(args)
    if rc != 0:
        return rc
    args.target = args.url
    return cmd_export(args)


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="예배영상에서 설교와 축도만 뽑아 하나로 잇는다")
    ap.add_argument("--work-dir", help="작업 폴더 (기본: ./data)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--gap", type=float, help="블록 병합 간격(초). 기본 90")
        p.add_argument("--model", help="Whisper 모델. 기본 medium (CPU)")
        p.add_argument("--device", choices=["cpu", "cuda"], help="기본 cpu")

    p = sub.add_parser("doctor", help="설치 상태 점검")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("list", help="채널 최근 영상 목록")
    p.add_argument("channel")
    p.add_argument("-n", "--limit", type=int, default=10)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("analyze", help="구간 검출 + 마커 저장")
    p.add_argument("url")
    p.add_argument("--no-stt", action="store_true", help="STT 없이 화자분리 결과만")
    p.add_argument("--no-refine", action="store_true", help="자막 기반 경계보정 생략")
    p.add_argument("--force", action="store_true", help="화자분리 캐시 무시")
    common(p)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("show", help="저장된 마커 확인")
    p.add_argument("target", help="video_id 또는 URL")
    common(p)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("set", help="사람이 구간 수정")
    p.add_argument("target")
    p.add_argument("--sermon", help="예: 32:18-1:09:44")
    p.add_argument("--bene", help="예: 1:15:20-1:16:50")
    p.add_argument("--no-bene", action="store_true", help="축도 제외")
    common(p)
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("export", help="확정 구간 내보내기")
    p.add_argument("target")
    p.add_argument("--keep-parts", action="store_true", help="구간별 원본 파일 보존")
    common(p)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("run", help="analyze + export")
    p.add_argument("url")
    p.add_argument("--no-stt", action="store_true")
    p.add_argument("--no-refine", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--keep-parts", action="store_true")
    common(p)
    p.set_defaults(func=cmd_run)

    return ap


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ToolError as exc:
        print(f"\n[오류] {exc}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\n중단했습니다.", file=sys.stderr)
        raise SystemExit(130)
