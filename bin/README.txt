이 폴더의 실행파일들은 프로그램이 자동으로 찾아 씁니다. PATH 설정이 필요 없습니다.
exe 로 빌드할 때도 이 폴더가 통째로 함께 묶입니다.

  ffmpeg.exe   영상 자르기 / 오디오 변환
  ffprobe.exe  영상 길이 확인
  yt-dlp.exe   유튜브 다운로드
  deno.exe     yt-dlp 가 유튜브 포맷을 해석할 때 쓰는 JS 런타임.
               없으면 'Requested format is not available' 오류가 납니다.

각각 공식 배포처:
  https://github.com/BtbN/FFmpeg-Builds/releases
  https://github.com/yt-dlp/yt-dlp/releases
  https://github.com/denoland/deno/releases

llama/  — 3분 요약에서 'AI 고르기' 를 켰을 때 쓰는 로컬 LLM 서버입니다.
          llama.cpp b10706 의 win-cpu-x64 판에서 llama-server 하나에 필요한
          것만 골라 넣었습니다 (40MB, 파일 25개).

          CPU 세대별 DLL 14개가 들어 있어 ggml-base 가 그 PC 에 맞는 것을
          실행 중에 골라 부릅니다. 오래된 PC 든 최신 PC 든 이대로 돌아가고,
          CUDA 도 그래픽카드도 필요 없습니다.

          모델 파일(gguf, 약 2GB)은 여기 없습니다. 처음 쓸 때 내려받습니다.
              python setup_ai.py
