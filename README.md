# 창의진교회 설교영상 편집기 (sermon-tool)

유튜브 예배 영상(1~2시간)에서 **설교 구간만** 뽑아 mp4 하나로 만드는 윈도우 프로그램.
축도는 선택으로 붙일 수 있고, 자막·mp3·시작 영상도 함께 만든다.

```
유튜브 URL ─┐
            ├─→ 목사님 성문으로 설교 찾기 ─→ 사람이 확인·미세조정 ─→ mp4 (+자막/mp3)
내 컴퓨터 영상 ┘        (80분 영상 75초)          (마우스로)
```

**쓰는 사람은 교회 담당자다.** 컴퓨터를 잘 모른다고 가정하고 만들었다.
주 1회, 주일 예배 뒤에 쓴다. 이 전제가 UI 결정의 대부분을 설명한다.

---

## 5분 만에 돌려보기

```powershell
# Windows, 정식 출시판 Python 3.11~3.13 (3.10.0rc1 같은 rc 판은 빌드가 깨진다)
cd D:\homily\sermon-tool
pip install -r requirements.txt
python app.py
```

가장 쉬운 방법은 `run.bat` 을 두 번 클릭하는 것이다. 프로젝트 안에 `.venv` 가 없으면
정식판 Python(3.11 → 3.12 → 3.13 순)으로 만들고 `requirements.txt` 를 깐 뒤 GUI 를 띄운다.
이미 `.venv` 가 있으면 바로 실행한다. `auto_run.bat` · `build_all.bat` 도 `.venv` 가 있으면 그것을 먼저 쓴다.

터미널에서 직접 돌릴 때는 시스템 `python` 대신 가상환경 것을 쓴다 (전역 파이썬에는 패키지가 없다):

```powershell
.venv\Scripts\python.exe app.py
.venv\Scripts\python.exe analyze.py doctor    # 무엇이 준비됐고 무엇이 없는지 한 번에 본다
```

`bin/` 안에 ffmpeg·ffprobe·yt-dlp·deno 실행파일이 이미 들어 있으므로 따로 설치할 것이 없다.
프로그램이 켜지면 [진행 로그] 탭 맨 위에 무엇이 준비됐고 무엇이 없는지 찍어 준다.
거기가 문제 파악의 출발점이다.

동작을 코드로 확인하려면:

```powershell
python selftest.py     # 외부 도구·모델 없이 검출/보정/자막 로직만 검증
```

---

## 파일별 책임

| 파일 | 하는 일 | 줄 수 |
|---|---|---|
| `app.py` | GUI 전부. 화면·재생·구간 편집·작업 실행 | ~2000 |
| `ui_summary.py` | 편집기의 [3분 요약] 탭 (app.py 에 섞어 쓰는 조각) | ~470 |
| `core/voiceprint.py` | **핵심.** 성문 등록·스캔·설교/축도 판정 | ~340 |
| `core/cut.py` | 자르기·이어붙이기·음량/잡음·자막 굽기·mp3·시작 영상 | ~400 |
| `core/export.py` | 내보내기 조립 (자르기 → 시작영상 → 자막 → 파일명) | ~290 |
| `core/util.py` | 외부 실행파일 찾기, 진행률 스트리밍, 시간 포맷 | ~310 |
| `core/config.py` | 설정 한 곳. 경로는 개발/설치본에 따라 갈린다 | ~170 |
| `core/marker.py` | 구간 데이터 모델 (AI 예측 / 사람 수정본 / 편집 구간) | ~120 |
| `core/fetch.py` | yt-dlp 로 영상·오디오 받기 | ~175 |
| `core/detect.py` | 검출 결과를 마커에 반영 | ~200 |
| `core/transcribe.py` | faster-whisper 자막 (선택 기능) | ~110 |
| `core/refine.py` | 자막 문장 단위로 경계 다듬기 | ~110 |
| `core/pauses.py` | 오디오에서 목사님이 숨 고르는 자리 찾기 | ~180 |
| `core/highlight.py` | 3분 요약 문장 고르기 (규칙 → 임베딩 → AI) | ~500 |
| `core/embed.py` | 문장 임베딩 (ONNX, CPU) — 뜻으로 대표성 재기 | ~170 |
| `core/llm.py` | 로컬 소형 LLM (llama.cpp) — 후보 중 최종 선택 | ~230 |
| `core/shorts.py` | 세로(9:16) 요약 영상 만들기 | ~290 |
| `core/diarize.py` | pyannote 화자 분리 — **느린 폴백 전용** | ~250 |
| `analyze.py` | CLI 진입점 (doctor / analyze / export / show / set) | ~320 |
| `auto.py` | 채널 감시 자동 처리 (setup / once / watch / install-task) | ~350 |
| `enroll.py` | 성문 만들기·검증 (build / test / info) | ~160 |
| `check_speakers.py` | 화자분리 시절의 전제 검증 스크립트. 지금은 참고용 | ~80 |
| `build_exe.py` | PyInstaller 빌드 | ~240 |
| `setup_ai.py` | 임베딩·AI 모델 받기 + 세 방식 견주어 보기 | ~180 |
| `selftest.py` | 외부 의존 없는 로직 테스트 | ~200 |
| `guitest.py` | 화면을 실제로 만들어 눌러 보는 검사 (PySide6 필요) | ~160 |

