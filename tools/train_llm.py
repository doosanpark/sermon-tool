#!/usr/bin/env python3
"""AI 학습 — 이 교회 설교로 작은 AI(4B)를 학습시켜 설교 편집기에 넣는다.

학습용 파이썬 환경(.venv-train)에서, 그래픽카드가 있는 PC 에서 돈다.

    python tools/train_llm.py all        아래를 차례로 (멈췄다 다시 돌리면 끝난 단계는 건너뜀)
    python tools/train_llm.py download   바탕 모델 받기 (약 8GB, 처음 한 번)
    python tools/train_llm.py train      LoRA 학습 (그래픽카드)
    python tools/train_llm.py merge      학습한 것을 바탕 모델에 합치기
    python tools/train_llm.py gguf       설교 편집기가 쓰는 파일(gguf, 약 2.5GB)로 바꾸기
    python tools/train_llm.py eval       검증용 설교로 학습 전·후 견주기 → data/train/eval_report.txt
    python tools/train_llm.py prepare    바탕 모델·변환 도구를 미리 받고, 가짜 문제 4개로 끝까지 한 번 시험
                                          (학습 자료가 준비되기 전에 도구가 이 PC 에서 도는지 확인한다.
                                           시험 결과물은 지운다. [학습_2_설교모으기.bat] 끝에 자동으로 돈다)

결과: data/models/llm/sermon-qwen3-4b-Q4_K_M.gguf
    설교 편집기는 이 파일이 있으면 기본 모델 대신 이걸 쓴다 (core/llm.py model_path).
    다른 PC 에서 쓰려면 이 파일 하나만 그 PC 의 같은 자리에 복사하면 된다.

무엇을 배우나:
    blocks.label_with_ai 가 묻는 두 가지 — 구획(약 5분)마다 덩어리의 역할·혼자·인상과
    구획 요약, 그리고 구획 요약을 모아 본 설교 주제·문제제기·핵심·적용 구획.
    묻는 글은 학습 자료(tools/train_data.py)와 설교 편집기가 같은 함수로 만든다.

왜 LoRA 인가:
    4B 모델 전체(80억 바이트)를 고치려면 그래픽카드 메모리가 40GB 넘게 든다. LoRA 는 바탕은
    얼려 두고 작은 덧판(3천만 개 남짓)만 배운다 — 16GB 카드에서 넉넉하고, 배운 뒤 바탕에
    합치면 크기도 속도도 원래 모델과 같다. 그래서 교회 PC(CPU)에서 지금처럼 돈다.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAIN = ROOT / "data" / "train"
os.environ.setdefault("HF_HOME", str(TRAIN / "hf"))
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

BASE_REPO = "Qwen/Qwen3-4B-Instruct-2507"     # 생각 단계가 없는 4B. 지금 쓰는 Qwen3-4B 의 개선판
SFT = TRAIN / "sft"
LORA = TRAIN / "lora"
MERGED = TRAIN / "merged"
BF16 = TRAIN / "sermon-bf16.gguf"
LLAMA_TAG = "b10706"                          # bin/llama 의 llama-server 와 같은 판
OUT_DIR = ROOT / "data" / "models" / "llm"
OUT_NAME = "sermon-qwen3-4b-Q4_K_M.gguf"
LOG = TRAIN / "train_log.txt"

# 학습 설정 — 문제 수천 개짜리 좁은 일이라 크게 잡을 필요가 없다
LORA_R = 16
LORA_ALPHA = 32
LORA_DROPOUT = 0.05
LR = 2e-4
EPOCHS = 2
ACCUM = 16              # 문제 16개마다 한 걸음
WARMUP = 10
MAX_LEN = 4096


def say(text: str) -> None:
    print(text, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")


def mmss(sec: float) -> str:
    sec = int(max(sec, 0))
    return f"{sec // 3600}시간 {sec % 3600 // 60:02d}분" if sec >= 3600 else f"{sec // 60}분 {sec % 60:02d}초"


def keep_awake(on: bool = True) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
    except Exception:
        pass


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ------------------------------------------------------------------ 1. 바탕 모델
def base_dir() -> Path:
    from huggingface_hub import snapshot_download
    return Path(snapshot_download(BASE_REPO, allow_patterns=["*.json", "*.safetensors", "*.txt",
                                                             "*.model", "*.jinja", "merges.txt"]))


def cmd_download(_args) -> int:
    say(f"[받기] 바탕 모델 {BASE_REPO} (약 8GB, 처음 한 번만)")
    path = base_dir()
    say(f"  받았습니다: {path}")
    return 0


# ------------------------------------------------------------------ 2. 학습
def load_base(dtype, device_map):
    from transformers import AutoModelForCausalLM
    kw = dict(attn_implementation="sdpa", device_map=device_map, low_cpu_mem_usage=True)
    try:
        return AutoModelForCausalLM.from_pretrained(base_dir(), dtype=dtype, **kw)
    except TypeError:                                   # transformers 옛 판
        return AutoModelForCausalLM.from_pretrained(base_dir(), torch_dtype=dtype, **kw)


def encode(tok, ex: dict):
    """문제 하나 → (글자 번호들, 정답 표시). 답(assistant) 부분만 배운다."""
    msgs = [{"role": "system", "content": ex["system"]}, {"role": "user", "content": ex["user"]}]
    prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True,
                                     enable_thinking=False)
    p_ids = tok(prompt, add_special_tokens=False)["input_ids"]
    a_ids = tok(ex["assistant"] + "<|im_end|>", add_special_tokens=False)["input_ids"]
    ids = p_ids + a_ids
    if len(ids) > MAX_LEN:
        return None
    return ids, [-100] * len(p_ids) + a_ids


def cmd_train(args) -> int:
    import torch
    import torch.nn.functional as F
    from peft import LoraConfig, get_peft_model
    from transformers import AutoTokenizer

    best = LORA / "best"
    if (best / "adapter_config.json").exists() and not args.force:
        say("[학습] 이미 학습해 둔 것이 있습니다 — 건너뜁니다 (다시 하려면 --force)")
        return 0
    if not torch.cuda.is_available():
        say("[학습] 그래픽카드를 쓸 수 없습니다. [학습_1_준비.bat] 을 다시 실행해 확인해 주세요.")
        return 1
    tok = AutoTokenizer.from_pretrained(base_dir())
    train = [e for e in (encode(tok, x) for x in load_jsonl(SFT / "train.jsonl")) if e]
    valid = [e for e in (encode(tok, x) for x in load_jsonl(SFT / "valid.jsonl")) if e]
    if args.smoke:
        train, valid = train[:ACCUM], valid[:4]
    n_tok = sum(len(i) for i, _ in train)
    say(f"[학습] 문제 학습용 {len(train)}개 · 검증용 {len(valid)}개 · 글자조각 {n_tok:,}개")

    model = load_base(torch.bfloat16, {"": 0})
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model = get_peft_model(model, LoraConfig(
        r=LORA_R, lora_alpha=LORA_ALPHA, lora_dropout=LORA_DROPOUT, bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]))
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    say(f"  배우는 덧판 {trainable / 1e6:.0f}M 개 (바탕 모델은 그대로)")
    inner = model.base_model.model              # Qwen3ForCausalLM (LoRA 가 끼워진 채)

    def loss_of(ids, labels):
        """답 부분만 다음 글자 맞히기 손실. 151k 단어장 전체 로짓을 다 만들지 않아 메모리를 아낀다."""
        x = torch.tensor([ids], device="cuda")
        h = inner.model(input_ids=x).last_hidden_state[0]
        lab = torch.tensor(labels, device="cuda")
        pos = (lab[1:] != -100).nonzero().squeeze(-1)
        logits = inner.lm_head(h[pos]).float()
        return F.cross_entropy(logits, lab[pos + 1])

    def evaluate() -> float:
        model.eval()
        total = 0.0
        with torch.no_grad():
            for ids, labels in valid:
                total += float(loss_of(ids, labels))
        model.train()
        return total / max(len(valid), 1)

    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=LR, weight_decay=0.0)
    steps_per_epoch = math.ceil(len(train) / ACCUM)
    total_steps = steps_per_epoch * EPOCHS

    def lr_at(step: int) -> float:
        warm = min(1.0, (step + 1) / WARMUP)
        prog = min(step / max(total_steps, 1), 1.0)
        return warm * (0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * prog)))

    sched = torch.optim.lr_scheduler.LambdaLR(opt, lr_at)
    v0 = evaluate()
    say(f"  학습 전 검증 손실 {v0:.3f}")
    best_v = float("inf")
    began = time.monotonic()
    seen_tok = 0
    step = 0
    model.train()
    rnd = random.Random(1)
    for epoch in range(EPOCHS):
        order = list(range(len(train)))
        rnd.shuffle(order)
        run_loss, run_n = 0.0, 0
        for j, idx in enumerate(order):
            ids, labels = train[idx]
            loss = loss_of(ids, labels) / ACCUM
            loss.backward()
            run_loss += float(loss) * ACCUM
            run_n += 1
            seen_tok += len(ids)
            if (j + 1) % ACCUM == 0 or j + 1 == len(order):
                torch.nn.utils.clip_grad_norm_(params, 1.0)
                opt.step()
                sched.step()
                opt.zero_grad(set_to_none=True)
                step += 1
                if step % 5 == 0 or step == total_steps:
                    spent = time.monotonic() - began
                    speed = seen_tok / spent
                    left = (total_steps - step) * spent / step
                    mem = torch.cuda.max_memory_allocated() / 2**30
                    say(f"  [{epoch + 1}/{EPOCHS}회째] 걸음 {step}/{total_steps}  손실 {run_loss / run_n:.3f}"
                        f"  {speed:,.0f}조각/초  메모리 {mem:.1f}GB  남은 시간 약 {mmss(left)}")
                    run_loss, run_n = 0.0, 0
        v = evaluate()
        say(f"  {epoch + 1}회째 끝 — 검증 손실 {v:.3f} (학습 전 {v0:.3f})")
        model.save_pretrained(LORA / f"epoch{epoch + 1}")
        if v < best_v:
            best_v = v
            if best.exists():
                shutil.rmtree(best)
            model.save_pretrained(best)
            say(f"  가장 좋은 덧판으로 저장: {best}")
    tok.save_pretrained(best)
    (LORA / "result.json").write_text(json.dumps({
        "base": BASE_REPO, "train": len(train), "valid": len(valid), "tokens": n_tok,
        "valid_loss_before": round(v0, 4), "valid_loss_best": round(best_v, 4),
        "epochs": EPOCHS, "lr": LR, "r": LORA_R, "minutes": round((time.monotonic() - began) / 60, 1),
        "finished": datetime.now().isoformat(timespec="seconds")}, indent=1), encoding="utf-8")
    say(f"[학습] 끝 — {mmss(time.monotonic() - began)}")
    return 0


# ------------------------------------------------------------------ 3. 합치기
def cmd_merge(args) -> int:
    import torch
    from peft import PeftModel
    from transformers import AutoTokenizer

    if (MERGED / "config.json").exists() and not args.force:
        say("[합치기] 이미 합쳐 둔 것이 있습니다 — 건너뜁니다")
        return 0
    say("[합치기] 학습한 덧판을 바탕 모델에 합칩니다 (몇 분)")
    base = load_base(torch.bfloat16, {"": "cpu"})
    model = PeftModel.from_pretrained(base, str(LORA / "best"))
    model = model.merge_and_unload()
    if MERGED.exists():
        shutil.rmtree(MERGED)
    model.save_pretrained(MERGED, safe_serialization=True, max_shard_size="2GB")
    AutoTokenizer.from_pretrained(base_dir()).save_pretrained(MERGED)
    say(f"  합쳤습니다: {MERGED}")
    return 0


# ------------------------------------------------------------------ 4. gguf
def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "sermon-tool"})
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        got, last = 0, 0.0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if time.monotonic() - last > 2:
                last = time.monotonic()
                print(f"    {got / 2**20:.0f}MB" + (f" / {total / 2**20:.0f}MB" if total else ""), flush=True)
    os.replace(tmp, dest)
    return dest


def llama_src() -> Path:
    """llama.cpp 원본 코드 (같은 판). 그 안의 변환 프로그램과 gguf 도구를 쓴다."""
    root = TRAIN / f"llama.cpp-{LLAMA_TAG}"
    if (root / "convert_hf_to_gguf.py").exists():
        return root
    say(f"  llama.cpp {LLAMA_TAG} 원본을 받습니다 (변환 프로그램)")
    z = _download(f"https://github.com/ggml-org/llama.cpp/archive/refs/tags/{LLAMA_TAG}.zip",
                  TRAIN / f"llama.cpp-{LLAMA_TAG}.zip")
    with zipfile.ZipFile(z) as zf:
        zf.extractall(TRAIN)
    return root


def llama_quantize() -> Path:
    """gguf 를 4비트로 줄이는 llama-quantize.exe (bin/llama 와 같은 판의 CPU 묶음에 들어 있다)."""
    folder = TRAIN / "llama-bin"
    exe = folder / ("llama-quantize.exe" if os.name == "nt" else "llama-quantize")
    if exe.exists():
        return exe
    say(f"  llama.cpp {LLAMA_TAG} 실행파일 묶음을 받습니다 (llama-quantize)")
    name = f"llama-{LLAMA_TAG}-bin-win-cpu-x64.zip"
    url = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/{name}"
    try:                                               # 이름이 바뀌었을 수 있어 목록에서 찾는다
        api = f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{LLAMA_TAG}"
        req = urllib.request.Request(api, headers={"User-Agent": "sermon-tool"})
        assets = json.loads(urllib.request.urlopen(req, timeout=60).read()).get("assets", [])
        for a in assets:
            if "win-cpu-x64" in a.get("name", "") and a["name"].endswith(".zip"):
                url, name = a["browser_download_url"], a["name"]
                break
    except Exception:
        pass
    z = _download(url, TRAIN / name)
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(z) as zf:
        zf.extractall(folder)
    if not exe.exists():                               # 하위 폴더에 풀렸으면 찾는다
        for p in folder.rglob(exe.name):
            return p
    return exe


def run(cmd: list, **kw) -> int:
    say("  > " + " ".join(str(c) for c in cmd)[:220])
    return subprocess.call([str(c) for c in cmd], **kw)


def cmd_gguf(args) -> int:
    out = OUT_DIR / OUT_NAME
    if out.exists() and not args.force:
        say(f"[gguf] 이미 있습니다 — {out}")
        return 0
    src = llama_src()
    say("[gguf] 변환 도구 깔기")
    run([sys.executable, "-m", "pip", "install", "--quiet", str(src / "gguf-py")])
    say("[gguf] 합친 모델 → gguf (bf16, 약 8GB)")
    if run([sys.executable, str(src / "convert_hf_to_gguf.py"), str(MERGED),
            "--outtype", "bf16", "--outfile", str(BF16)]) != 0 or not BF16.exists():
        say("  변환에 실패했습니다. 위 글을 Claude 에게 보여 주세요.")
        return 1
    q = llama_quantize()
    say("[gguf] 4비트로 줄이기 (Q4_K_M, 약 2.5GB — 지금 쓰는 모델과 같은 크기)")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    if run([q, BF16, tmp, "Q4_K_M"], cwd=str(q.parent)) != 0 or not tmp.exists():
        say("  줄이기에 실패했습니다. 위 글을 Claude 에게 보여 주세요.")
        return 1
    os.replace(tmp, out)
    say(f"  만들었습니다: {out} ({out.stat().st_size / 2**30:.1f}GB)")
    return 0


# ------------------------------------------------------------------ 5. 견주기
def _iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def cmd_eval(args) -> int:
    import copy
    import train_data as td
    from core import blocks as blk, llm as llm_mod
    from core.config import Config

    split = json.loads((SFT / "split.json").read_text(encoding="utf-8"))
    vids = split["valid"][:args.limit] if args.limit else split["valid"]
    if args.smoke:
        vids = vids[:1]
    cfg = Config()
    cfg.llm_threads = max(4, min(16, (os.cpu_count() or 8) - 4))
    models = [("규칙만", None)]
    base_gguf = llm_mod.model_dir(cfg) / cfg.llm_model_file
    if base_gguf.exists():
        models.append(("학습 전 AI", base_gguf))
    if (OUT_DIR / OUT_NAME).exists():
        models.append(("학습한 AI", OUT_DIR / OUT_NAME))
    say(f"[견주기] 검증용 설교 {len(vids)}편 × {' / '.join(n for n, _ in models)}")
    say(f"  AI 는 교회 PC 처럼 CPU 로 돌립니다 ({cfg.llm_threads}스레드). 한 편에 몇 분씩 걸립니다.")

    built = {}
    for vid in vids:
        info, marks, blocks, secs = td.build(vid)
        lab = json.loads((td.TEACHER / f"{vid}.json").read_text(encoding="utf-8"))
        built[vid] = (info, marks, blocks, lab)

    rows = []           # (모델, 영상, 역할 맞음, 역할 수, 핵심 IoU, 적용 IoU, 핵심 후보 안, 적용 후보 안, 초, JSON 실패)
    detail = []
    for name, path in models:
        server = None
        if path is not None:
            server = llm_mod.start(cfg, progress=lambda t: None, model=path)
        try:
            for vid in vids:
                info, marks, blocks0, lab = built[vid]
                blocks = copy.deepcopy(blocks0)
                t = time.monotonic()
                bad = [0]
                if server is not None:
                    def ask(system, user, max_tokens, _s=server):
                        reply = llm_mod.ask(_s, system, user, max_tokens=max_tokens, progress=False)
                        if not blk._json_obj(reply):
                            bad[0] += 1
                        return reply
                    blk.label_with_ai(blocks, ask)
                spent = time.monotonic() - t
                truth = {r[0]: td.ROLE_KEY[r[1]] for r in lab["blocks"]}
                judged = [b for b in blocks if b.role not in ("reading", "aside")
                          and truth.get(b.index) not in ("reading", "aside")]
                right = sum(1 for b in judged if b.role == truth.get(b.index))
                by = {b.index: b for b in blocks}
                slots = blk.assemble("two", blocks, 100.0)
                res = {"core": (0.0, False), "closing": (0.0, False)}
                lines = []
                for s in slots:
                    pk = lab.get("picks", {}).get(s.key)
                    if not pk or not s.chosen:
                        continue
                    gold = (by[pk[0]].start, by[pk[1]].end)
                    first = _iou((s.chosen.start, s.chosen.end), gold)
                    inside = any(_iou((c.start, c.end), gold) >= 0.3 for c in s.cands)
                    res[s.key] = (first, inside)
                    lines.append(f"    {s.name}: 고른 {td.hms(s.chosen.start)}~{td.hms(s.chosen.end)}"
                                 f" · 정답 {td.hms(gold[0])}~{td.hms(gold[1])} · 겹침 {first:.0%}"
                                 f"{'' if inside else ' · 후보 4개 안에 없음'}")
                rows.append((name, vid, right, len(judged), res["core"][0], res["closing"][0],
                             res["core"][1], res["closing"][1], spent, bad[0]))
                detail.append(f"  [{name}] {info.get('title_date', '')} {info.get('title', '')[:40]}  "
                              f"역할 {right}/{len(judged)} · {spent:.0f}초")
                detail.extend(lines)
                say(detail[-1 - len(lines)])
        finally:
            if server is not None:
                server.close()

    out = [f"견준 때: {datetime.now():%Y-%m-%d %H:%M} · 검증용 설교 {len(vids)}편 (학습에 쓰지 않은 설교)",
           "정답은 채점지(teacher) 기준. '겹침' 은 프로그램이 처음 고른 대목과 정답 대목이 시간으로 겹치는 정도.",
           "", f"{'방법':<10} {'역할 맞춤':>8} {'핵심 겹침':>8} {'적용 겹침':>8} {'핵심 후보안':>9} {'적용 후보안':>9} {'한 편 시간':>9}"]
    for name, _ in models:
        rs = [r for r in rows if r[0] == name]
        if not rs:
            continue
        acc = sum(r[2] for r in rs) / max(sum(r[3] for r in rs), 1)
        core = sum(r[4] for r in rs) / len(rs)
        clos = sum(r[5] for r in rs) / len(rs)
        cin = sum(r[6] for r in rs) / len(rs)
        lin = sum(r[7] for r in rs) / len(rs)
        sec = sum(r[8] for r in rs) / len(rs)
        bad = sum(r[9] for r in rs)
        out.append(f"{name:<10} {acc:>8.0%} {core:>8.0%} {clos:>8.0%} {cin:>9.0%} {lin:>9.0%} {mmss(sec):>9}"
                   + (f"  (JSON 깨짐 {bad}번)" if bad else ""))
    out += ["", "설교별:"] + detail
    report = (OUT_DIR if args.smoke else TRAIN) / "eval_report.txt"
    report.write_text("\n".join(out) + "\n", encoding="utf-8")
    say("\n".join(out[:4 + len(models)]))
    say(f"  자세한 결과: {report}")
    return 0


def use_smoke() -> None:
    """시험 실행 — 결과물을 data/train/smoke/ 로 돌리고 아주 조금만 배운다."""
    global LORA, MERGED, BF16, OUT_DIR, EPOCHS, SFT
    base = TRAIN / "smoke"
    LORA, MERGED, BF16, OUT_DIR = base / "lora", base / "merged", base / "sermon-bf16.gguf", base
    SFT = base / "sft"
    EPOCHS = 1


def _fake_examples() -> list[dict]:
    """도구 시험용 가짜 문제. 실제 묻는 글 모양(section_prompt)을 그대로 쓴다."""
    from core import blocks as blk
    texts = ["오늘 본문은 우리에게 아주 중요한 질문을 던집니다 성벽을 아무리 높이 세우면 뭐합니까",
             "하나님이 보고 계신다는 거예요 여러분이 약한 사람을 어떻게 대하는지 하나님이 보고 계십니다",
             "느헤미야 5장 1절부터 5절까지 같이 읽겠습니다",
             "이번 한 주도 하나님을 경외하면서 옳은 일을 선택하며 살아가시기를 축원합니다"]
    roles = ["문제제기", "핵심", "봉독", "적용"]
    out = []
    for k in range(4):
        bl = [blk.Block(index=k * 2 + i, first=0, last=0, start=0, end=20, text=texts[(k + i) % 4],
                        pos=(k * 2 + i) / 8) for i in range(2)]
        user, _ = blk.section_prompt([bl], 0)
        ans = {"요약": "시험용 요약 한 문장", "덩어리": [
            {"번호": b.index, "역할": roles[(k + i) % 4], "혼자": 4, "인상": 3} for i, b in enumerate(bl)]}
        out.append({"system": blk.AI_SYSTEM, "user": user + "\n\n/no_think",
                    "assistant": json.dumps(ans, ensure_ascii=False)})
    return out


def cmd_prepare(args) -> int:
    """학습에 필요한 것을 미리 받고, 가짜 문제로 학습→합치기→gguf→llama-server 까지 한 번 돌려 본다."""
    say("[준비] 바탕 모델과 변환 도구를 미리 받고, 끝까지 한 번 시험해 봅니다 (20~40분)")
    ok_file = TRAIN / "prepare_ok.txt"
    if ok_file.exists() and not args.force:
        say("  이미 시험을 마쳤습니다 — 건너뜁니다")
        return 0
    cmd_download(args)
    src = llama_src()
    run([sys.executable, "-m", "pip", "install", "--quiet", str(src / "gguf-py")])
    llama_quantize()
    use_smoke()
    for d in (LORA, MERGED):
        if d.exists():
            shutil.rmtree(d)
    SFT.mkdir(parents=True, exist_ok=True)
    fake = _fake_examples()
    for name in ("train", "valid"):
        with (SFT / f"{name}.jsonl").open("w", encoding="utf-8") as fh:
            for ex in (fake if name == "train" else fake[:2]):
                fh.write(json.dumps(ex, ensure_ascii=False) + "\n")
    args.smoke, args.force = True, True
    for step in (cmd_train, cmd_merge, cmd_gguf):
        if step(args) != 0:
            say("[준비] 시험이 중간에 멈췄습니다. data/train/train_log.txt 를 Claude 에게 보여 주세요.")
            return 1
    from core import blocks as blk, llm as llm_mod
    from core.config import Config
    cfg = Config()
    cfg.llm_threads = max(4, min(16, (os.cpu_count() or 8) - 4))
    server = llm_mod.start(cfg, progress=say, model=OUT_DIR / OUT_NAME)
    try:
        t = time.monotonic()
        reply = llm_mod.ask(server, blk.AI_SYSTEM, fake[0]["user"], max_tokens=200, progress=False)
        say(f"  llama-server 로 물어봄 ({time.monotonic() - t:.0f}초): {reply.strip()[:160]}")
    finally:
        server.close()
    for p in (MERGED, BF16, OUT_DIR / OUT_NAME):        # 시험 결과물은 18GB 남짓이라 지운다
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            pass
    ok_file.write_text(f"{datetime.now():%Y-%m-%d %H:%M} 준비 시험 통과\n{reply.strip()[:400]}\n",
                       encoding="utf-8")
    say("[준비] 끝 — 학습 도구가 이 PC 에서 끝까지 돕니다")
    return 0


def cmd_all(args) -> int:
    for step in (cmd_download, cmd_train, cmd_merge, cmd_gguf, cmd_eval):
        code = step(args)
        if code != 0:
            return code
    say("")
    if args.smoke:
        say("시험 실행이 끝까지 됐습니다. 진짜 학습은 [학습_3_AI학습.bat] 으로 합니다.")
    else:
        say("모두 끝났습니다. 설교 편집기의 [3분 요약] 탭에서 고르는 방법을 '뜻 + AI' 로 두면 학습한 AI 를 씁니다.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["all", "prepare", "download", "train", "merge", "gguf", "eval"])
    ap.add_argument("--force", action="store_true", help="이미 한 단계도 다시")
    ap.add_argument("--limit", type=int, default=8, help="견주기에 쓸 검증용 설교 수 (0 이면 전부)")
    ap.add_argument("--smoke", action="store_true", help="시험 실행 (결과는 data/train/smoke/)")
    args = ap.parse_args()
    if args.smoke:
        use_smoke()
    keep_awake(True)
    try:
        return {"all": cmd_all, "prepare": cmd_prepare, "download": cmd_download, "train": cmd_train,
                "merge": cmd_merge, "gguf": cmd_gguf, "eval": cmd_eval}[args.cmd](args)
    finally:
        keep_awake(False)


if __name__ == "__main__":
    sys.exit(main())
