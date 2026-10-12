"""확정 구간 다운로드 → 이어붙이기 → SRT 생성.

설교와 축도를 각각 받아 하나의 mp4로 concat 한다. 중간의 찬양/헌금 구간은
빠지고, 자막 타임코드도 이어붙인 타임라인 기준으로 다시 계산한다.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import cut as cut_mod
from . import fetch
from .config import Config
from .marker import Marker
import re

from .util import ToolError, ffprobe_duration, require, run, srt_time


_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_stem(marker: Marker, suffix: str = "") -> str:
    """설교 제목이 있으면 그걸로, 없으면 날짜와 영상 id 로 파일 이름을 만든다."""
    title = _BAD_CHARS.sub(" ", (marker.sermon_title or "").strip())
    title = re.sub(r"\s+", " ", title).strip(" .")
    date = marker.upload_date or ""

    if title:
        stem = f"{date}_{title}" if date else title
    else:
        stem = f"{date or 'video'}_{marker.video_id}"

    stem = stem[:120].strip(" ._")
    return f"{stem}{suffix}" or "sermon"


def _concat(clips: list[Path], out_path: Path) -> Path:
    """concat demuxer로 무재인코딩 결합. 실패하면 재인코딩으로 폴백한다."""
    ffmpeg = require("ffmpeg")
    with tempfile.TemporaryDirectory() as tmp:
        listfile = Path(tmp) / "list.txt"
        listfile.write_text(
            "".join(f"file '{c.resolve().as_posix()}'\n" for c in clips),
            encoding="utf-8",
        )
        try:
            run([
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(listfile),
                "-c", "copy", "-movflags", "+faststart", str(out_path),
            ])
        except ToolError:
            run([
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(listfile),
                "-c:v", "libx264", "-preset", cut_mod.VIDEO_PRESET,
                "-crf", str(cut_mod.VIDEO_CRF),
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(out_path),
            ])
    return out_path


def build_srt(ranges: list[tuple[float, float, str]], clip_durations: list[float],
              transcript: list[dict], lead: float = 0.0,
              min_cue: float = 1.0, min_share: float = 0.3) -> str:
    """원본 절대시간 자막을 이어붙인 타임라인으로 옮긴다.

    lead 는 앞에 붙인 시작 영상의 길이. 그만큼 자막을 전부 뒤로 민다.

    **조금이라도 겹치면 넣으면 안 된다.** 컷을 쉼에 맞추느라 구간을 앞뒤로
    벌리는데, 전사문 문장은 빈틈없이 이어져 있어서 이웃 문장의 꼬리·머리가
    그 여백에 걸린다. 그러면 0.2초짜리 자막이 생겨 화면이 깜빡인다.
    실측(3분 요약): 자막 84줄 중 **22줄이 0.5초 미만**, 그중 17줄이 딱 0.20초 —
    컷 앞에 두는 여백(`pauses.snap_start` 의 lead)과 정확히 같은 값이었다.

    그래서 두 가지로 거른다.
      min_share  그 문장이 이만큼은 들어와야 이 조각의 말로 본다
      min_cue    잘라 놓고 보니 너무 짧으면 버린다 (읽을 수도 없다)

    값은 실측으로 골랐다. 세게 거르면 깜빡임은 없어지지만 소리는 나는데 자막이
    없는 대목이 생긴다. 3분 요약 84줄로 견준 결과:

        거르지 않음        84줄 · 0.5초 미만 22줄 · 못 읽을 만큼 빠른 줄 28
        share 0.5         52줄 · 깜빡임 0    · 소리 덮는 비율 90%
        **cue 1.0 · share 0.3**  55줄 · 깜빡임 0 · 덮는 비율 93% · 못 읽는 줄 0
    """
    lines: list[str] = []
    idx = 1
    offset = lead
    for (r_start, r_end, _label), clip_dur in zip(ranges, clip_durations):
        picked = [s for s in transcript if s["end"] > r_start and s["start"] < r_end]
        picked.sort(key=lambda s: s["start"])
        for seg in picked:
            span = seg["end"] - seg["start"]
            overlap = min(seg["end"], r_end) - max(seg["start"], r_start)
            if span <= 0 or overlap <= 0:
                continue
            if overlap < span * min_share:
                continue                       # 꼬리만 걸친 이웃 문장
            s = max(seg["start"], r_start) - r_start + offset
            e = min(seg["end"], r_end) - r_start + offset
            if e - s < min_cue:
                continue                       # 깜빡이는 자막
            lines.append(str(idx))
            lines.append(f"{srt_time(s)} --> {srt_time(e)}")
            lines.append(seg["text"])
            lines.append("")
            idx += 1
        offset += clip_dur
    return "\n".join(lines)


def _add_intro(final: Path, cfg: Config, result: dict) -> float:
    """설정에 시작 영상이 있으면 앞에 붙이고, 그 길이를 돌려준다."""
    intro = getattr(cfg, "intro_path", "")
    if not intro:
        return 0.0
    intro_path = Path(intro)
    if not intro_path.exists():
        print(f"  시작 영상을 찾지 못해 건너뜁니다: {intro_path.name}")
        return 0.0
    print(f"시작 영상을 앞에 붙입니다: {intro_path.name}")
    lead = cut_mod.prepend_intro(final, intro_path)
    result["시작 영상"] = f"{intro_path.name} ({lead:.0f}초)"
    return lead


def sermon_ranges(marker: Marker) -> list[tuple[float, float]]:
    """설교 구간만. 축도(benediction)는 뺀다.

    사람이 직접 만든 구간(label='clip')은 설교의 일부로 본다 — 직접 정하기로
    작업한 경우 label 이 전부 'clip' 이라 이걸 빼면 남는 게 없다.
    """
    picked = [(s, e) for s, e, label in marker.ranges() if label != "benediction"]
    return picked or [(s, e) for s, e, _ in marker.ranges()]


def summary_runs(marker: Marker, cfg: Config, transcript: list[dict],
                 wav: Path | None = None) -> list[tuple[float, float]]:
    """3분 요약으로 고른 문장들을 이어붙일 구간으로 바꾼다.

    문장 경계 그대로 자르면 말꼬리가 잘린다. 전사문에는 쉼이 안 적혀 있어서
    (vad_filter 가 무음을 걷어낸다) 오디오에서 쉼을 따로 찾아 거기에 맞춘다.
    소리 파일이 없으면 스냅 없이 문장 경계 그대로 쓴다 — 조금 거칠 뿐 돌아간다.
    """
    from . import highlight, pauses as pause_mod

    rng = marker.sermon_range()
    if not rng:
        raise ToolError("설교 구간이 정해지지 않았습니다. 먼저 [자동으로 찾기] 를 누르세요.")

    marks = highlight.build_marks(transcript, rng[0], rng[1])
    if not marks:
        raise ToolError("설교 구간에 자막이 없습니다. 먼저 자막을 만들어 주세요.")

    flags = marker.summary_chosen([m.start for m in marks])
    for m, on in zip(marks, flags):
        m.chosen = on
    snap = None
    if wav and Path(wav).exists():
        found = pause_mod.find_pauses(Path(wav), rng[0], rng[1])
        print(f"  {pause_mod.describe(found)} — 컷을 여기에 맞춥니다")
        snap = pause_mod.make_snap(found)
    else:
        print("  소리 파일이 없어 문장 경계 그대로 자릅니다")

    # 편집 화면이 길이를 셀 때와 **같은 함수**를 쓴다. 갈라지면 화면에 3분이라
    # 떠 놓고 3분 30초짜리가 나온다 (스냅이 구간을 앞뒤로 벌리기 때문).
    out = highlight.runs(marks, snap)
    if not out:
        raise ToolError("3분 요약에 넣을 문장을 고르지 않았습니다.")
    return out


def export_summary(marker: Marker, cfg: Config, src: Path,
                   transcript: list[dict], wav: Path | None = None) -> dict:
    """3분 요약 세로 영상을 만든다."""
    from . import shorts

    runs = summary_runs(marker, cfg, transcript, wav)
    total = sum(e - s for s, e in runs)

    cfg.ensure_dirs()
    out = cfg.export_dir / f"{safe_stem(marker, '_3분요약')}.mp4"

    ranges = [(s, e, "summary") for s, e in runs]
    srt_text = build_srt(ranges, [e - s for s, e in runs], transcript)

    verse = _guess_verse(transcript, marker)
    shorts.build(Path(src), runs, out, cfg,
                 title=marker.sermon_title or "", verse=verse, srt_text=srt_text)

    return {
        "video": out,
        "길이": f"{int(total // 60)}분 {int(total % 60):02d}초",
        "조각": f"{len(runs)}개",
        "크기": f"{out.stat().st_size / (1024 * 1024):.1f}MB",
    }


def export_summary_mp3(marker: Marker, cfg: Config, src: Path,
                       transcript: list[dict], wav: Path | None = None) -> dict:
    """3분 요약을 소리만 mp3 로 뽑는다.

    영상과 똑같은 구간·똑같은 순서로 잇는다 — `summary_runs()` 한 곳을 같이
    쓰므로 영상과 mp3 의 내용이 어긋날 수 없다. 화면을 건드리지 않아
    3분짜리는 몇 초면 끝난다 (영상은 몇 분 걸린다).
    """
    runs = summary_runs(marker, cfg, transcript, wav)
    total = sum(e - s for s, e in runs)

    cfg.ensure_dirs()
    out = cfg.export_dir / f"{safe_stem(marker, '_3분요약')}.mp3"

    print(f"{total:.0f}초를 소리만 뽑습니다 (mp3) — 조각 {len(runs)}개")
    if cfg.denoise:
        print(f"  잡음 제거: {'약하게' if cfg.denoise == 'light' else '강하게'}")
    if cfg.normalize_audio:
        print(f"  소리 크기를 방송 표준({cfg.target_lufs:.0f} LUFS)에 맞춥니다")

    title = marker.sermon_title or marker.title
    cut_mod.extract_mp3(
        Path(src), runs, out,
        bitrate=getattr(cfg, "mp3_bitrate", "96k"),
        mono=getattr(cfg, "mp3_mono", True),
        normalize=cfg.normalize_audio, denoise=cfg.denoise,
        lufs=cfg.target_lufs, peak=cfg.target_peak_db,
        tags={
            "title": f"{title} (3분 요약)" if title else "설교 3분 요약",
            "album": "창의진교회 설교 요약",
            "artist": "창의진교회",
            "date": (marker.upload_date or "")[:4],
            "genre": "Speech",
            "comment": _guess_verse(transcript, marker),
        },
    )
    return {
        "mp3": out,
        "길이": f"{int(total // 60)}분 {int(total % 60):02d}초",
        "조각": f"{len(runs)}개",
        "크기": f"{out.stat().st_size / (1024 * 1024):.1f}MB",
    }


_VERSE = re.compile(r"([가-힣]{2,6})\s*(\d+)\s*장\s*(\d+)\s*절"
                    r"(?:\s*부터\s*(\d+)\s*절)?")


BOOKS = (
    "창세기 출애굽기 레위기 민수기 신명기 여호수아 사사기 룻기 사무엘상 사무엘하 "
    "열왕기상 열왕기하 역대상 역대하 에스라 느헤미야 에스더 욥기 시편 잠언 전도서 "
    "아가 이사야 예레미야 예레미야애가 에스겔 다니엘 호세아 요엘 아모스 오바댜 요나 "
    "미가 나훔 하박국 스바냐 학개 스가랴 말라기 마태복음 마가복음 누가복음 요한복음 "
    "사도행전 로마서 고린도전서 고린도후서 갈라디아서 에베소서 빌립보서 골로새서 "
    "데살로니가전서 데살로니가후서 디모데전서 디모데후서 디도서 빌레몬서 히브리서 "
    "야고보서 베드로전서 베드로후서 요한일서 요한이서 요한삼서 유다서 요한계시록"
).split()


_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"


def _jamo(text: str) -> str:
    """한글을 자모로 풀어 쓴다.

    받아쓰기가 흘려듣는 건 글자가 아니라 소리다 — '헤' 와 '에' 는 초성 하나,
    '야' 와 '아' 는 중성 하나 차이다. 글자째로 견주면 '느헤미야' 와 '느에미아' 가
    반도 안 닮은 것으로 나오지만, 자모로 풀면 여덟 중 여섯이 같다.
    """
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out.append(_CHO[code // 588])
            out.append(_JUNG[(code % 588) // 28])
            jong = _JONG[code % 28]
            if jong != " ":
                out.append(jong)
        elif not ch.isspace():
            out.append(ch)
    return "".join(out)


def fix_book(name: str) -> str:
    """받아쓰기가 흘려들은 성경책 이름을 바로잡는다.

    실측: 느헤미야가 '느에미아' / '니에미아' 로 적혔다. 화면에 태워 넣으면
    되돌릴 수 없는 글자라 여기서만이라도 잡는다. 어지간히 닮지 않으면 원래
    글자를 그대로 둔다 — 엉뚱한 책 이름을 박는 것보다 받아쓴 대로가 낫다.
    """
    name = " ".join(name.split())
    if name in BOOKS:
        return name
    target = _jamo(name)
    if not target:
        return name

    best, score = name, 0.0
    for book in BOOKS:
        if abs(len(book) - len(name.replace(" ", ""))) > 1:
            continue
        cand = _jamo(book)
        if abs(len(cand) - len(target)) > 2:
            continue
        same = sum(1 for a, b in zip(cand, target) if a == b)
        s = same / max(len(cand), len(target), 1)
        if s > score:
            best, score = book, s
    return best if score >= 0.62 else name


def _guess_verse(transcript: list[dict], marker: Marker) -> str:
    """설교 앞부분에서 본문 성구를 찾아 '느헤미야 1:5-11' 꼴로 만든다.

    못 찾으면 빈 문자열. 제목 아래 한 줄이라 없으면 없는 대로 둔다.
    """
    rng = marker.sermon_range()
    lo = rng[0] if rng else 0.0
    for seg in sorted(transcript, key=lambda s: s["start"]):
        if seg["start"] > lo + 240:
            break
        hit = _VERSE.search(seg.get("text") or "")
        if hit:
            book, ch, v1, v2 = hit.groups()
            book = fix_book(book)
            return f"{book} {ch}:{v1}-{v2}" if v2 else f"{book} {ch}:{v1}"
    return ""


def export_mp3(marker: Marker, cfg: Config, src: Path,
               sermon_only: bool = True) -> dict[str, Path]:
    """설교를 소리만 mp3 로 뽑는다. 영상은 만들지 않는다."""
    ranges = sermon_ranges(marker) if sermon_only else \
        [(s, e) for s, e, _ in marker.ranges()]
    if not ranges:
        raise ToolError("내보낼 구간이 없습니다.")

    cfg.ensure_dirs()
    out = cfg.export_dir / f"{safe_stem(marker)}.mp3"

    total = sum(e - s for s, e in ranges)
    print(f"{total / 60:.0f}분을 소리만 뽑습니다 (mp3)"
          if total >= 60 else f"{total:.0f}초를 소리만 뽑습니다 (mp3)")
    if cfg.denoise:
        print(f"  잡음 제거: {'약하게' if cfg.denoise == 'light' else '강하게'}")
    if cfg.normalize_audio:
        print(f"  소리 크기를 방송 표준({cfg.target_lufs:.0f} LUFS)에 맞춥니다")

    cut_mod.extract_mp3(
        Path(src), ranges, out,
        bitrate=getattr(cfg, "mp3_bitrate", "96k"),
        mono=getattr(cfg, "mp3_mono", True),
        normalize=cfg.normalize_audio, denoise=cfg.denoise,
        lufs=cfg.target_lufs, peak=cfg.target_peak_db,
        tags={
            "title": marker.sermon_title or marker.title,
            "album": "창의진교회 설교",
            "artist": "창의진교회",
            "date": (marker.upload_date or "")[:4],
            "genre": "Speech",
        },
    )
    size = out.stat().st_size / (1024 * 1024)
    length = f"{total / 60:.0f}분" if total >= 60 else f"{total:.0f}초"
    return {"mp3": out, "길이": length, "크기": f"{size:.1f}MB"}


def export_local(marker: Marker, cfg: Config, src: Path,
                 transcript: list[dict] | None = None,
                 reencode: bool = True) -> dict[str, Path]:
    """이미 받아둔 로컬 영상에서 잘라 이어붙인다. 다시 받지 않으므로 빠르다.

    자막을 태우는 설정이면 자르기와 태우기를 ffmpeg 한 번으로 끝낸다. 예전에는
    자른 뒤 다시 태워 화면을 두 번 인코딩했다. 손실이 두 번 쌓이고 시간도 두 배였다.
    태우기는 어차피 다시 인코딩하므로 이때는 빠른 컷을 골랐어도 정확한 컷이 된다.
    """
    ranges = marker.ranges()
    if not ranges:
        raise ToolError("내보낼 구간이 없습니다.")

    cfg.ensure_dirs()
    final = cfg.export_dir / f"{safe_stem(marker)}.mp4"
    plain = [(s, e) for s, e, _ in ranges]

    burn = bool(cfg.make_srt and transcript
                and getattr(cfg, "subtitle_mode", "embed") == "burn")
    burn_srt: Path | None = None
    with tempfile.TemporaryDirectory() as tmp:
        if burn:
            # 태울 자막은 이어붙인 타임라인 기준이다. 시작 영상은 이 뒤에 앞으로
            # 붙으므로 lead=0. 파일로 남기는 .srt 는 아래에서 lead 를 넣어 따로 만든다.
            text = build_srt(ranges, [e - s for s, e in plain], transcript)
            if text.strip():
                burn_srt = Path(tmp) / "burn.srt"
                burn_srt.write_text(text, encoding="utf-8")
                reencode = True

        print(f"{len(plain)}개 구간을 {'정확히(재인코딩)' if reencode else '빠르게(스트림복사)'} 자릅니다")
        if burn_srt:
            print("  자막을 화면에 태웁니다. 자르기와 한 번에 인코딩합니다 (화면을 다시 그리므로 오래 걸립니다)")
        if cfg.denoise:
            print(f"  잡음 제거: {'약하게' if cfg.denoise == 'light' else '강하게'}")
        if cfg.normalize_audio:
            print(f"  소리 크기를 방송 표준({cfg.target_lufs:.0f} LUFS)에 맞춥니다")
        cut_mod.cut(Path(src), plain, final, reencode=reencode,
                    normalize=cfg.normalize_audio, denoise=cfg.denoise,
                    lufs=cfg.target_lufs, peak=cfg.target_peak_db,
                    crf=getattr(cfg, "video_crf", cut_mod.VIDEO_CRF),
                    preset=getattr(cfg, "video_preset", cut_mod.VIDEO_PRESET),
                    burn_srt=burn_srt, font=cfg.subtitle_font, size=cfg.subtitle_size)

    result: dict[str, Path] = {"video": final}
    lead = _add_intro(final, cfg, result)

    if cfg.make_srt and transcript:
        if reencode:
            durations = [e - s for s, e in plain]
        else:
            durations = [ffprobe_duration(final)] if len(plain) == 1 else [e - s for s, e in plain]
        srt_text = build_srt(ranges, durations, transcript, lead=lead)
        if srt_text.strip():
            srt_path = final.with_suffix(".srt")
            srt_path.write_text(srt_text, encoding="utf-8")
            result["srt"] = srt_path
            if burn_srt is not None:
                # 이미 태웠다. .srt 는 유튜브용으로 남기되 영상 옆에서 치운다.
                _stash_srt(final, srt_path, result)
                result["자막"] = "화면에 태움 (항상 보임)"
            else:
                _apply_subtitles(final, srt_path, cfg, result)

    return result


def _stash_srt(video: Path, srt: Path, result: dict) -> None:
    """자막이 이미 영상 안에 있으면 .srt 를 영상 옆에서 치운다.

    같은 이름의 .srt 가 옆에 있으면 VLC·팟플레이어가 그것까지 자동으로 읽어,
    자막이 두 줄로 겹쳐 보이거나 자막 목록이 두 개로 나온다.
    지우지는 않는다 — 유튜브에 따로 올리거나 나중에 고칠 때 쓴다.
    """
    backup_dir = video.parent / "자막백업"
    backup_dir.mkdir(exist_ok=True)
    try:
        srt.replace(backup_dir / srt.name)
        result["srt"] = backup_dir / srt.name
    except OSError:
        pass


def _apply_subtitles(video: Path, srt: Path, cfg: Config, result: dict) -> None:
    """설정에 따라 자막을 영상에 넣는다."""
    mode = getattr(cfg, "subtitle_mode", "embed")
    if mode == "burn":
        print("  자막을 화면에 태워 넣습니다 — 화면을 다시 그리므로 오래 걸립니다")
        cut_mod.burn_subtitles(video, srt, cfg.subtitle_font, cfg.subtitle_size)
        _stash_srt(video, srt, result)
        result["자막"] = "화면에 태움 (항상 보임)"
    elif mode == "file":
        # CLI 용. GUI 에는 없다 — 영상 안에 넣기와 하는 일이 겹친다.
        result["자막"] = "파일로만 (.srt)"
    else:
        print("  영상 안에 자막을 넣습니다 (재생기에서 껐다 켤 수 있음)")
        cut_mod.embed_subtitles(video, srt)
        _stash_srt(video, srt, result)
        result["자막"] = "영상 안에 넣음 (껐다 켤 수 있음)"


def export(marker: Marker, cfg: Config, transcript: list[dict] | None = None,
           keep_parts: bool = False) -> dict[str, Path]:
    ranges = marker.ranges()
    if not ranges:
        raise ToolError("내보낼 구간이 없습니다. 먼저 analyze 를 실행하세요.")

    cfg.ensure_dirs()
    stem = safe_stem(marker)
    part_dir = cfg.export_dir / f"{stem}_parts"
    part_dir.mkdir(parents=True, exist_ok=True)

    padded: list[tuple[float, float, str]] = []
    for start, end, label in ranges:
        s = max(0.0, start - cfg.head_pad)
        e = end + cfg.tail_pad
        if marker.duration:
            e = min(e, marker.duration)
        padded.append((s, e, label))

    clips: list[Path] = []
    for i, (start, end, label) in enumerate(padded, 1):
        clip = part_dir / f"{i:02d}_{label}.mp4"
        print(f"  [{i}/{len(padded)}] {label} 구간 다운로드 중...")
        fetch.download_section(marker.url, start, end, clip)
        clips.append(clip)

    result: dict[str, Path] = {}
    final = cfg.export_dir / f"{stem}.mp4"
    if len(clips) == 1:
        run([
            require("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(clips[0]), "-c", "copy", "-movflags", "+faststart", str(final),
        ])
    else:
        _concat(clips, final)
    result["video"] = final
    lead = _add_intro(final, cfg, result)

    if cfg.make_srt and transcript:
        durations = [ffprobe_duration(c) for c in clips]
        srt_text = build_srt(padded, durations, transcript, lead=lead)
        if srt_text.strip():
            srt_path = final.with_suffix(".srt")
            srt_path.write_text(srt_text, encoding="utf-8")
            result["srt"] = srt_path
            _apply_subtitles(final, srt_path, cfg, result)

    if not keep_parts:
        for c in clips:
            try:
                c.unlink()
            except OSError:
                pass
        try:
            part_dir.rmdir()
        except OSError:
            pass

    return result
