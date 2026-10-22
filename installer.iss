; SermonTool 설치본 스크립트 (Inno Setup 6)
;   1) https://jrsoftware.org/isdl.php 에서 Inno Setup 설치
;   2) python build_exe.py  로 dist\SermonTool 생성
;   3) 이 파일을 Inno Setup 에서 열고 Build > Compile  (또는 아래 명령)
;      "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;   결과: Output\SermonToolSetup.exe

#define AppName "SermonTool"
#define AppVerName "창의진교회 설교영상 편집기"
; 버전은 core/version.py 한 곳에서 정한다. build_exe.py 가 version.iss 를 만들어 준다.
#include "version.iss"
#define AppPublisher "homily"
#define AppExeName "SermonTool.exe"

[Setup]
AppId={{8F3C21A4-6C4E-4E2B-9E71-2F0B5A7D91C2}
AppName={#AppVerName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppVerName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=SermonToolSetup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 설치본이 크므로 디스크 여유를 미리 확인시킨다
DiskSpanning=no
UninstallDisplayName={#AppVerName} {#AppVersion}

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 만들기"; GroupDescription: "추가 아이콘:"
Name: "startmenuicon"; Description: "시작 메뉴 바로가기 만들기"; GroupDescription: "추가 아이콘:"; Flags: checkedonce

[Files]
; build_exe.py 가 만든 원폴더 전체를 그대로 담는다
;   - SermonTool.exe, PySide6, sherpa-onnx(성문 검출)
;   - bin\ : ffmpeg, ffprobe, yt-dlp, deno, models\campplus.onnx
;   - voiceprint.npz : 목사 성문 (사용자 폴더에 없으면 이걸 쓴다)
;   - 고칠말.txt : 지금까지 모아 둔 오인식 사전 (첫 실행 때 사용자 폴더로 옮겨 간다)
; models\ 는 아래에서 따로 담는다 — 여기서는 뺀다
Source: "dist\SermonTool\*"; DestDir: "{app}"; Excludes: "models\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

; AI 모델 (뜻 보기 135MB + 설교로 학습시킨 모델 2.5GB).
; gguf·onnx 는 이미 압축된 파일이라 다시 압축해 봐야 거의 안 줄고 시간만 몇십 분
; 걸린다. nocompression 으로 그대로 담는다. 설치본이 2.6GB 쯤 커지는 대신,
; 받는 분이 따로 내려받을 것이 없다.
Source: "dist\SermonTool\models\*"; DestDir: "{app}\models"; Flags: ignoreversion recursesubdirs createallsubdirs nocompression

[Icons]
Name: "{group}\{#AppVerName}"; Filename: "{app}\{#AppExeName}"; Tasks: startmenuicon
Name: "{group}\{cm:UninstallProgram,{#AppVerName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppVerName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "지금 실행"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 사용자 데이터(%LOCALAPPDATA%\SermonTool)는 지우지 않는다 — 받아둔 영상과 구간이 들어 있다
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
