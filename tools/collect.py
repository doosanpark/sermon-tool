#!/usr/bin/env python3
"""학습 자료 모으기 — 교회 유튜브 채널의 설교를 받아 자막과 쉼까지 만들어 둔다.

학습용 파이썬 환경(.venv-train)에서 돈다. 설교 편집기를 쓰는 데는 필요 없다.

    python tools/collect.py check-torch     그래픽카드로 AI 를 학습시킬 수 있는지
    python tools/collect.py check-whisper   그래픽카드로 자막을 만들 수 있는지
    python tools/collect.py run             목록의 설교를 차례로 처리 (멈췄다 다시 돌려도 이어서)
    python tools/collect.py status          어디까지 했는지

만드는 것 (모두 data/ 아래):
    audio/영상.wav                 16kHz 모노 소리 (설교 편집기와 같은 자리)
    cache/영상.transcript.json     자막 (설교 편집기와 같은 자리 — 편집기에서 그대로 열린다)
    train/pauses/영상.json         목사님이 숨 고르는 자리 (덩어리 나누기에 쓴다)
    train/meta/영상.json           제목 · 날짜 · 설교 구간 · 걸린 시간
    train/collect_state.json       처리 이력 (다시 돌리면 끝난 것은 건너뛴다)

영상(화면)은 받지 않는다. 학습에는 말만 있으면 되고, 소리만 받으면 한 편에 수십 MB다.

자막은 교회 PC 와 **같은 모델(medium)** 로 만든다. 학습한 AI 가 실제로 읽게 될 자막과
같은 버릇(오인식)을 보고 배워야 하기 때문이다. 그래픽카드가 있으면 그걸로, 안 되면 CPU 로 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAIN = ROOT / "data" / "train"
os.environ.setdefault("HF_HOME", str(TRAIN / "hf"))          # 모델을 C: 가 아니라 이 폴더에
sys.path.insert(0, str(ROOT))

LOG = TRAIN / "collect_log.txt"
STATE = TRAIN / "collect_state.json"
REPORT = TRAIN / "setup_report.txt"

SERMON_MIN = 15 * 60        # 이보다 짧으면 설교 영상이 아니다 (찬양·광고·쇼츠)
SERMON_MAX = 70 * 60        # 이보다 길면 예배 전체 — 성문으로 설교 구간을 찾는다
_DATE_IN_TITLE = re.compile(r"(20\d\d)\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# 채널에는 설교 말고도 찬양 모음·라디오·묵상·강좌가 많다 (2026-10-19 목록 236편 중 설교는 80편 남짓).
# 길이로만 고르면 50분짜리 CCM 모음이 '설교' 로 들어온다 — 제목으로 고른다.
_KEEP = re.compile(r"주일예배|열린예배|성탄축하예배|송구영신예배|신년감사")
_DROP = re.compile(r"찬양|CCM|Radio|라디오|Playlist|묵상|베이직|Basics|아침예배|오디오 버전")


def say(text: str) -> None:
    print(text, flush=True)
    TRAIN.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")


def report(key: str, value) -> None:
    line = f"{key}: {value}"
    print("  " + line, flush=True)
    TRAIN.mkdir(parents=True, exist_ok=True)
    with REPORT.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def mmss(sec: float) -> str:
    sec = int(max(sec, 0))
    return f"{sec // 3600}시간 {sec % 3600 // 60:02d}분" if sec >= 3600 else f"{sec // 60}분 {sec % 60:02d}초"


# ------------------------------------------------------------------ 그래픽카드 라이브러리
def add_cuda_dlls() -> list[str]:
    """pip 로 받은 NVIDIA 라이브러리(cuBLAS·cuDNN) 폴더를 윈도우가 찾을 수 있게 알린다.

    faster-whisper 가 쓰는 ctranslate2 는 이 DLL 들을 PATH 에서 찾는다. pip 꾸러미는
    site-packages/nvidia/<이름>/bin 에 DLL 을 풀어 두기만 하고 PATH 에는 넣지 않는다.
    """
    found: list[str] = []
    if os.name != "nt":
        return found
    import site
    bases = [Path(p) for p in site.getsitepackages()] + [Path(sys.prefix) / "Lib" / "site-packages"]
    for base in bases:
        nv = base / "nvidia"
        if not nv.is_dir():
            continue
        for sub in sorted(nv.iterdir()):
            b = sub / "bin"
            if b.is_dir() and str(b) not in found:
                found.append(str(b))
    for d in found:
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
    if found:
        os.environ["PATH"] = os.pathsep.join(found + [os.environ.get("PATH", "")])
    return found


# ------------------------------------------------------------------ 확인
def cmd_check_torch(_args) -> int:
    report("파이썬", sys.version.split()[0])
    try:
        import torch
    except Exception as exc:
        report("torch", f"없음 ({exc})")
        return 1
    report("torch", torch.__version__)
    ok = torch.cuda.is_available()
    report("torch 그래픽카드", "됨" if ok else "안 됨")
    if not ok:
        return 1
    p = torch.cuda.get_device_properties(0)
    report("그래픽카드", f"{p.name} · 메모리 {p.total_memory / 2**30:.1f}GB · 세대 sm_{p.major}{p.minor}")
    try:
        a = torch.randn(4096, 4096, device="cuda", dtype=torch.bfloat16)
        b = torch.randn(4096, 4096, device="cuda", dtype=torch.bfloat16)
        for _ in range(3):
            a @ b
        torch.cuda.synchronize()
        t = time.perf_counter()
        for _ in range(20):
            a @ b
        torch.cuda.synchronize()
        dt = time.perf_counter() - t
        report("bf16 계산 속도", f"{20 * 2 * 4096 ** 3 / dt / 1e12:.0f} TFLOPS (학습 가능)")
    except Exception as exc:
        report("bf16 계산", f"실패 — {type(exc).__name__}: {exc}")
        return 1
    return 0


def cmd_check_whisper(_args) -> int:
    dlls = add_cuda_dlls()
    report("NVIDIA 라이브러리 폴더", len(dlls))
    try:
        import ctranslate2
        report("ctranslate2", ctranslate2.__version__)
        report("ctranslate2 그래픽카드 수", ctranslate2.get_cuda_device_count())
    except Exception as exc:
        report("ctranslate2", f"불러오지 못함 ({exc})")
        return 1
    import numpy as np
    from faster_whisper import WhisperModel
    noise = (np.random.default_rng(0).standard_normal(16000 * 8) * 0.02).astype(np.float32)
    for device, ctype in (("cuda", "float16"), ("cpu", "int8")):
        try:
            t = time.perf_counter()
            model = WhisperModel("medium", device=device, compute_type=ctype,
                                 cpu_threads=_cpu_threads() if device == "cpu" else 0)
            loaded = time.perf_counter() - t
            t = time.perf_counter()
            segs, _ = model.transcribe(noise, language="ko", beam_size=1, vad_filter=False)
            list(segs)
            report(f"자막 {device}", f"됨 (모델 올리기 {loaded:.0f}초, 8초 소리 {time.perf_counter() - t:.1f}초)")
            del model
        except Exception as exc:
            report(f"자막 {device}", f"안 됨 — {type(exc).__name__}: {str(exc)[:200]}")
    return 0


def _cpu_threads() -> int:
    return max(4, min(16, (os.cpu_count() or 8) - 4))


# ------------------------------------------------------------------ 처리 이력
def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def save_state(state: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


def load_list() -> list[dict]:
    path = TRAIN / "videos.json"
    if not path.exists():
        say("영상 목록이 없습니다. [학습_1_준비.bat] 을 먼저 실행해 주세요.")
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("videos", [])


def title_date(title: str) -> str:
    m = _DATE_IN_TITLE.search(title or "")
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{y}{int(mo):02d}{int(d):02d}"


def kind_of(title: str, duration: float) -> str:
    """설교만 올린 영상(sermon)인가, 예배 전체를 담은 영상(service)인가.

    'LIVE 방송' 은 길이가 짧아도 예배 실황이다 (52분짜리도 있었다) — 찬양·기도가 섞여 있으니
    성문으로 설교 구간을 찾아야 한다.
    """
    if "LIVE" in (title or "").upper() or duration > SERMON_MAX:
        return "service"
    return "sermon"


def plan(videos: list[dict], only: set[str] | None = None) -> list[dict]:
    """처리할 영상과 순서. 설교만 올린 영상을 먼저, 예배 전체 영상은 그다음.

    같은 날 설교 영상이 따로 있으면 예배 전체 영상은 건너뛴다 — 같은 설교를 두 번 배우면
    그 주 설교에 치우친다.
    """
    keep = []
    for v in videos:
        if only and v["id"] not in only:
            continue
        title = v.get("title") or ""
        if not only and (not _KEEP.search(title) or _DROP.search(title)):
            continue
        d = v.get("duration") or 0.0
        if d and d < SERMON_MIN:
            continue
        if v.get("live_status") in ("is_live", "is_upcoming", "upcoming"):
            continue
        v = dict(v)
        v["kind"] = kind_of(title, d)
        keep.append(v)
    sermons = [v for v in keep if v["kind"] != "service"]
    services = [v for v in keep if v["kind"] == "service"]
    covered = {title_date(v["title"]) for v in sermons} - {""}
    services = [v for v in services if title_date(v["title"]) not in covered]
    return sermons + services


# ------------------------------------------------------------------ 한 편 처리
class Whisper:
    """그래픽카드로 자막을 만들고, 안 되면 CPU 로 바꾼다. 한 번 올린 모델을 계속 쓴다."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.device = ""

    def ensure(self, prefer_gpu: bool = True) -> str:
        if self.device:
            return self.device
        from core import transcribe
        from faster_whisper import WhisperModel
        cfg = self.cfg
        if prefer_gpu:
            try:
                add_cuda_dlls()
                import numpy as np
                model = WhisperModel(cfg.whisper_model, device="cuda", compute_type="float16")
                noise = (np.random.default_rng(0).standard_normal(16000 * 4) * 0.02).astype(np.float32)
                list(model.transcribe(noise, language="ko", beam_size=1, vad_filter=False)[0])
                cfg.device, cfg.compute_type = "cuda", "float16"
                transcribe._MODEL_CACHE[(cfg.whisper_model, cfg.device, cfg.compute_type)] = model
                self.device = "gpu"
                say("  자막은 그래픽카드로 만듭니다")
                return self.device
            except Exception as exc:
                say(f"  그래픽카드로 자막을 만들 수 없어 CPU 로 합니다 ({type(exc).__name__}: {str(exc)[:120]})")
        model = WhisperModel(cfg.whisper_model, device="cpu", compute_type="int8",
                             cpu_threads=_cpu_threads())
        cfg.device, cfg.compute_type = "cpu", "int8"
        transcribe._MODEL_CACHE[(cfg.whisper_model, cfg.device, cfg.compute_type)] = model
        self.device = "cpu"
        say(f"  자막은 CPU({_cpu_threads()}스레드)로 만듭니다")
        return self.device

    def fall_back(self) -> None:
        from core import transcribe
        transcribe._MODEL_CACHE.clear()
        self.device = ""
        self.ensure(prefer_gpu=False)


