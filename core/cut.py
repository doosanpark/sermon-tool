"""로컬 영상 파일에서 구간을 잘라 이어붙인다.

유튜브에서 구간을 다시 받는 것보다 빠르고, 프레임 단위로 정확하게 자를 수 있다.

  reencode=True   filter_complex 로 한 번에 trim+concat. 지정한 시각 그대로 정확.
                  대신 다시 인코딩하므로 CPU에서 느리다.
  reencode=False  구간별로 스트림 복사 후 concat. 훨씬 빠르지만 컷 지점이
                  키프레임으로 밀려 최대 수 초 오차가 생긴다.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .util import (ToolError, ffprobe_duration, has_audio, require, run,
                    run_stream)


def _esc(path: Path) -> str:
    return path.resolve().as_posix()


DENOISE_FILTERS = {
    # 실측(예배 방송 40초): 원본 잡음 바닥 -59.2dB
    "light": "highpass=f=80,afftdn=nf=-25",        # -72.2dB · 목소리 손상 거의 없음
    "strong": "highpass=f=80,afftdn=nf=-35:nr=20",  # -78.3dB · 잡음이 심할 때
}


# 다시 인코딩할 때의 기본 화질. 설정(core/config.py 의 video_crf · video_preset)이
# 있으면 그 값이 들어오고, 여기 값은 설정을 안 넘기는 자리(시작 영상 규격 맞추기,
# 이어붙이기 폴백)에서 쓴다.
#
# 실측(720p 60초, 12스레드 CPU, 원본 대비 SSIM):
#     crf20 veryfast  0.9948  27배속   ← 예전 기본값
#     crf18 veryfast  0.9960  35배속
#     crf18 fast      0.9974  16배속
#     crf18 medium    0.9976  12배속   ← 지금 기본값
# 손실은 crf 20→18 에서 거의 다 줄고, 프리셋은 medium 까지가 남은 몫을 채운다.
# 같은 crf 에서 프리셋을 올리면 파일이 오히려 조금 커진다. 압축을 더 잘하는 게
# 아니라 같은 화질 목표를 더 충실히 지키기 때문이다.
VIDEO_CRF = 18
VIDEO_PRESET = "medium"


def sub_style(font: str = "Malgun Gothic", size: int = 22) -> str:
    """자막 태우기 스타일. 자르기와 함께 태울 때와 따로 태울 때 같은 모양이어야 한다."""
    return (f"FontName={font},FontSize={size},PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H90000000,BorderStyle=3,Outline=1,Shadow=0,"
            "MarginV=28,Alignment=2")


def audio_chain(denoise: str = "", normalize: bool = True,
                lufs: float = -16.0, peak: float = -1.5) -> str:
    """잡음 제거 → 음량 맞추기 순서로 필터를 잇는다.

    순서가 중요하다. 잡음을 먼저 지워야 음량을 키울 때 잡음까지 커지지 않는다.
    """
    parts = []
    if denoise in DENOISE_FILTERS:
        parts.append(DENOISE_FILTERS[denoise])
    if normalize:
        parts.append(loudnorm_filter(lufs, peak))
    return ",".join(parts)


def loudnorm_filter(lufs: float = -16.0, peak: float = -1.5) -> str:
    """방송 표준 음량으로 맞추는 필터.

    I  = 목표 음량(LUFS), TP = 최대 피크(dB), LRA = 다이내믹 레인지.
    설교는 말소리라 LRA 11 이면 자연스럽다.
    """
    return f"loudnorm=I={lufs}:TP={peak}:LRA=11"


def cut_reencode(src: Path, ranges: list[tuple[float, float]], out_path: Path,
                 crf: int = VIDEO_CRF, preset: str = VIDEO_PRESET,
                 normalize: bool = True, lufs: float = -16.0, peak: float = -1.5,
                 denoise: str = "", burn_srt: Path | None = None,
                 font: str = "Malgun Gothic", size: int = 22) -> Path:
    """정확한 컷. 한 번의 ffmpeg 호출로 trim + concat 을 끝낸다.

    burn_srt 를 주면 자막까지 같은 호출에서 태운다. 자른 뒤 따로 태우면 화면을
    두 번 인코딩해 손실이 두 번 쌓이고(실측 SSIM 0.9948 → 0.9928) 시간도 두 배다.
    자막 시각은 이어붙인 타임라인 기준이어야 한다 (export.build_srt 가 그렇게 만든다).
    """
    if not ranges:
        raise ToolError("자를 구간이 없습니다.")

    parts = []
    labels = []
    for i, (start, end) in enumerate(ranges):
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
        labels.append(f"[v{i}][a{i}]")

    vlabel = "[vraw]" if burn_srt else "[v]"
    graph = (";".join(parts) + ";" + "".join(labels)
             + f"concat=n={len(ranges)}:v=1:a=1{vlabel}[araw]")

    # 잡음 제거·음량 맞추기를 여기서 같이 태운다. 예전에는 인자만 받고 쓰지
    # 않아서, [정확히 자르기] 를 켜면 소리가 원본 그대로 나갔다.
    chain = audio_chain(denoise, normalize, lufs, peak)
    graph += f";[araw]{chain}[a]" if chain else ";[araw]anull[a]"

    # 윈도우 절대경로를 필터 안에 넣으면 콜론과 역슬래시가 문법으로 읽혀 깨진다.
    # 자막 파일을 임시 폴더에 복사해 두고 작업 폴더를 거기로 옮겨 이름만 넘긴다.
    with tempfile.TemporaryDirectory() as tmp:
        cwd = None
        if burn_srt:
            shutil.copyfile(burn_srt, Path(tmp) / "sub.srt")
            graph += f";[vraw]subtitles=sub.srt:force_style='{sub_style(font, size)}'[v]"
            cwd = tmp
        run_stream([
            require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-stats",
            "-i", str(Path(src).resolve()),
            "-filter_complex", graph,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(Path(out_path).resolve()),
        ], tag="[ffmpeg] ", total=sum(e - s for s, e in ranges), cwd=cwd)
    return out_path


def video_params(path: Path) -> dict:
    """이어붙이려면 두 영상의 규격이 같아야 한다. 기준이 될 값을 읽는다."""
    exe = require("ffprobe")
    out = run([
        exe, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]).split()
    if len(out) < 3:
        raise ToolError(f"영상 규격을 읽지 못했습니다: {path}")
    width, height, rate = int(out[0]), int(out[1]), out[2]
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    except (ValueError, ZeroDivisionError):
        fps = 30.0

    audio = run([
        exe, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,channels",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]).split()
    sample_rate = int(audio[0]) if audio else 48000
    channels = int(audio[1]) if len(audio) > 1 else 2
    return {"width": width, "height": height, "fps": round(fps, 3),
            "sample_rate": sample_rate, "channels": channels}


def _match_format(src: Path, params: dict, out_path: Path,
                  crf: int = VIDEO_CRF, preset: str = VIDEO_PRESET) -> Path:
    """시작 영상을 본 영상과 같은 규격으로 다시 만든다.

    크기가 다르면 비율을 지킨 채 줄이고 남는 자리는 검게 채운다(pad).
    소리가 없는 영상이면 무음 트랙을 넣는다 — 없으면 이어붙일 때 소리가 밀린다.
    """
    w, h = params["width"], params["height"]
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
          f"fps={params['fps']},format=yuv420p,setsar=1")

    cmd = [require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-stats",
           "-i", str(src)]
    if not has_audio(src):
        cmd += ["-f", "lavfi", "-i",
                f"anullsrc=r={params['sample_rate']}:cl="
                f"{'mono' if params['channels'] == 1 else 'stereo'}",
                "-shortest"]
    cmd += [
        "-vf", vf,
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-c:a", "aac", "-b:a", "192k",
        "-ar", str(params["sample_rate"]), "-ac", str(params["channels"]),
        "-movflags", "+faststart", str(out_path),
    ]
    run_stream(cmd, tag="[ffmpeg] ")
    return out_path


def prepend_intro(video: Path, intro: Path) -> float:
    """시작 영상을 본 영상 앞에 붙인다. 붙인 시작 영상의 길이(초)를 돌려준다.

    규격을 맞춰 둔 뒤 concat 으로 이어붙이므로 본 영상은 다시 인코딩하지
    않는다 — 40분 분량이라도 몇 초면 끝난다.
    """
    video, intro = Path(video), Path(intro)
    if not intro.exists():
        raise ToolError(f"시작 영상이 없습니다: {intro}")

    params = video_params(video)
    with tempfile.TemporaryDirectory() as tmp:
        fixed = Path(tmp) / "intro.mp4"
        print("  시작 영상을 본 영상 규격에 맞춥니다"
              f" ({params['width']}x{params['height']}, {params['fps']:.0f}fps)")
        _match_format(intro, params, fixed)
        intro_len = ffprobe_duration(fixed)

        listfile = Path(tmp) / "list.txt"
        listfile.write_text(
            f"file '{fixed.resolve().as_posix()}'\n"
            f"file '{video.resolve().as_posix()}'\n", encoding="utf-8")
        merged = video.with_name(video.stem + ".intro.mp4")
        ffmpeg = require("ffmpeg")
        base = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats",
                "-f", "concat", "-safe", "0", "-i", str(listfile)]
        try:
            run_stream(base + ["-c", "copy", "-movflags", "+faststart",
                               str(merged)], tag="[ffmpeg] ")
        except ToolError:
            print("  빠른 이어붙이기가 안 돼 다시 인코딩합니다 (조금 걸립니다)")
            run_stream(base + ["-c:v", "libx264", "-preset", VIDEO_PRESET,
                               "-crf", str(VIDEO_CRF),
                               "-c:a", "aac", "-b:a", "192k",
                               "-movflags", "+faststart", str(merged)],
                       tag="[ffmpeg] ")
        video.unlink()
        merged.rename(video)
    return intro_len


def extract_mp3(src: Path, ranges: list[tuple[float, float]], out_path: Path,
                bitrate: str = "96k", mono: bool = True,
                normalize: bool = True, lufs: float = -16.0, peak: float = -1.5,
                denoise: str = "", tags: dict[str, str] | None = None) -> Path:
    """구간을 이어붙여 소리만 mp3 로 뽑는다.

    화면을 건드리지 않으므로 영상 만들기보다 훨씬 빠르다 (40분 분량 1분 안쪽).
    설교는 말소리뿐이라 모노 96kbps 면 충분하고, 파일이 작아 휴대폰으로
    주고받기 좋다.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    if not src.exists():
        raise ToolError(f"원본 파일이 없습니다: {src}")
    if not ranges:
        raise ToolError("자를 구간이 없습니다.")

    bad = [(s, e) for s, e in ranges if e - s <= 0.2]
    if bad:
        raise ToolError(f"길이가 0에 가까운 구간이 있습니다: {bad}")

    parts, labels = [], []
    for i, (start, end) in enumerate(ranges):
        parts.append(f"[0:a]atrim=start={start:.3f}:end={end:.3f},"
                     f"asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[a{i}]")
    graph = ";".join(parts) + ";" + "".join(labels) + \
        f"concat=n={len(ranges)}:v=0:a=1[ac]"

    chain = audio_chain(denoise, normalize, lufs, peak)
    if chain:
        graph += f";[ac]{chain}[aout]"
        out_label = "[aout]"
    else:
        out_label = "[ac]"

    cmd = [
        require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-stats",
        "-i", str(src),
        "-filter_complex", graph,
        "-map", out_label, "-vn",
        "-c:a", "libmp3lame", "-b:a", bitrate, "-ar", "44100",
    ]
    if mono:
        cmd += ["-ac", "1"]
    for key, value in (tags or {}).items():
        if value:
            cmd += ["-metadata", f"{key}={value}"]
    cmd.append(str(out_path))

    run_stream(cmd, tag="[ffmpeg] ", total=sum(e - s for s, e in ranges))
    return out_path


