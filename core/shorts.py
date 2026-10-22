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

from .util import (ToolError, ffprobe_duration, is_memory_error,
                   require, run_stream)
from . import cut as cut_mod
from . import levels, machine

WIDTH, HEIGHT = 1080, 1920
HEAD_H = 330                 # 제목 자리 높이
VIDEO_H = 1140               # 영상 높이
BG = "0x0E1513"              # 위아래 여백 색 — 짙은 무채색이 화면을 안 뺏는다
# 조각 이음매의 소리 페이드(초). 이음매의 '툭' 소리만 없애면 되므로 짧아야 한다.
# 0.25 였을 때 말꼬리의 여운이 통째로 깎여 문장 끝이 부자연스러웠다
# (2026-10-20). 구간 끝은 말이 끝나고 0.3초 뒤라, 0.25초를 깎으면 그 여운이
# 거의 다 사라진다. 30밀리초면 '툭' 은 안 나고 여운은 그대로 남는다.
FADE = 0.03

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
                   fps: float, audio_chain: str, denoise: str = "",
                   scaler: str = "", sharpen: float = 0.0,
                   region: bool = False, asrc: str = "0:a",
                   gains: list[float] | None = None) -> str:
    """조각마다 잘라 세로로 맞추고 이어붙이는 필터 그래프.

    확대 화질: 720p 원본에서 350x370 쯤을 잘라 1080x1140 으로 약 3배 키운다.
    그냥 키우면 원본(0.5Mbps 라이브 재인코딩)의 압축 블록까지 3배로 커진다.
    그래서 순서가 정해져 있다. 작을 때 블록을 살짝 뭉개고(denoise, 픽셀이 적어
    싸다) → lanczos 로 키우고(윤곽 보존) → 키운 뒤 윤곽만 되살린다(cas).
    없는 디테일을 만들지는 못한다. 안경테·태블릿 모서리·나무결이 조금 사는 정도다.
    """
    scale = f"scale={WIDTH}:{VIDEO_H}" + (f":flags={scaler}" if scaler else "")
    post = f"cas={sharpen:.2f}" if sharpen > 0 else ""
    if region:
        # 두 번째 입력([1:v])이 이미 잘라 키워 둔 조각이다 (AI 초해상 등). 잡음 완화와
        # 선명화는 그쪽에서 끝났으므로 크기만 맞춘다. 소리는 여전히 원본([0:a])에서 온다.
        vin, vchain = "[1:v]", f"scale={WIDTH}:{VIDEO_H}:flags=lanczos"
    else:
        vin, vchain = "[0:v]", ",".join(
            p for p in (f"crop={w}:{h}:{x}:{y}", denoise, scale, post) if p)
    parts, labels = [], []
    for i, (start, end) in enumerate(runs):
        dur = end - start
        fo = max(0.0, dur - FADE)
        parts.append(
            f"{vin}trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
            f"{vchain},"
            f"fps={fps:.3f},setsar=1[v{i}]"
        )
        # 말소리 크기를 고르게 펴는 이득을 먼저 건다 (core/levels.py).
        # 차이의 7할만 좁히므로 힘주어 말한 대목의 결은 남는다.
        vol = levels.volume_filter(gains[i]) if gains and i < len(gains) else ""
        parts.append(
            f"[{asrc}]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
            + (vol + "," if vol else "")
            + f"afade=t=in:st=0:d={FADE},afade=t=out:st={fo:.3f}:d={FADE}[a{i}]"
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
          title: str = "", verse: str = "", srt_text: str = "",
          region: Path | None = None, audio_src: Path | None = None,
          level_wav: Path | None = None) -> Path:
    """세로 요약 영상을 만든다. 조각 자르기·이어붙이기·자막을 한 번에 한다.

    region 은 선택이다. 원본과 시간이 맞는, 목사님 자리(crop_rect)만 미리 잘라
    AI 초해상 등으로 키워 둔 영상을 주면 그 화면을 쓴다. 해상도는 무엇이든 된다.
    소리·구간·자막·제목은 그대로 원본 기준으로 처리한다. 교회 PC 에는 GPU 가
    없으므로 기본 경로에서는 쓰지 않는다. 실측(2026-09-15, RTX 4060 Ti):
    Real-ESRGAN x4plus 1.8fps, animevideov3 12fps, FlashVSR 0.05fps(8GB 로는 부족).
    """
    src, out_path = Path(src), Path(out_path)
    if not src.exists():
        raise ToolError(f"원본 영상이 없습니다: {src}")
    if region is not None:
        region = Path(region)
        if not region.exists():
            raise ToolError(f"미리 키워 둔 조각 영상이 없습니다: {region}")
    if not runs:
        raise ToolError("고른 문장이 없습니다. 문장을 몇 개 골라 주세요.")

    bad = [(s, e) for s, e in runs if e - s <= 0.4]
    if bad:
        raise ToolError(f"길이가 0에 가까운 구간이 있습니다: {bad[:3]}")

    # 메모리를 먼저 본다. 몇 분 돌다 ffmpeg 가 "Cannot allocate memory" 로
    # 터지는 것보다, 시작 전에 사정을 말해 주는 쪽이 낫다 (2026-10-20).
    ok, note = machine.export_check(WIDTH, HEIGHT, len(runs))
    if note:
        for line in note.split("\n"):
            print(("  [메모리] " if line else "") + line, flush=True)
    if not ok:
        raise ToolError(note)

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

    # 말소리 크기를 재어 고르게 편다. 조각끼리는 물론 **조각 안에서도** 문장마다
    # 잰다 — 한 조각(34초) 안에서 문장끼리 9.3dB 차이가 난 적이 있다
    # (core/levels.py). 쉼 한가운데서 바뀌므로 바뀌는 순간은 들리지 않는다.
    gains = levels.plan_gains(audio_src or level_wav, runs,
                              say=lambda t: print(f"  {t}", flush=True))

    # 소리 다듬기는 조각마다, 음량 맞추기는 이어붙인 뒤 한 번만 (cut.audio_chain_split).
    chain, final_af = cut_mod.audio_chain_split(
        cfg.denoise, cfg.normalize_audio, cfg.target_lufs, cfg.target_peak_db,
        bool(getattr(cfg, "enhance_speech", False)))
    # 소리를 어느 입력에서 가져오나. 입력 순서는 원본 → (조각) → (울림 줄인 wav).
    asrc = "0:a"
    if audio_src is not None:
        asrc = f"{2 if region is not None else 1}:a"

    # ffmpeg 필터에 윈도우 절대경로를 넣으면 ':' 와 '\' 가 문법으로 읽혀 깨진다.
    # 작업 폴더를 임시 폴더로 옮기고 파일 이름만 넘긴다.
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        if font:
            shutil.copyfile(font, tmpdir / "title.ttf")
            (tmpdir / "title.txt").write_text(title or "", encoding="utf-8")
            (tmpdir / "verse.txt").write_text(verse or "", encoding="utf-8")

        denoise = str(getattr(cfg, "shorts_denoise", "") or "")
        scaler = str(getattr(cfg, "shorts_scaler", "") or "")
        sharpen = float(getattr(cfg, "shorts_sharpen", 0.0) or 0.0)
        if region is not None:
            rp = cut_mod.video_params(region)
            print(f"  미리 키워 둔 조각을 씁니다: {region.name} ({rp['width']}x{rp['height']})")
        else:
            steps = [name for name, on in (("키우기 전 잡음 완화", denoise),
                                           (f"{scaler} 확대", scaler),
                                           (f"윤곽 되살리기 {sharpen:.1f}", sharpen > 0)) if on]
            if steps:
                print("  확대 화질: " + " → ".join(steps))
        # 스레드마다 화면 버퍼를 잡고, 조각마다 원본을 따로 물고 있다. 메모리가
        # 빠듯하면 **화질을 깎는 대신 조각을 나눠** 여러 번 만들고 이어붙인다
        # (2026-10-20 사용자 뜻). 이어붙일 때는 화면을 그대로 복사한다.
        _threads = machine.ffmpeg_threads()
        _preset, _why = machine.x264_preset(cfg.shorts_preset)
        if _why:
            print(f"  [영상] {_why}", flush=True)

        per = machine.batch_runs(len(runs))
        groups = [runs[i:i + per] for i in range(0, len(runs), per)]
        if len(groups) > 1:
            print(f"  메모리를 아끼려고 조각 {len(runs)}개를 {per}개씩 "
                  f"{len(groups)}번에 나눠 만듭니다 — 화질은 그대로입니다", flush=True)

        pieces: list[Path] = []
        at = 0.0                           # 이어붙인 타임라인에서 이 묶음이 시작하는 시각
        for gi, group in enumerate(groups):
            at_run = sum(len(g) for g in groups[:gi])
            graph = _compose_graph(group, w, h, x, y, params["fps"], chain,
                                   denoise, scaler, sharpen,
                                   region=region is not None, asrc=asrc,
                                   gains=gains[at_run:at_run + len(group)])
            span = sum(e - s for s, e in group)

            vf_extra = _title_filters(font, title, verse)
            if srt_text.strip():
                # 자막 시각은 이어붙인 타임라인 기준이다. 이 묶음에 걸친 것만
                # 남기고 묶음 시작 시각만큼 당긴다.
                part = cut_mod.shift_srt(srt_text, at, at + span)
                if part.strip():
                    ass = srt_to_ass(part, cfg.subtitle_font, cfg.shorts_sub_size)
                    (tmpdir / f"sub{gi}.ass").write_text(ass, encoding="utf-8")
                    vf_extra += f",subtitles=sub{gi}.ass"

            if vf_extra:
                part_graph = (graph.replace("[vpad]", "[vp0]")
                              + f";[vp0]{vf_extra.lstrip(',')}[vout]")
                vmap = "[vout]"
            else:
                part_graph, vmap = graph, "[vpad]"

            name = f"piece{gi:03d}.mp4" if len(groups) > 1 else "out.mp4"
            tag = (f"[ffmpeg {gi + 1}/{len(groups)}] " if len(groups) > 1
                   else "[ffmpeg] ")

            def _encode(threads: int) -> None:
                run_stream([
                    require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-stats",
                    "-i", str(src.resolve()),
                    *(["-i", str(region.resolve())] if region is not None else []),
                    *(["-i", str(audio_src.resolve())] if audio_src is not None else []),
                    "-filter_complex_threads", str(threads),
                    "-filter_complex", part_graph,
                    "-map", vmap, "-map", "[a]",
                    "-threads", str(threads),
                    "-c:v", "libx264", "-preset", _preset, "-crf", str(cfg.shorts_crf),
                    *machine.x264_extra(),      # 빠듯하면 내다보기를 줄인다
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", name,
                ], tag=tag, total=span, cwd=tmpdir)

            try:
                _encode(_threads)
            except ToolError as exc:
                # 메모리로 넘어졌으면 스레드만 1개로 줄여 그 묶음만 다시 해 본다.
                # 화질 설정은 그대로 둔다 — 조각마다 달라지면 이어붙일 때 어긋난다.
                if _threads <= 1 or not is_memory_error(exc):
                    raise
                print("  메모리가 모자라 이 묶음만 스레드 1개로 다시 합니다", flush=True)
                _encode(1)

            pieces.append(tmpdir / name)
            at += span

        stage = tmpdir / "out.mp4"
        if len(pieces) > 1:
            print(f"  만든 {len(pieces)}묶음을 이어붙이고 음량을 맞춥니다 — "
                  "화면은 그대로 복사하므로 화질이 깎이지 않습니다", flush=True)
            cut_mod.concat_copy(pieces, stage, audio_filter=final_af)
        elif final_af:
            # 한 묶음이면 조각 안에서 끝내지 않고 여기서 음량을 맞춘다.
            one = tmpdir / "one.mp4"
            stage.rename(one)
            cut_mod.concat_copy([one], stage, audio_filter=final_af)

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