def process(v: dict, cfg, whisper: Whisper, covered_dates: set[str]) -> dict:
    from core import fetch, pauses as pm, transcribe
    from core.util import ffprobe_duration

    vid = v["id"]
    times: dict[str, float] = {}
    t0 = time.monotonic()
    meta = fetch.info(v["url"])
    if meta.get("live_status") in ("is_live", "is_upcoming"):
        return {"status": "skipped", "note": "아직 방송 중이거나 예정된 방송"}
    date = meta.get("upload_date") or title_date(meta.get("title", ""))
    duration = float(meta.get("duration") or v.get("duration") or 0.0)
    kind = kind_of(meta.get("title") or v.get("title", ""), duration)
    if duration and duration < SERMON_MIN:
        return {"status": "skipped", "note": f"{duration / 60:.0f}분짜리 — 설교 영상이 아닌 듯"}
    tdate = title_date(meta.get("title", "")) or date
    if kind == "service" and tdate in covered_dates:
        return {"status": "skipped", "note": "같은 날 설교만 올린 영상을 이미 처리함"}

    wav = cfg.audio_path(vid)
    if not wav.exists():
        say("  소리 받는 중")
        fetch.download_audio(meta["webpage_url"] or v["url"], wav)
    times["download"] = round(time.monotonic() - t0, 1)
    duration = duration or ffprobe_duration(wav)

    # 설교 구간
    t = time.monotonic()
    if kind == "sermon":
        start, end = 0.0, duration
    else:
        from core.voiceprint import Embedder, Voiceprint, analyze_media
        if not cfg.voiceprint_path.exists():
            return {"status": "skipped", "note": "예배 전체 영상인데 성문이 없어 설교 구간을 못 찾음"}
        say("  예배 전체 영상 — 성문으로 설교 구간을 찾는 중")
        vp = Voiceprint.load(cfg.voiceprint_path)
        result = analyze_media(wav, vp, Embedder(threads=8), hop=cfg.vp_hop,
                               threshold=cfg.vp_threshold, progress=lambda s: None)
        if not result.sermon:
            return {"status": "skipped", "note": "성문으로 설교를 찾지 못함 (다른 분이 설교하셨거나 예배가 아님)"}
        start, end = float(result.sermon[0]), float(result.sermon[1])
    times["detect"] = round(time.monotonic() - t, 1)

    # 자막
    t = time.monotonic()
    tpath = cfg.transcript_path(vid)
    if tpath.exists():
        segs = json.loads(tpath.read_text(encoding="utf-8"))
        say(f"  자막이 이미 있습니다 ({len(segs)}문장)")
    else:
        whisper.ensure()
        try:
            segs = transcribe.transcribe_range(wav, start, end, cfg, "sermon")
        except Exception as exc:
            if whisper.device != "gpu":
                raise
            say(f"  그래픽카드 자막이 멈췄습니다 ({type(exc).__name__}) — CPU 로 다시 합니다")
            whisper.fall_back()
            segs = transcribe.transcribe_range(wav, start, end, cfg, "sermon")
        transcribe.save_transcript(tpath, segs)
    times["stt"] = round(time.monotonic() - t, 1)
    if len(segs) < 50:
        return {"status": "skipped", "note": f"자막이 {len(segs)}문장뿐 — 설교가 아닌 듯"}

    # 쉼
    t = time.monotonic()
    found = pm.find_pauses(wav, start, end)
    (TRAIN / "pauses").mkdir(parents=True, exist_ok=True)
    (TRAIN / "pauses" / f"{vid}.json").write_text(
        json.dumps([[round(p.start, 3), round(p.end, 3)] for p in found]), encoding="utf-8")
    times["pauses"] = round(time.monotonic() - t, 1)

    info = {
        "id": vid, "title": meta.get("title", ""), "upload_date": date, "title_date": tdate,
        "duration": round(duration, 2), "kind": kind, "sermon": [round(start, 2), round(end, 2)],
        "sentences": len(segs), "pauses": len(found),
        "whisper": f"{cfg.whisper_model} · {whisper.device or '이미 있던 자막'}",
        "seconds": times, "made_at": datetime.now().isoformat(timespec="seconds"),
    }
    (TRAIN / "meta").mkdir(parents=True, exist_ok=True)
    (TRAIN / "meta" / f"{vid}.json").write_text(json.dumps(info, ensure_ascii=False, indent=1),
                                                encoding="utf-8")
    covered_dates.add(tdate)
    return {"status": "done", "note": f"{len(segs)}문장 · 설교 {(end - start) / 60:.0f}분",
            "date": tdate, "kind": kind}