def clean_audio_in_place(path: Path, lufs: float = -16.0, peak: float = -1.5,
                         denoise: str = "", normalize: bool = True) -> Path:
    """이미 만들어진 파일의 소리만 손본다 (잡음 제거·음량 맞추기).

    영상은 그대로 복사하고 소리만 다시 인코딩하므로 40분 분량도 1분 안쪽이다.
    """
    ffmpeg = require("ffmpeg")
    tmp = path.with_name(path.stem + ".norm.mp4")
    try:
        total = ffprobe_duration(path)
    except ToolError:
        total = None
    run_stream([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats",
        "-i", str(path),
        "-af", audio_chain(denoise, normalize, lufs, peak),
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(tmp),
    ], tag="[ffmpeg] ", total=total)
    path.unlink()
    tmp.rename(path)
    return path


def cut_copy(src: Path, ranges: list[tuple[float, float]], out_path: Path) -> Path:
    """빠른 컷. 스트림 복사라 컷 지점이 키프레임으로 밀린다."""
    if not ranges:
        raise ToolError("자를 구간이 없습니다.")

    ffmpeg = require("ffmpeg")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        clips = []
        for i, (start, end) in enumerate(ranges):
            clip = tmpdir / f"part{i:02d}.mp4"
            run([
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                "-i", str(src), "-c", "copy",
                "-avoid_negative_ts", "make_zero", str(clip),
            ])
            clips.append(clip)

        if len(clips) == 1:
            run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(clips[0]), "-c", "copy",
                 "-movflags", "+faststart", str(out_path)])
            return out_path

        listfile = tmpdir / "list.txt"
        listfile.write_text("".join(f"file '{_esc(c)}'\n" for c in clips), encoding="utf-8")
        run([
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listfile),
            "-c", "copy", "-movflags", "+faststart", str(out_path),
        ])
    return out_path


