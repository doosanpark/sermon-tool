"""로컬 소형 LLM — 좁혀진 후보 안에서 무엇을 남길지 고른다.

DIAMOND(arXiv:2506.02351)가 말하는 가운데 단계다. 규칙과 임베딩이 후보를
좁히고(Preparation), 여기서 문맥으로 고르고(Decision), 사람이 편집기에서
확정한다(Reflection). 논문에서 수치 지표(WPA)만 쓰면 F1 42.9% 였는데, 이 가운데
단계를 얹은 전체 구조가 84.8% 였다 (KBO 경기 5편 기준).

왜 llama.cpp 인가:
    `CLAUDE.md` 가 런타임에 torch 를 금지한다. llama.cpp 는 순수 C++ 이라
    torch 가 필요 없고, ffmpeg·yt-dlp 처럼 `bin/` 에 실행파일 하나만 두면 된다.
    말을 거는 방법도 OpenAI 와 같은 형식이라, 나중에 클라우드로 바꾸고 싶으면
    주소만 갈아 끼우면 된다.

없으면 조용히 건너뛴다. 실행파일이 없든 모델이 없든 서버가 안 뜨든 답이
이상하든, 전부 None 을 돌려주고 규칙이 고른 결과를 그대로 쓴다.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .util import PROJECT_ROOT, ToolError, find_tool, tool_env

SERVER_EXE = "llama-server"
READY_TIMEOUT = 90.0        # 2GB 모델을 CPU 로 올리는 데 걸리는 시간
ASK_TIMEOUT = 480.0         # 생각을 끄면 1~3분이면 끝난다. 그 배쯤 기다려 준다.
                            # 더 늘리지 말 것 — 기다리는 동안 화면이 잠겨 있다.


def find_server() -> str | None:
    """llama-server 실행파일을 찾는다.

    ffmpeg 처럼 bin/ 바로 아래가 아니라 bin/llama/ 에 둔다. DLL 이 스물몇 개라
    ffmpeg·yt-dlp 옆에 흩뿌리면 어느 게 무엇인지 알 수 없게 된다.
    윈도우는 실행파일 옆의 DLL 을 먼저 찾으므로 한 폴더에 모여 있어야 한다.
    """
    for base in (PROJECT_ROOT / "bin" / "llama", PROJECT_ROOT / "bin"):
        for suffix in (".exe", ""):
            p = base / f"{SERVER_EXE}{suffix}"
            if p.exists():
                return str(p)
    return find_tool(SERVER_EXE)          # PATH 에 깔아 두신 분도 있다


def model_dir(cfg) -> Path:
    return Path(cfg.work_dir) / "models" / "llm"


def model_path(cfg) -> Path:
    return model_dir(cfg) / cfg.llm_model_file


def is_ready(cfg) -> bool:
    """실행파일과 모델이 둘 다 있어야 쓸 수 있다."""
    return bool(find_server()) and model_path(cfg).exists()


def why_not(cfg) -> str:
    """왜 못 쓰는지 사람 말로. 화면에 그대로 띄운다."""
    missing = []
    if not find_server():
        missing.append(
            f"{SERVER_EXE} 실행파일이 없습니다.\n"
            "  https://github.com/ggml-org/llama.cpp/releases 에서\n"
            "  llama-...-bin-win-cpu-x64.zip 을 받아 풀고, llama-server.exe 와\n"
            "  같은 폴더의 DLL 들을 다음 폴더에 넣어 주세요:\n"
            f"  {PROJECT_ROOT / 'bin' / 'llama'}")
    if not model_path(cfg).exists():
        missing.append(f"모델 파일이 없습니다: {model_path(cfg).name}")
    return "\n".join(missing)


def download(cfg, progress=None) -> Path:
    """모델을 처음 한 번만 내려받는다 (약 2GB)."""
    target = model_path(cfg)
    if target.exists():
        return target
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise ToolError("huggingface_hub 가 필요합니다.  pip install huggingface_hub") from exc

    say = progress or (lambda t: print(f"  {t}"))
    say(f"AI 모델을 처음 한 번만 내려받습니다 — {cfg.llm_model_file} (약 2GB)")
    say("  한 번 받아 두면 그 뒤로는 인터넷 없이 돌아갑니다.")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        got = hf_hub_download(repo_id=cfg.llm_model_repo, filename=cfg.llm_model_file)
        target.write_bytes(Path(got).read_bytes())
    except Exception as exc:
        raise ToolError(
            "AI 모델을 내려받지 못했습니다.\n"
            f"  {exc}\n\n"
            "인터넷이 막혀 있으면 다른 PC 에서 받아 아래에 넣어 주세요.\n"
            f"  {target}\n"
            f"  https://huggingface.co/{cfg.llm_model_repo}\n\n"
            "넣지 않아도 3분 요약은 규칙과 임베딩만으로 돌아갑니다."
        ) from exc
    say("  받았습니다")
    return target


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Server:
    url: str
    proc: subprocess.Popen | None = None

    def close(self) -> None:
        if not self.proc:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None


def start(cfg, progress=None) -> Server:
    """llama-server 를 띄운다. cfg.llm_url 이 있으면 그걸 그대로 쓴다."""
    say = progress or (lambda t: print(f"  {t}"))
    if getattr(cfg, "llm_url", ""):
        return Server(url=cfg.llm_url.rstrip("/"))

    exe = find_server()
    if not exe:
        raise ToolError(why_not(cfg))
    model = model_path(cfg)
    if not model.exists():
        raise ToolError(why_not(cfg))

    port = _free_port()
    cmd = [exe, "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
           "-c", str(cfg.llm_context), "-t", str(cfg.llm_threads),
           "--no-webui", "-ngl", "0"]
    say(f"AI 모델을 올립니다 ({model.name}) — 처음 한 번 시간이 걸립니다")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=tool_env(),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    url = f"http://127.0.0.1:{port}"
    started = time.monotonic()
    while time.monotonic() - started < READY_TIMEOUT:
        if proc.poll() is not None:
            raise ToolError(f"{SERVER_EXE} 가 바로 종료됐습니다. 모델 파일을 확인해 주세요.")
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2):
                say(f"  준비됨 ({time.monotonic() - started:.0f}초)")
                return Server(url=url, proc=proc)
        except (urllib.error.URLError, OSError):
            time.sleep(1.0)
    Server(url, proc).close()
    raise ToolError(f"{SERVER_EXE} 가 {READY_TIMEOUT:.0f}초 안에 뜨지 않았습니다.")


def ask(server: Server, system: str, user: str, max_tokens: int = 400,
        temperature: float = 0.2, progress: bool = True,
        step: float = 5.0) -> str:
    """OpenAI 와 같은 형식으로 묻는다. 나중에 클라우드로 바꿔도 이 함수만 그대로다.

    두 가지를 반드시 지킨다.

    **생각을 끈다.** Qwen3 계열은 기본으로 <think>…</think> 를 길게 쓴 뒤에야
    답을 낸다. 우리가 원하는 답은 숫자 배열 한 줄이라 생각이 전부 낭비이고,
    CPU 에서는 그 낭비가 몇 분이 된다. Qwen 이 정한 신호 `/no_think` 를 붙이고,
    템플릿 쪽으로도 꺼 달라고 알린다. 둘 다 모르는 모델이면 그냥 무시된다.

    **진행을 흘린다.** 응답을 통째로 기다리면 몇 분간 아무것도 안 찍혀
    멈춘 줄 안다 — 자막에서 겪은 것과 같은 실수다. 흘려받으며 몇 초마다
    어디까지 왔는지 찍는다.
    """
    body = json.dumps({
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user + "\n\n/no_think"}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "cache_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{server.url}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"}, method="POST")

    began = time.monotonic()
    last_at = began
    out: list[str] = []
    if progress:
        print("  [AI] 읽는 중 — 이 줄이 움직이면 잘 돌고 있는 겁니다", flush=True)

    with urllib.request.urlopen(req, timeout=ASK_TIMEOUT) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                out.append(piece)
            now = time.monotonic()
            if progress and now - last_at >= step:
                last_at = now
                got = "".join(out)
                shown = re.sub(r"\s+", " ", got[-46:]).strip()
                print(f"  [AI] {now - began:4.0f}초  {len(got):4d}자"
                      + (f"  …{shown}" if shown else "  (생각 중)"), flush=True)

    text = "".join(out)
    # 그래도 생각을 쓴 모델이면 그 부분을 떼어 낸다
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S)
    if progress:
        print(f"  [AI] 끝 — {time.monotonic() - began:.0f}초, {len(text)}자", flush=True)
    return text


# ------------------------------------------------------------------ 문장 고르기
SYSTEM = (
    "당신은 한국 교회의 주일설교 영상을 짧게 줄이는 편집자입니다. "
    "설교의 뼈대와 목사님의 핵심 메시지가 남도록 문장을 고릅니다. "
    "성경 봉독, 광고, 인사말은 고르지 않습니다. "
    "반드시 번호만 JSON 배열로 답하고 다른 말은 쓰지 않습니다."
)

PROMPT = """아래는 설교 자막입니다. 번호가 붙은 문장 중에서 {target}초 분량을 골라 주세요.

