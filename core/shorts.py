"""고른 문장들을 세로(9:16) 요약 영상으로 만든다.

왜 잘라내기가 아니라 짜맞추기인가:
    원본은 고정 카메라 한 대로 찍은 16:9 다. 9:16 으로 가운데를 잘라내면
    폭이 원본의 56% 만 남아 오른쪽 설교 제목 화면과 왼쪽 십자가가 통째로
    날아간다. 그래서 목사님만 크게 잡아 가운데 두고, 제목은 화면에서
    오려내는 대신 `marker.sermon_title` 에 적힌 글자로 위에 얹는다.

화면 나누기 (1080x1920):
      0 ~  330   제목과 본문
    330 ~ 1470   영상 (1080x1140)
   1470 ~ 1920   자막

크롭 자리는 비율로 잡는다. 원본이 720p 로 받아지든 1080p 로 받아지든 같은
구도가 나와야 하기 때문이다. 고정 카메라라 한 번 맞춰 두면 계속 쓴다.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from .util import ToolError, ffprobe_duration, require, run_stream
from . import cut as cut_mod

WIDTH, HEIGHT = 1080, 1920
HEAD_H = 330                 # 제목 자리 높이
VIDEO_H = 1140               # 영상 높이
BG = "0x0E1513"              # 위아래 여백 색 — 짙은 무채색이 화면을 안 뺏는다
FADE = 0.25                  # 조각 이음매의 소리 페이드(초)

# 한글이 나오는 굵은 글꼴. 앞에 있는 것부터 찾는다.
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\malgunbd.ttf",
    r"C:\Windows\Fonts\malgun.ttf",
    r"C:\Windows\Fonts\NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
]


def find_font() -> Path | None:
    for c in FONT_CANDIDATES:
        p = Path(c)
        if p.exists():
            return p
    return None


def crop_rect(src_w: int, src_h: int, cx: float, cy: float, zoom: float
              ) -> tuple[int, int, int, int]:
    """비율로 적힌 크롭 자리를 실제 픽셀로 바꾼다.

    cx, cy 는 목사님이 계신 자리(0~1). zoom 은 원본 높이의 몇 배를 담을지로,
    작을수록 크게 잡힌다. 화면 밖으로 나가지 않게 안쪽으로 밀어 넣는다.
    """
    aspect = WIDTH / VIDEO_H
    h = max(64, min(src_h, int(src_h * zoom)))
    w = int(round(h * aspect))
    if w > src_w:                      # 원본이 좁으면 높이를 줄여 맞춘다
        w = src_w
        h = int(round(w / aspect))
    x = int(round(src_w * cx - w / 2))
    y = int(round(src_h * cy - h / 2))
    x = max(0, min(src_w - w, x))
    y = max(0, min(src_h - h, y))
    return w, h, x, y


def _compose_graph(runs: list[tuple[float, float]], w: int, h: int, x: int, y: int,
                   fps: float, audio_chain: str) -> str:
    """조각마다 잘라 세로로 맞추고 이어붙이는 필터 그래프."""
    parts, labels = [], []
    for i, (start, end) in enumerate(runs):
        dur = end - start
        fo = max(0.0, dur - FADE)
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
            f"crop={w}:{h}:{x}:{y},scale={WIDTH}:{VIDEO_H},"
            f"fps={fps:.3f},setsar=1[v{i}]"
        )
        parts.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={FADE},afade=t=out:st={fo:.3f}:d={FADE}[a{i}]"
        )
        labels.append(f"[v{i}][a{i}]")

    graph = ";".join(parts) + ";" + "".join(labels)
    graph += f"concat=n={len(runs)}:v=1:a=1[vc][araw]"
    graph += (f";[vc]pad={WIDTH}:{HEIGHT}:0:{HEAD_H}:color={BG},"
              f"format=yuv420p[vpad]")
    graph += f";[araw]{audio_chain}[a]" if audio_chain else ";[araw]anull[a]"
    return graph


def _title_filters(font: Path | None, title: str, verse: str) -> str:
    """제목과 본문 글자. 글꼴을 못 찾으면 조용히 건너뛴다."""
    if not font or not (title or verse):
        return ""
    out = []
    if title:
        out.append("drawtext=fontfile=title.ttf:textfile=title.txt:"
                   "fontcolor=white:fontsize=72:x=(w-tw)/2:y=118")
    if verse:
        out.append("drawtext=fontfile=title.ttf:textfile=verse.txt:"
                   "fontcolor=0x8FC9BC:fontsize=40:x=(w-tw)/2:y=224")
    return "," + ",".join(out)


def _ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):d}:{int(m):02d}:{s:05.2f}"


_SRT_TIME = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def srt_to_ass(srt_text: str, font: str, size: int, margin_v: int = 205) -> str:
    """자막을 ASS 로 바꿔 화면 크기를 못 박는다.

    ffmpeg 의 subtitles 필터에 .srt 를 그냥 주면 libass 가 PlayResY 를 288 로
    가정하고 영상 높이(1920)에 맞춰 6.7배로 늘린다. 그러면 FontSize 56 이
    370픽셀이 되고 MarginV 170 이 1130 이 되어, 자막이 화면 위쪽에 거대하게 뜬다.
    ASS 로 만들어 PlayResX/Y 를 실제 크기로 적어 두면 숫자가 픽셀 그대로 먹는다.
    """
    def to_sec(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000.0

    head = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        f"PlayResX: {WIDTH}\n"
        f"PlayResY: {HEIGHT}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        f"-1,0,0,0,100,100,0,0,1,4,0,2,70,70,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = []
    for block in re.split(r"\n\s*\n", srt_text.strip()):
        rows = [r for r in block.splitlines() if r.strip()]
        if len(rows) < 2:
            continue
        hit = None
        body_at = 0
        for i, r in enumerate(rows[:2]):
            hit = _SRT_TIME.search(r)
            if hit:
                body_at = i + 1
                break
        if not hit:
            continue
        a = to_sec(*hit.group(1, 2, 3, 4))
        b = to_sec(*hit.group(5, 6, 7, 8))
        text = "\\N".join(r.strip() for r in rows[body_at:])
        text = text.replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Default,,0,0,0,,{text}")
    return head + "\n".join(lines) + "\n"


def build(src: Path, runs: list[tuple[float, float]], out_path: Path, cfg,
          title: str = "", verse: str = "", srt_text: str = "") -> Path:
    """세로 요약 영상을 만든다. 조각 자르기·이어붙이기·자막을 한 번에 한다."""
    src, out_path = Path(src), Path(out_path)
    if not src.exists():
        raise ToolError(f"원본 영상이 없습니다: {src}")
    if not runs:
        raise ToolError("고른 문장이 없습니다. 문장을 몇 개 골라 주세요.")

    bad = [(s, e) for s, e in runs if e - s <= 0.4]
    if bad:
        raise ToolError(f"길이가 0에 가까운 구간이 있습니다: {bad[:3]}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    params = cut_mod.video_params(src)
    w, h, x, y = crop_rect(params["width"], params["height"],
                           cfg.shorts_center_x, cfg.shorts_center_y, cfg.shorts_zoom)
    total = sum(e - s for s, e in runs)
    print(f"세로 요약 영상을 만듭니다 — 조각 {len(runs)}개, {total:.0f}초")
    print(f"  원본 {params['width']}x{params['height']} 에서 {w}x{h} 를 잘라 "
          f"{WIDTH}x{HEIGHT} 로 만듭니다")

    font = find_font()
    if not font:
        print("  한글 글꼴을 찾지 못해 제목 글자는 건너뜁니다")

    chain = cut_mod.audio_chain(cfg.denoise, cfg.normalize_audio,
                                cfg.target_lufs, cfg.target_peak_db)

    # ffmpeg 필터에 윈도우 절대경로를 넣으면 ':' 와 '\' 가 문법으로 읽혀 깨진다.
    # 작업 폴더를 임시 폴더로 옮기고 파일 이름만 넘긴다.
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        if font:
            shutil.copyfile(font, tmpdir / "title.ttf")
            (tmpdir / "title.txt").write_text(title or "", encoding="utf-8")
            (tmpdir / "verse.txt").write_text(verse or "", encoding="utf-8")

        graph = _compose_graph(runs, w, h, x, y, params["fps"], chain)

        vf_extra = _title_filters(font, title, verse)
        if srt_text.strip():
            ass = srt_to_ass(srt_text, cfg.subtitle_font, cfg.shorts_sub_size)
            (tmpdir / "sub.ass").write_text(ass, encoding="utf-8")
            vf_extra += ",subtitles=sub.ass"

        if vf_extra:
            graph = graph.replace("[vpad]", "[vp0]") + f";[vp0]{vf_extra.lstrip(',')}[vout]"
            vmap = "[vout]"
        else:
            vmap = "[vpad]"

        stage = tmpdir / "out.mp4"
        run_stream([
            require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-stats",
            "-i", str(src.resolve()),
            "-filter_complex", graph,
            "-map", vmap, "-map", "[a]",
            "-c:v", "libx264", "-preset", cfg.shorts_preset, "-crf", str(cfg.shorts_crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", "out.mp4",
        ], tag="[ffmpeg] ", total=total, cwd=tmpdir)

        # 지웠다 옮기지 않는다. 윈도우는 열려 있는 파일을 못 지우고(재생기에서
        # 보고 있으면 흔한 일이다), 지운 뒤 옮기다 실패하면 결과물이 사라진다.
        # 같은 폴더에 임시 이름으로 옮겨 두고 os.replace 로 한 번에 갈아 끼운다.
        near = out_path.with_name(out_path.name + ".new")
        shutil.copyfile(stage, near)
        try:
            os.replace(near, out_path)
        except OSError as exc:
            raise ToolError(
                f"만든 영상을 제자리에 놓지 못했습니다: {out_path.name}\n"
                f"  {exc}\n"
                "재생기에서 그 파일을 열어 두었다면 닫고 다시 만들어 주세요.\n"
                f"  새로 만든 것은 여기 있습니다: {near}") from exc

    made = ffprobe_duration(out_path)
    print(f"  완성 — {int(made // 60)}분 {int(made % 60):02d}초, "
          f"{out_path.stat().st_size / (1024 * 1024):.1f}MB")
    return out_path