def embed_subtitles(video: Path, srt: Path) -> Path:
    """영상 안에 자막 트랙으로 넣는다. 재생기에서 끄고 켤 수 있다.

    화면을 다시 그리지 않으므로 40분 분량도 몇 초면 끝난다.
    """
    ffmpeg = require("ffmpeg")
    tmp = video.with_name(video.stem + ".sub.mp4")
    run_stream([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats",
        "-i", str(video), "-i", str(srt),
        "-map", "0", "-map", "1",
        "-c", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", "language=kor",
        "-metadata:s:s:0", "title=한국어",
        "-movflags", "+faststart", str(tmp),
    ], tag="[ffmpeg] ")
    video.unlink()
    tmp.rename(video)
    return video


def burn_subtitles(video: Path, srt: Path, font: str = "Malgun Gothic",
                   size: int = 22, crf: int = VIDEO_CRF, preset: str = VIDEO_PRESET) -> Path:
    """이미 만들어진 영상에 자막을 태워 넣는다. 어디서나 보이지만 끌 수 없다.

    화면을 전부 다시 그리므로 40분 분량에 20~30분 걸린다. 로컬 영상 내보내기는
    이 함수를 쓰지 않는다. cut_reencode 가 자르기와 함께 한 번에 태운다.
    유튜브에서 구간을 받아 잇는 export() 처럼 영상이 이미 있을 때만 쓴다.
    """
    ffmpeg = require("ffmpeg")
    tmp = video.with_name(video.stem + ".burn.mp4")

    # 윈도우 경로의 콜론·역슬래시는 필터 문법과 충돌하므로 작업 폴더를 옮겨 파일명만 쓴다
    vf = f"subtitles={srt.name}:force_style='{sub_style(font, size)}'"

    try:
        total = ffprobe_duration(video)
    except ToolError:
        total = None

    run_stream([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats",
        "-i", video.name, "-vf", vf,
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-c:a", "copy", "-movflags", "+faststart", tmp.name,
    ], tag="[ffmpeg] ", total=total, cwd=video.parent)
    video.unlink()
    tmp.rename(video)
    return video