고르는 기준:
1. 목사님이 번호로 선언하신 대목(첫 번째·두 번째…)은 반드시 넣습니다.
2. 설교 전체의 핵심 주장과, 그 주장을 풀어 주는 설명을 넣습니다.
3. 성경을 소리 내어 읽는 부분, 광고, 인사말은 넣지 않습니다.
4. 서로 이어 붙였을 때 말이 통하도록, 붙어 있는 번호를 묶어서 고릅니다.
5. 설교 앞부분에만 몰리지 않게 처음부터 끝까지 고르게 고릅니다.

문장 (번호 · 길이초 · 내용):
{lines}

답: 고른 번호만 JSON 배열로. 예) [3,4,5,17,18,40]"""


def _parse_numbers(text: str, valid: set[int]) -> list[int]:
    """답에서 번호만 건져 낸다. 모델이 군말을 붙여도 견디게 만든다."""
    block = re.search(r"\[[\s\d,]*\]", text)
    raw = block.group(0) if block else text
    got = [int(x) for x in re.findall(r"\d+", raw)]
    seen: set[int] = set()
    out: list[int] = []
    for n in got:
        if n in valid and n not in seen:
            seen.add(n)
            out.append(n)
    return sorted(out)


def choose(server: Server, items: list[tuple[int, float, str]],
           target: float, progress: bool = True) -> list[int] | None:
    """(번호, 길이초, 문장) 목록에서 남길 번호를 고르게 한다.

    번호는 호출한 쪽이 매긴 것을 그대로 쓴다. 답이 범위를 벗어나면 버린다 —
    출력이 정수뿐이라 지어낼 여지가 거의 없고, 검사도 쉽다.
    """
    if not items:
        return None
    lines = "\n".join(f"{i}. ({d:.0f}초) {t}" for i, d, t in items)
    if progress:
        print(f"  [AI] 후보 {len(items)}문장을 넘깁니다 (약 {len(lines) // 2}토큰)",
              flush=True)
    reply = ask(server, SYSTEM, PROMPT.format(target=int(target), lines=lines),
                progress=progress)
    picked = _parse_numbers(reply, {i for i, _, _ in items})
    return picked or None
