#!/usr/bin/env python3
"""목사님 성문 만들기와 정확도 검증.

  python enroll.py build                       data/export 의 설교 영상으로 성문 생성
  python enroll.py build a.mp4 b.mp4           파일을 직접 지정
  python enroll.py test <영상> [--marker x.json]  전체 영상에서 설교를 찾아 정답과 비교
  python enroll.py info                        만들어진 성문 정보

성문은 data/voiceprint.npz 에 저장된다 (수 KB).
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from core.config import Config
from core.marker import Marker
from core.util import ToolError, hms
from core.voiceprint import (Embedder, Voiceprint, detect, enroll, read_wav,
                             scan, smooth, to_wav)


def vp_path(cfg: Config) -> Path:
    return cfg.work_dir / "voiceprint.npz"


def media_to_audio(media: Path, tmpdir: Path) -> Path:
    out = tmpdir / (media.stem + ".wav")
    if media.suffix.lower() == ".wav":
        return media
    to_wav(media, out)
    return out


def cmd_build(args, cfg: Config) -> int:
    files = [Path(f) for f in args.files] if args.files else sorted(cfg.export_dir.glob("*.mp4"))
    if not files:
        raise ToolError(f"설교 영상이 없습니다. 먼저 GUI 로 잘라 두세요: {cfg.export_dir}")

    print(f"성문 재료 {len(files)}편")
    for f in files:
        print(f"  {f.name}")

    embedder = Embedder(threads=args.threads)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        print("\n오디오 추출 + 표본 수집")
        wavs = [media_to_audio(f, tmpdir) for f in files]
        started = time.time()
        vp = enroll(wavs, embedder, window=args.window, hop=args.hop)
    print(f"  걸린 시간 {time.time()-started:.0f}초")

    path = vp.save(vp_path(cfg))
    print(f"\n성문 저장: {path}  ({path.stat().st_size/1024:.0f} KB)")
    print(f"  차원 {len(vp.vector)}  표본 {vp.sample_count}개  출처 {', '.join(vp.sources)}")
    return 0


def cmd_test(args, cfg: Config) -> int:
    vp = Voiceprint.load(vp_path(cfg))
    media = Path(args.media)
    if not media.exists():
        raise ToolError(f"파일이 없습니다: {media}")

    embedder = Embedder(threads=args.threads)
    with tempfile.TemporaryDirectory() as tmp:
        wav = media_to_audio(media, Path(tmp))
        audio = read_wav(wav)
        total = len(audio) / 16000
        print(f"{media.name}  전체 {total/60:.0f}분")
        started = time.time()
        times, scores = scan(audio, vp, embedder, hop=args.hop)
        elapsed = time.time() - started
    print(f"훑는 데 {elapsed:.0f}초 (실시간의 {total/max(elapsed,0.1):.0f}배)")

    scores = smooth(scores, args.smooth)
    blocks = detect(times, scores, threshold=args.threshold,
                    gap=args.gap, min_len=args.min_len)

    print(f"\n찾은 구간 (임계값 {args.threshold})")
    for i, (s, e) in enumerate(blocks[:5], 1):
        print(f"  {i}. {hms(s)} ~ {hms(e)}  ({(e-s)/60:.1f}분)")
    if not blocks:
        print("  없음 — 임계값을 낮춰 보세요")

    marker_path = Path(args.marker) if args.marker else cfg.marker_path(media.stem)
    if marker_path.exists() and blocks:
        m = Marker.load(marker_path)
        truth = m.sermon_range()
        if truth:
            got = blocks[0]
            ds, de = got[0] - truth[0], got[1] - truth[1]
            overlap = max(0.0, min(got[1], truth[1]) - max(got[0], truth[0]))
            union = max(got[1], truth[1]) - min(got[0], truth[0])
            print("\n=== 정답과 비교 ===")
            print(f"  정답  {hms(truth[0])} ~ {hms(truth[1])}  ({(truth[1]-truth[0])/60:.1f}분)")
            print(f"  검출  {hms(got[0])} ~ {hms(got[1])}  ({(got[1]-got[0])/60:.1f}분)")
            print(f"  시작 오차 {ds:+.0f}초   끝 오차 {de:+.0f}초")
            print(f"  겹침 비율(IoU) {overlap/union*100:.1f}%")
            verdict = "쓸 만함" if abs(ds) <= 60 and abs(de) <= 60 else "보정 필요"
            print(f"  판정: {verdict}")
    return 0


def cmd_info(args, cfg: Config) -> int:
    path = vp_path(cfg)
    if not path.exists():
        print("아직 성문이 없습니다.  python enroll.py build")
        return 1
    vp = Voiceprint.load(path)
    print(f"성문: {path}")
    print(f"  모델 {vp.model}  차원 {len(vp.vector)}  창 {vp.window}초")
    print(f"  표본 {vp.sample_count}개")
    print(f"  출처 {', '.join(vp.sources)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="목사님 성문 등록과 검증")
    ap.add_argument("--threads", type=int, default=4)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="설교 영상으로 성문 만들기")
    p.add_argument("files", nargs="*", help="비우면 data/export/*.mp4 전부")
    p.add_argument("--window", type=float, default=3.0)
    p.add_argument("--hop", type=float, default=3.0)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("test", help="전체 영상에서 설교 찾아 정답과 비교")
    p.add_argument("media")
    p.add_argument("--marker", help="정답 JSON. 없으면 video_id 로 자동 탐색")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--hop", type=float, default=1.5)
    p.add_argument("--gap", type=float, default=90.0)
    p.add_argument("--min-len", type=float, default=600.0)
    p.add_argument("--smooth", type=int, default=7)
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("info", help="성문 정보")
    p.set_defaults(func=cmd_info)

    args = ap.parse_args()
    cfg = Config()
    cfg.ensure_dirs()
    return args.func(args, cfg)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ToolError as exc:
        print(f"\n[오류] {exc}", file=sys.stderr)
        raise SystemExit(2)