def cut(src: Path, ranges: list[tuple[float, float]], out_path: Path,
        reencode: bool = True, normalize: bool = True,
        lufs: float = -16.0, peak: float = -1.5, denoise: str = "",
        crf: int = VIDEO_CRF, preset: str = VIDEO_PRESET,
        burn_srt: Path | None = None,
        font: str = "Malgun Gothic", size: int = 22) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    src = Path(src)
    if not src.exists():
        raise ToolError(f"원본 파일이 없습니다: {src}")

    bad = [(s, e) for s, e in ranges if e - s <= 0.2]
    if bad:
        raise ToolError(
            f"길이가 0에 가까운 구간이 있습니다: {bad}\n"
            "실수로 뭉개진 구간입니다. GUI 에서 Ctrl+Z 나 [AI가 찾은 구간으로] 를 눌러 되돌리세요.")

    # 스트림 복사는 키프레임 단위라 몇 초짜리 구간에서는 빈 파일이 나온다
    if not reencode and any(e - s < 10.0 for s, e in ranges):
        print("  10초 미만 구간이 있어 정확한 컷(재인코딩)으로 전환합니다")
        reencode = True

    # 자막을 태우려면 어차피 화면을 다시 그려야 한다. 그러면 정확한 컷이 공짜다.
    if burn_srt and not reencode:
        print("  자막을 태우므로 정확한 컷(재인코딩)으로 전환합니다 (시간은 같고 손실은 한 번뿐)")
        reencode = True

    if reencode:
        return cut_reencode(src, ranges, out_path, crf=crf, preset=preset,
                            normalize=normalize, lufs=lufs, peak=peak,
                            denoise=denoise, burn_srt=burn_srt, font=font, size=size)

    cut_copy(src, ranges, out_path)
    if normalize or denoise:
        what = []
        if denoise:
            what.append("잡음 제거")
        if normalize:
            what.append("소리 크기 맞추기")
        print(f"  {' + '.join(what)} 중 (영상은 그대로, 소리만 다시 인코딩)")
        clean_audio_in_place(out_path, lufs, peak, denoise, normalize)
    return out_path


def extract_still(src: Path, seconds: float, out_path: Path, width: int = 320) -> Path:
    """특정 시각의 정지화면. 경계 확인용."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run([
        require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, seconds):.3f}", "-i", str(src),
        "-frames:v", "1", "-vf", f"scale={width}:-1", str(out_path),
    ])
    return out_path
