"""페이징 파일을 넉넉하게 잡아 준다 — [메모리 넉넉하게 만들기] 의 속.

왜 필요한가 (2026-10-20):
    이 PC 는 메모리 13.8GB 가 달렸는데 윈도우가 통틀어 내주는 양(커밋 한도)이
    14.7GB 였다. 페이징 파일이 896MB 로 잡혀 있었기 때문이다. 그래서 메모리가
    6.4GB 남아 있어도 실제로 더 쓸 수 있는 양은 0.5GB 뿐이고, 영상을 만들려면
    ffmpeg 이 "Cannot allocate memory" 로 멈춘다.

    고치는 길은 윈도우 고급 설정에서 페이징 파일을 고정 크기로 늘리는 것뿐인데,
    교회에서 쓰시는 분들은 그 화면까지 들어가기 어렵다. 그래서 프로그램이
    대신 해 드린다.

어떻게 하나:
    관리자 권한이 필요하므로 PowerShell 을 '관리자로 실행' 해서 띄운다
    (윈도우가 "이 앱이 변경하도록 허용하시겠습니까?" 를 한 번 묻는다).
    바꾼 뒤에는 **윈도우를 다시 시작해야** 한다 — 커밋 한도는 부팅 때 정해진다.

지키는 선:
    - C: 드라이브만 건드린다.
    - 디스크 여유를 보고 크기를 정한다. 여유가 없으면 아예 하지 않는다.
    - 이미 더 크게 잡혀 있으면 줄이지 않는다.
    - 언제든 '자동 관리' 로 되돌릴 수 있다고 알려 준다.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from . import machine

DRIVE = "C:"
_SPARE_MB = 10240          # 페이징 파일을 잡은 뒤에도 남겨 둘 디스크 여유


def _is_windows() -> bool:
    """윈도우인가. 검사에서 바꿔 끼울 수 있게 함수로 둔다."""
    return os.name == "nt"


def disk_free_mb(drive: str = DRIVE) -> float | None:
    """그 드라이브의 남은 공간(MB). 못 보면 None."""
    try:
        return shutil.disk_usage(drive + "\\" if os.name == "nt" else "/").free / 2 ** 20
    except Exception:
        return None


def pagefile_mb() -> float | None:
    """지금 잡혀 있는 페이징 파일 크기(MB) = 커밋 한도 − 실제 메모리."""
    limit, ram = machine.commit_limit_mb(), machine.total_mb()
    if limit is None or ram is None:
        return None
    return max(0.0, limit - ram)


def needs_fix() -> bool:
    """늘려야 하는 상태인가.

    페이징 파일이 실제 메모리의 절반도 안 되면 좁다고 본다. 윈도우 기본값은
    보통 메모리만큼 잡히므로, 그보다 훨씬 작다면 뭔가 잘못 잡힌 것이다.
    """
    if not _is_windows():
        return False
    now, ram = pagefile_mb(), machine.total_mb()
    if now is None or ram is None:
        return False
    return now < ram * 0.5


def plan() -> dict:
    """무엇을 어떻게 바꿀지 정한다.

    돌려주는 것: {"ok": bool, "initial": MB, "max": MB, "why": 사람이 읽을 말}
    ok 가 False 면 why 에 왜 못 하는지가 들어 있다.
    """
    if not _is_windows():
        return {"ok": False, "why": "윈도우에서만 할 수 있습니다."}

    ram = machine.total_mb()
    now = pagefile_mb()
    free = disk_free_mb()
    if ram is None or free is None:
        return {"ok": False, "why": "이 컴퓨터의 메모리·디스크 상태를 읽지 못했습니다."}

    # 메모리만큼 잡고 최대는 그 두 배. 커밋 한도가 메모리의 3배쯤 되어
    # 자막·AI·영상 만들기가 겹쳐도 넉넉해진다.
    want_init = int(round(ram / 1024)) * 1024
    want_max = want_init * 2

    # 디스크 사정에 맞춘다. 최대 크기만큼 실제로 커질 수 있으니 여유를 남긴다.
    room = free - _SPARE_MB
    if room < 8192:
        return {"ok": False,
                "why": (f"C: 드라이브 여유가 {free / 1024:.1f}GB 뿐입니다. "
                        f"페이징 파일을 넉넉히 잡으려면 최소 "
                        f"{(8192 + _SPARE_MB) / 1024:.0f}GB 는 비어 있어야 합니다.\n"
                        "필요 없는 영상 파일을 지우고 다시 눌러 주세요.")}
    if want_max > room:
        want_max = int(room // 1024) * 1024
        want_init = min(want_init, want_max)

    if now is not None and now >= want_init:
        return {"ok": False,
                "why": (f"페이징 파일이 이미 {now / 1024:.1f}GB 로 넉넉합니다. "
                        "메모리가 모자란 까닭은 다른 데 있습니다 — "
                        "돌고 있는 다른 프로그램을 닫아 보세요.")}

    limit = machine.commit_limit_mb() or ram
    after = ram + want_init
    return {
        "ok": True, "initial": want_init, "max": want_max,
        "why": (f"C: 드라이브의 페이징 파일을 {want_init / 1024:.0f}GB"
                f"(최대 {want_max / 1024:.0f}GB) 로 고정합니다.\n\n"
                f"지금  — 메모리 {ram / 1024:.1f}GB + 페이징 파일 "
                f"{(now or 0) / 1024:.1f}GB = 통틀어 {limit / 1024:.1f}GB\n"
                f"바꾼 뒤 — 메모리 {ram / 1024:.1f}GB + 페이징 파일 "
                f"{want_init / 1024:.0f}GB = 통틀어 {after / 1024:.1f}GB 이상\n\n"
                f"C: 여유 {free / 1024:.1f}GB 중 {want_init / 1024:.0f}GB 를 "
                "이 파일이 차지합니다.\n"
                "윈도우가 '변경을 허용하시겠습니까' 를 한 번 묻습니다. "
                "허용하신 뒤 **윈도우를 다시 시작하면** 적용됩니다.\n"
                "나중에 되돌리려면 같은 화면에서 '자동 관리' 로 바꾸면 됩니다."),
    }


_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$out = @{ ok = $false; message = '' }
try {
    $cs = Get-CimInstance Win32_ComputerSystem
    if ($cs.AutomaticManagedPagefile) {
        Set-CimInstance -InputObject $cs -Property @{ AutomaticManagedPagefile = $false }
    }
    $name = '__DRIVE__\pagefile.sys'
    # 이름으로 걸러낸다. WQL 필터에 역슬래시를 넣는 것보다 확실하다.
    $pf = Get-CimInstance Win32_PageFileSetting |
          Where-Object { $_.Name -like '__DRIVE__*' } | Select-Object -First 1
    if ($pf) {
        Set-CimInstance -InputObject $pf -Property @{ InitialSize = __INIT__; MaximumSize = __MAX__ }
    } else {
        New-CimInstance -ClassName Win32_PageFileSetting -Property @{
            Name = $name; InitialSize = __INIT__; MaximumSize = __MAX__ } | Out-Null
    }
    $chk = Get-CimInstance Win32_PageFileSetting |
           Where-Object { $_.Name -like '__DRIVE__*' } | Select-Object -First 1
    if (-not $chk) { throw '설정이 저장되지 않았습니다.' }
    $out.ok = $true
    $out.message = "설정했습니다: 처음 크기 $($chk.InitialSize)MB, 최대 $($chk.MaximumSize)MB"
} catch {
    $out.message = $_.Exception.Message
}
$out | ConvertTo-Json | Set-Content -Path '__RESULT__' -Encoding UTF8
'''


def apply(say=None) -> dict:
    """정한 대로 바꾼다. 관리자 권한을 한 번 묻는다.

    돌려주는 것: {"ok": bool, "message": str, "reboot": bool}
    """
    tell = say or (lambda t: None)
    p = plan()
    if not p.get("ok"):
        return {"ok": False, "message": p.get("why", "할 수 없습니다."), "reboot": False}

    work = Path(tempfile.mkdtemp(prefix="sermon_pagefile_"))
    result = work / "result.json"
    script = work / "set_pagefile.ps1"
    body = (_SCRIPT.replace("__DRIVE__", DRIVE)
            .replace("__INIT__", str(int(p["initial"])))
            .replace("__MAX__", str(int(p["max"])))
            .replace("__RESULT__", str(result).replace("\\", "\\\\")))
    # PowerShell 은 BOM 이 있는 UTF-8 을 가장 안전하게 읽는다 (한글 때문).
    script.write_text(body, encoding="utf-8-sig")

    tell("윈도우에 권한을 묻습니다 — '예' 를 눌러 주세요")
    code = _run_elevated("powershell.exe",
                         f'-NoProfile -ExecutionPolicy Bypass -File "{script}"')
    if code is None:
        return {"ok": False, "reboot": False,
                "message": ("권한을 받지 못했습니다. 다시 누르시고 윈도우가 물을 때 "
                            "'예' 를 눌러 주세요.")}

    if result.exists():
        try:
            got = json.loads(result.read_text(encoding="utf-8-sig"))
        except Exception:
            got = {"ok": False, "message": "결과를 읽지 못했습니다."}
    else:
        got = {"ok": False, "message": f"설정이 끝나지 않았습니다 (끝 번호 {code})."}

    ok = bool(got.get("ok"))
    msg = str(got.get("message", ""))
    if ok:
        msg += ("\n\n이제 윈도우를 다시 시작해 주세요. "
                "통틀어 쓸 수 있는 양은 켜질 때 정해지므로, "
                "다시 시작하기 전까지는 그대로입니다.")
    try:
        shutil.rmtree(work, ignore_errors=True)
    except Exception:
        pass
    return {"ok": ok, "message": msg, "reboot": ok}


def _run_elevated(exe: str, args: str, timeout_ms: int = 180000) -> int | None:
    """관리자 권한으로 띄우고 끝나기를 기다린다. 끝 번호, 못 띄우면 None."""
    if not _is_windows():
        return None
    import ctypes
    import ctypes.wintypes as wt

    class _Info(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("fMask", ctypes.c_ulong),
                    ("hwnd", wt.HWND), ("lpVerb", ctypes.c_wchar_p),
                    ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                    ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int),
                    ("hInstApp", wt.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", ctypes.c_wchar_p), ("hkeyClass", wt.HKEY),
                    ("dwHotKey", wt.DWORD), ("hIcon", wt.HANDLE),
                    ("hProcess", wt.HANDLE)]

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(_Info)]
    shell.ShellExecuteExW.restype = wt.BOOL
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.WaitForSingleObject.restype = wt.DWORD
    k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    k32.GetExitCodeProcess.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]

    info = _Info()
    info.cbSize = ctypes.sizeof(_Info)
    info.fMask = 0x00000040            # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"              # 이것이 관리자 권한 요청이다
    info.lpFile = exe
    info.lpParameters = args
    info.nShow = 0                     # SW_HIDE
    if not shell.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
        return None                    # 사용자가 '아니요' 를 눌렀을 때도 여기로 온다
    try:
        k32.WaitForSingleObject(info.hProcess, timeout_ms)
        code = wt.DWORD()
        if not k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            return None
        return int(code.value)
    finally:
        k32.CloseHandle(info.hProcess)