---

## 고쳤으면 돌릴 것

```bash
python selftest.py    # 외부 도구 없이 도는 로직 검사
python guitest.py     # 화면을 실제로 만들어 눌러 보는 검사 (PySide6 필요)
```

---

## 실행 방법 세 가지

| | 명령 | 언제 |
|---|---|---|
| GUI | `python app.py` | 평소. 교회 담당자가 쓰는 화면 |
| CLI | `python analyze.py run <URL>` | 서버·자동화·디버깅 |
| 자동 | `python auto.py watch` | 채널을 감시해 새 예배가 올라오면 처리 |

---

## 검출 방식 — 성문(voiceprint)

목사님 목소리의 지문(`data/voiceprint.npz`, 2KB)을 미리 만들어 두고,
3초씩 잘라 코사인 유사도를 잰다. 설교 구간 안 0.80, 밖 0.36~0.40 으로 갈린다 (임계값 0.6).

| | 성문 | 화자 분리(pyannote) |
|---|---|---|
| 80분 영상 처리 | **75초** | 30~90분 |
| 필요한 것 | sherpa-onnx + 28MB 모델 | torch + 2GB 모델 + HF 토큰 |
| 정확도 (IoU) | **97.0% / 97.8%** | 비슷 |

**화자 분리로 되돌리지 말 것.** CPU 전용 PC에 설치해야 하는데 torch 를 넣으면
설치본이 2GB 넘게 불어난다. 성문이 없을 때만 폴백으로 쓴다.

목사님이 바뀌면 → `docs/HANDOVER.md` 의 "목사님이 바뀌면" 절.

---

## 데이터가 쌓이는 곳

| | 경로 |
|---|---|
| 개발 중 (`python app.py`) | `sermon-tool/data/` |
| 설치본 (`SermonTool.exe`) | `%LOCALAPPDATA%\SermonTool\data\` |

`data/` 아래: `preview`(받은 영상) · `audio` · `cache`(자막 캐시) · `markers`(구간 JSON) ·
`export`(결과물) · `voiceprint.npz`(성문).

GUI 설정(자막 방식·잡음·축도·시작 영상)은 파일이 아니라 윈도우 레지스트리에
`QSettings("창의진교회", "SermonTool")` 로 저장된다.

---

## exe 만들기

```powershell
build_all.bat
```

정식 출시판 Python 을 3.13 → 3.12 → 3.11 순으로 찾아 쓴다.
**출시 후보(rc) 판에서는 PyInstaller 가 `IndexError` 로 죽는다** — 원인을 패키지에서
찾다가 몇 시간을 버린 적이 있다. 결과물은 `dist\SermonTool\` 폴더째 복사해서 배포한다.

---

## 문서 지도

| 문서 | 읽는 사람 |
|---|---|
| `docs/사용설명서.md` (+ .docx) | 교회 담당자. 화면 그대로 따라 하는 설명. `.md` 를 고쳤으면 `.docx` 도 다시 만든다 (방법은 `.md` 맨 위 주석) |
| `docs/구현문서.md` | 개발자. 알고리즘·자료구조·함정 |
| `docs/HANDOVER.md` | **인수받는 개발자. 여기부터 읽는다** |
| `docs/변경기록_20260831_3분요약.md` | 3분 요약 기능을 왜 이렇게 만들었는지 · 실측 숫자 |
| `CLAUDE.md` / `AGENTS.md` | AI 에이전트에게 주는 규칙 (내용 동일) |

---

## 처음 만지는 사람이 반드시 알아야 할 일곱 가지

1. **설교자는 언제나 "목사님"이라고 부른다.** 화면·로그·문서 전부. 코드 식별자는 `pastor`.
2. **CUDA 없는 PC에서 돌아야 한다.** 런타임에 torch 를 쓰지 않는다.
3. **마우스만으로 다 되어야 한다.** 단축키는 있어도 되지만, 없어도 모든 기능을 쓸 수 있어야 한다.
4. **문자열 치환으로 코드를 고쳤으면 실제로 바뀌었는지 확인한다.** 앵커가 안 맞으면
   조용히 아무 일도 일어나지 않는다. 실제로 `ZoomBar is not defined` 로 한참 헤맸다.
5. **`data/preview/*.mp4` 를 함부로 지우지 않는다.** 같은 이름의 `.webm` 은 병합 중인
   오디오 트랙일 수 있다. 지웠다가 "소리 없는 영상" 오류를 만든 적이 있다.
6. **오래 걸리는 일은 반드시 진행 상황을 흘린다.** 조용하면 쓰는 분은 멈춘 줄 안다.
   설교 35분 자막이 CPU 로 20~60분 걸리는데 아무것도 안 찍혀 신고가 들어온 적이 있다.
7. **작업이 도는 동안에는 구간을 만질 수 없게 잠근다** (`_set_busy`). 작업이 끝나면
   `_apply_marker` 가 구간을 새로 넣기 때문에, 도는 중의 편집은 소리 없이 사라진다.
   버튼뿐 아니라 `range_bar`·`zoom_bar`·목록까지 잠가야 한다.
