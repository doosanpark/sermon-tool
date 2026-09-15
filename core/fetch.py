"""yt-dlp 래퍼.

분석용(오디오만)과 내보내기용(확정 구간만)을 분리한다. 2시간 영상 전체를 받으면
2~3GB지만, 오디오만 받으면 수십 MB고 구간만 받으면 37분치로 끝난다.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .util import ToolError, has_audio, hms_ms, require, run, run_stream


def _yt() -> str:
    return require("yt-dlp")


def info(url: str) -> dict:
    """영상 메타데이터(id, title, duration, upload_date)."""
    out = run([_yt(), "-J", "--no-playlist", "--skip-download", url])
    data = json.loads(out)
    return {
        "id": data.get("id", ""),
        "title": data.get("title", ""),
        "duration": float(data.get("duration") or 0.0),
        "upload_date": data.get("upload_date", ""),
        "webpage_url": data.get("webpage_url", url),
        "live_status": data.get("live_status") or ("is_live" if data.get("is_live") else ""),
    }


def channel_videos(channel_url: str, limit: int = 10) -> list[dict]:
    """채널/재생목록의 최신 영상 목록 (다운로드 없음)."""
    out = run([
        _yt(), "-J", "--flat-playlist",
        "--playlist-end", str(limit),
        channel_url,
    ])
    data = json.loads(out)
    items = []
    for e in data.get("entries") or []:
        if not e:
            continue
        items.append({
            "id": e.get("id", ""),
            "title": e.get("title", ""),
            "duration": float(e.get("duration") or 0.0),
            "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id','')}",
        })
    return items


def download_audio(url: str, out_path: Path, overwrite: bool = False) -> Path:
    """분석용 16kHz 모노 wav. 이미 있으면 건너뛴다."""
    out_path = Path(out_path)
    if out_path.exists() and not overwrite:
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmpl = str(Path(tmp) / "src.%(ext)s")
        run_stream([
            _yt(), "-f", "bestaudio/best", "--no-playlist",
            "--no-part", "--retries", "5", "--fragment-retries", "5",
            "--newline",
            "-o", tmpl, url,
        ], tag="[yt-dlp]")
        downloaded = sorted(Path(tmp).glob("src.*"))
        if not downloaded:
            raise ToolError("오디오 다운로드 결과 파일을 찾지 못했습니다.")
        print("  16kHz 모노 wav 로 변환 중...")
        run_stream([
            require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-stats",
            "-i", str(downloaded[0]),
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(out_path),
        ], tag="[ffmpeg] ")
    return out_path


def download_section(url: str, start: float, end: float, out_path: Path) -> Path:
    """확정된 구간만 영상으로 받는다. 컷 지점에 키프레임을 강제해 앞부분이 깨지지 않게 한다."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    run_stream([
        _yt(), "--newline",
        "--download-sections", f"*{hms_ms(start)}-{hms_ms(end)}",
        "--force-keyframes-at-cuts",
        "-f", "bv*+ba/best",
        "--merge-output-format", "mp4",
        "--no-playlist", "--no-part",
        "--retries", "5", "--fragment-retries", "5",
        "-o", str(out_path.with_suffix("")) + ".%(ext)s",
        url,
    ], tag="[yt-dlp]")

    if out_path.exists():
        return out_path
    produced = sorted(out_path.parent.glob(out_path.stem + ".*"))
    if not produced:
        raise ToolError(f"구간 다운로드 결과 파일이 없습니다: {out_path}")
    produced[0].rename(out_path)
    return out_path

def download_preview(url: str, out_path: Path, height: int | None = 360) -> Path:
    """검수용 영상. height=None 이면 원본 최고화질.

    라이브 다시보기처럼 저화질 포맷이 없는 영상이 있어서, 요청한 화질이 실패하면
    한 단계씩 올려가며 재시도한다.
    """
    out_path = Path(out_path)
    if out_path.exists():
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    specs: list[str] = []
    if height:
        specs.append(f"bv*[height<={height}]+ba/b[height<={height}]")
        specs.append(f"bv*[height<={height * 2}]+ba/b[height<={height * 2}]")
    specs.append("bv*+ba/b")
    specs.append("best")

    last: Exception | None = None
    for spec in specs:
        try:
            print(f"  포맷 시도: {spec}")
            print("  유튜브는 화면과 소리를 따로 줍니다 — 화면을 다 받은 뒤")
            print("  소리를 받고, 마지막에 둘을 하나로 합칩니다.")
            print("  합치는 동안은 진행률이 나오지 않습니다 (보통 1~2분).")
            run_stream([
                _yt(), "--newline",
                "-f", spec,
                "--merge-output-format", "mp4",
                "--no-playlist", "--no-part",
                "--retries", "5", "--fragment-retries", "5",
                "-o", str(out_path.with_suffix("")) + ".%(ext)s",
                url,
            ], tag="[yt-dlp]")

            produced = out_path if out_path.exists() else next(
                iter(sorted(out_path.parent.glob(out_path.stem + ".*"))), None)
            if produced and not has_audio(produced):
                # 영상만 받아진 반쪽 파일. 지우고 다음 포맷으로 넘어간다.
                print("  소리가 없는 파일이 받아졌습니다 — 지우고 다른 포맷으로 재시도")
                produced.unlink(missing_ok=True)
                last = ToolError("오디오 없는 결과")
                continue
            break
        except ToolError as exc:
            last = exc
            print("  실패 — 다음 포맷으로 재시도")

    if out_path.exists():
        return out_path
    produced = sorted(out_path.parent.glob(out_path.stem + ".*"))
    if produced:
        produced[0].rename(out_path)
        return out_path

    print("  모든 포맷이 실패했습니다. 이 영상에서 받을 수 있는 포맷 목록:")
    try:
        run_stream([_yt(), "-F", "--no-playlist", url], tag="[formats]")
    except ToolError:
        pass
    raise ToolError(
        "영상 다운로드에 실패했습니다.\n"
        "위 포맷 목록을 확인하세요. 목록이 HLS(m3u8)뿐이라면 yt-dlp 가 JS 런타임을\n"
        "찾지 못한 것입니다. bin\\deno.exe 가 있는지 확인하고, 없으면\n"
        "  https://github.com/denoland/deno/releases  에서 받아 bin\\ 에 넣으세요.\n"
        f"\n마지막 오류: {last}")
