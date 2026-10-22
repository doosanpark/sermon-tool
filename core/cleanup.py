"""지난번에 비정상으로 끝나며 남은 우리 프로그램을 정리한다.

왜 필요한가 (2026-10-20):
    설교편집기가 메모리 부족으로 창째 사라지는 일이 여러 번 있었다. 그때
    우리가 띄워 둔 llama-server.exe·ffmpeg.exe 는 부모가 죽어도 그대로 남는다.
    llama-server 는 2.5GB 모델을 올린 채라 메모리를 크게 물고 있고, 다음에
    프로그램을 켜면 그만큼 모자란 상태에서 시작한다. 쓰시는 분은 작업 관리자를
    열어 하나씩 끝낼 수 없으니, 켤 때 프로그램이 알아서 치운다.

무엇을 끝내나 — **우리 폴더 안의 것만**:
    프로그램 폴더(또는 설치 폴더) 아래에 있는 llama-server.exe 와 ffmpeg.exe 만
    본다. 같은 이름이라도 다른 곳에서 돌고 있으면 건드리지 않는다. 쓰시는 분이
    따로 켜 둔 프로그램을 꺼 버리는 일이 없게 하려는 것이다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .util import IS_FROZEN, PROJECT_ROOT

# 우리가 띄우는 도우미들. 이 이름이면서 우리 폴더 안에 있을 때만 끝낸다.
HELPERS = ("llama-server.exe", "ffmpeg.exe", "ffprobe.exe", "yt-dlp.exe")

_MAX_PATH = 32768


def _roots() -> list[Path]:
    """우리 것으로 볼 폴더들.

    빌드본일 때만 실행파일이 있는 폴더를 더한다. 소스로 돌릴 때 이걸 더하면
    파이썬 설치 폴더(C:\\Python312 같은 곳)까지 "우리 것" 이 되어 버린다.
    """
    out = [Path(PROJECT_ROOT).resolve()]
    if IS_FROZEN:
        try:
            out.append(Path(sys.executable).resolve().parent)
        except Exception:
            pass
    return out


def _inside(path: Path, roots: list[Path]) -> bool:
    try:
        rp = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


_k32 = None


def _win32():
    """kernel32 를 쓸 수 있게 손봐서 돌려준다. 윈도우가 아니면 None.

    반환형을 꼭 지정한다. 안 하면 ctypes 가 핸들을 32비트 정수로 받아서
    64비트 윈도우에서 값이 잘릴 수 있고, 그러면 엉뚱한 핸들을 닫는다.
    """
    global _k32
    if os.name != "nt":
        return None
    if _k32 is not None:
        return _k32
    import ctypes
    import ctypes.wintypes as wt

    # 공용 windll.kernel32 대신 우리 몫을 따로 연다. 반환형을 손대도
    # 다른 곳(machine.py 의 메모리 읽기)에 영향이 없게 하려는 것이다.
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k.OpenProcess.restype = wt.HANDLE
    k.CloseHandle.argtypes = [wt.HANDLE]
    k.CloseHandle.restype = wt.BOOL
    k.TerminateProcess.argtypes = [wt.HANDLE, ctypes.c_uint]
    k.TerminateProcess.restype = wt.BOOL
    k.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k.WaitForSingleObject.restype = wt.DWORD
    k.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD,
                                             ctypes.c_wchar_p,
                                             ctypes.POINTER(wt.DWORD)]
    k.QueryFullProcessImageNameW.restype = wt.BOOL
    _k32 = k
    return k


def _list_processes() -> list[tuple[int, int, str]]:
    """지금 돌고 있는 (번호, 부모 번호, 실행파일 이름) 목록. 윈도우가 아니면 빈 목록."""
    k = _win32()
    if k is None:
        return []
    import ctypes
    import ctypes.wintypes as wt

    class _Entry(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_Entry)]
    k.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_Entry)]
    snap = k.CreateToolhelp32Snapshot(0x00000002, 0)       # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    out: list[tuple[int, int, str]] = []
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID),
                        entry.szExeFile))
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return out


def _exe_path(pid: int) -> Path | None:
    """그 프로세스의 실행파일 자리. 못 보면 None (권한 없는 것들이 있다)."""
    k = _win32()
    if k is None:
        return None
    import ctypes
    import ctypes.wintypes as wt

    h = k.OpenProcess(0x1000, False, pid)        # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        size = wt.DWORD(_MAX_PATH)
        buf = ctypes.create_unicode_buffer(_MAX_PATH)
        if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        return Path(buf.value)
    finally:
        k.CloseHandle(h)


def leftovers() -> list[tuple[int, Path]]:
    """지난번에 남은 우리 도우미들. (번호, 경로).

    **켤 때만 부른다.** 돌고 있는 중에 부르면 지금 일하고 있는 ffmpeg 까지
    "남은 것" 으로 잡힌다.

    **부모가 살아 있으면 남은 것이 아니다.** 프로그램을 실수로 두 번 켜는
    일이 흔한데, 그때 두 번째 창이 첫 번째 창의 ffmpeg 을 죽이면 안 된다.
    고아가 된 도우미는 부모 번호가 이미 사라진 번호를 가리킨다. (번호가
    다른 프로그램에 재사용됐을 수는 있다 — 그러면 살아 있다고 보고 놔둔다.
    못 치우는 쪽이 잘못 죽이는 쪽보다 낫다.)
    """
    k = _win32()
    if k is None:
        return []
    roots = _roots()
    me = os.getpid()
    procs = _list_processes()
    alive = {pid for pid, _parent, _name in procs}
    found: list[tuple[int, Path]] = []
    for pid, parent, name in procs:
        if pid == me or name.lower() not in HELPERS:
            continue
        if parent in alive:
            continue                                # 누군가의 자식 — 남은 것이 아니다
        path = _exe_path(pid)
        if path is not None and _inside(path, roots):
            found.append((pid, path))
    return found


def sweep(found: list[tuple[int, Path]] | None = None, say=None) -> int:
    """남은 것들을 끝낸다. 끝낸 개수를 돌려준다.

    found 를 주면 그 목록을 쓴다 (leftovers() 로 미리 적어 두고 그것을 치울 때).
    끝나기를 잠깐(최대 3초) 기다린다. 안 기다리면 바로 뒤에서 남은 메모리를
    읽을 때 아직 안 풀린 숫자가 찍힌다.
    실패하면 조용히 넘어간다 — 청소하다 프로그램이 안 켜지면 더 나쁘다.
    """
    k = _win32()
    if k is None:
        return 0
    done = 0
    for pid, path in (leftovers() if found is None else found):
        h = k.OpenProcess(0x0001 | 0x00100000, False, pid)   # TERMINATE|SYNCHRONIZE
        if not h:
            if say:
                say(f"{path.name} (번호 {pid}) 은 권한이 없어 못 치웠습니다")
            continue
        try:
            if k.TerminateProcess(h, 1):
                k.WaitForSingleObject(h, 3000)
                done += 1
                if say:
                    say(f"지난번에 남아 있던 {path.name} 을(를) 치웠습니다 (번호 {pid})")
        finally:
            k.CloseHandle(h)
    return done