def keep_awake(on: bool = True) -> None:
    """도는 동안 윈도우가 절전 모드로 들어가지 않게 한다 (밤새 돌려 두는 일이라)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        flags = 0x80000000 | (0x00000001 if on else 0)     # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def cmd_run(args) -> int:
    keep_awake(True)
    try:
        return _run(args)
    finally:
        keep_awake(False)


def _run(args) -> int:
    from core.config import Config
    cfg = Config()
    cfg.ensure_dirs()
    videos = load_list()
    if not videos:
        return 1
    todo = plan(videos, set(args.only.split(",")) if args.only else None)
    state = load_state()
    covered = {s.get("date", "") for s in state.values() if s.get("status") == "done"} - {""}
    left = [v for v in todo if state.get(v["id"], {}).get("status") not in ("done", "skipped")]
    if args.limit:
        left = left[:args.limit]
    say("=" * 60)
    say(f" 설교 모으기 — 모두 {len(todo)}편 중 남은 {len(left)}편")
    say(" 멈추려면 이 창을 닫으세요. 다시 실행하면 끝난 것은 건너뛰고 이어서 합니다.")
    say("=" * 60)
    whisper = Whisper(cfg)
    began = time.monotonic()
    done_n = 0
    fails_in_row = 0
    for k, v in enumerate(left, 1):
        free = shutil.disk_usage(ROOT).free / 2**30
        if free < 5:
            say(f"디스크 남은 자리가 {free:.0f}GB 뿐이라 멈춥니다. 자리를 비우고 다시 실행해 주세요.")
            break
        say(f"[{k}/{len(left)}] {v['title'][:50]}  ({v.get('duration', 0) / 60:.0f}분)")
        t = time.monotonic()
        try:
            res = process(v, cfg, whisper, covered)
            fails_in_row = 0
        except KeyboardInterrupt:
            say("멈췄습니다. 다시 실행하면 이어서 합니다.")
            save_state(state)
            return 0
        except Exception as exc:
            res = {"status": "error", "note": f"{type(exc).__name__}: {str(exc)[:300]}"}
            with LOG.open("a", encoding="utf-8") as fh:
                fh.write(traceback.format_exc() + "\n")
            fails_in_row += 1
        res["at"] = datetime.now().isoformat(timespec="seconds")
        res["title"] = v["title"]
        prev = state.get(v["id"], {})
        res["tries"] = int(prev.get("tries", 0)) + (1 if res["status"] == "error" else 0)
        if res["status"] == "error" and res["tries"] >= 3:
            res["status"] = "skipped"                 # 세 번 실패한 영상은 더 붙잡지 않는다
            res["note"] = "세 번 실패: " + res["note"]
        state[v["id"]] = res
        save_state(state)
        spent = time.monotonic() - t
        mark = {"done": "끝", "skipped": "건너뜀", "error": "실패"}[res["status"]]
        say(f"  → {mark} — {res.get('note', '')}  ({mmss(spent)})")
        if res["status"] == "done":
            done_n += 1
            avg = (time.monotonic() - began) / k
            say(f"  지금까지 {done_n}편 끝 · 남은 {len(left) - k}편 · 예상 남은 시간 약 {mmss(avg * (len(left) - k))}")
        if fails_in_row >= 4:
            say("연달아 4편이 실패했습니다. 유튜브가 잠시 막았거나 인터넷이 끊겼을 수 있습니다.")
            say("한두 시간 뒤 다시 실행해 주세요. 끝난 것은 건너뛰고 이어서 합니다.")
            break
        time.sleep(2)                                 # 유튜브에 연달아 몰아치지 않게
    cmd_status(args)
    return 0


def cmd_status(_args) -> int:
    state = load_state()
    from collections import Counter
    c = Counter(s.get("status") for s in state.values())
    say(f"처리 이력: 끝 {c.get('done', 0)}편 · 건너뜀 {c.get('skipped', 0)}편 · 실패 {c.get('error', 0)}편")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-torch")
    sub.add_parser("check-whisper")
    r = sub.add_parser("run")
    r.add_argument("--limit", type=int, default=0, help="이번에 처리할 최대 편수")
    r.add_argument("--only", default="", help="영상 ID 들 (쉼표로)")
    sub.add_parser("status")
    args = ap.parse_args()
    return {"check-torch": cmd_check_torch, "check-whisper": cmd_check_whisper,
            "run": cmd_run, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
