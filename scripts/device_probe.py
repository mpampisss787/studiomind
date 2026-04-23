# name=StudioMind Sandbox Probe
# url=https://github.com/anchinous/studiomind
# supportedDevices=StudioMindProbe

# One-shot diagnostic script to test what FL Studio's embedded Python allows.
# Purpose: verify whether FL's Python sandbox permits sockets, filesystem,
# ctypes, and named pipes, to determine if the SysEx-over-MIDI transport can
# be replaced with a driver-free IPC mechanism.
#
# USAGE:
#   1. Drop this file in:
#      FL Studio\Settings\Hardware\StudioMindProbe\device_probe.py
#   2. FL Studio -> Options -> MIDI Settings -> set Controller type to
#      "StudioMind Sandbox Probe" on some Input row and tick Enable.
#   3. Open View -> Script output (or check the hint bar).
#   4. The probe runs automatically on script load (OnInit).
#   5. Results are written to %TEMP%\studiomind_probe.log AND printed to FL's
#      Script output window.

import os
import sys
import time
import traceback

_results = []


def _log(line):
    _results.append(line)
    try:
        print(line)
    except Exception:
        pass


def _probe(name, fn):
    _log("")
    _log("[PROBE] " + name)
    try:
        detail = fn()
        _log("  OK: " + str(detail))
        return True
    except Exception as e:
        _log("  FAIL: " + type(e).__name__ + ": " + str(e))
        tb = traceback.format_exc().splitlines()
        for line in tb[-4:]:
            _log("    " + line)
        return False


def probe_python_info():
    return "python " + sys.version + " | platform=" + sys.platform + " | exe=" + sys.executable


def probe_import_socket():
    import socket
    return "socket module imported, AF_INET=" + str(socket.AF_INET)


def probe_import_os():
    return "os.getcwd()=" + os.getcwd()


def probe_import_ctypes():
    import ctypes
    pid = ctypes.windll.kernel32.GetCurrentProcessId()
    return "ctypes ok, GetCurrentProcessId=" + str(pid)


def probe_import_subprocess():
    import subprocess  # noqa: F401
    return "subprocess module imported"


def probe_import_threading():
    import threading
    return "threading ok, current=" + threading.current_thread().name


def probe_write_file():
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "studiomind_probe_write_test.txt")
    with open(path, "w") as f:
        f.write("hello from FL Python at " + str(time.time()))
    size = os.path.getsize(path)
    os.remove(path)
    return "wrote and deleted " + path + " (" + str(size) + " bytes)"


def probe_tcp_bind():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        return "bound on 127.0.0.1:" + str(port) + " (listen OK)"
    finally:
        s.close()


def probe_named_pipe_create():
    import ctypes
    from ctypes import wintypes

    PIPE_ACCESS_DUPLEX = 0x3
    PIPE_TYPE_MESSAGE = 0x4
    PIPE_READMODE_MESSAGE = 0x2
    PIPE_WAIT = 0x0
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE

    pipe_name = r"\\.\pipe\studiomind_probe"
    handle = kernel32.CreateNamedPipeW(
        pipe_name,
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
        1, 4096, 4096, 0, None,
    )
    if handle == INVALID_HANDLE_VALUE or handle is None:
        err = kernel32.GetLastError()
        raise RuntimeError("CreateNamedPipeW failed, GetLastError=" + str(err))
    kernel32.CloseHandle(handle)
    return "pipe created and closed: " + pipe_name


def probe_fl_modules():
    import device  # noqa: F401
    import general
    import ui
    return "FL modules OK | api=" + str(general.getVersion()) + " fl=" + str(ui.getVersion())


def _run_all_probes():
    _log("=" * 60)
    _log("StudioMind Sandbox Probe")
    _log("ts=" + str(time.time()))
    _log("=" * 60)

    _probe("python_info", probe_python_info)
    _probe("import os", probe_import_os)
    _probe("import threading", probe_import_threading)
    _probe("import socket", probe_import_socket)
    _probe("import ctypes", probe_import_ctypes)
    _probe("import subprocess", probe_import_subprocess)
    _probe("write file to TEMP", probe_write_file)
    _probe("bind localhost TCP socket", probe_tcp_bind)
    _probe("create named pipe (ctypes)", probe_named_pipe_create)
    _probe("FL API modules", probe_fl_modules)

    _log("")
    _log("=" * 60)
    _log("DONE.")
    _log("=" * 60)

    try:
        import tempfile
        log_path = os.path.join(tempfile.gettempdir(), "studiomind_probe.log")
        with open(log_path, "w") as f:
            f.write("\n".join(_results))
        try:
            import ui
            ui.setHintMsg("StudioMind probe: " + log_path)
        except Exception:
            pass
    except Exception as e:
        try:
            import ui
            ui.setHintMsg("StudioMind probe: log write FAILED: " + str(e))
        except Exception:
            pass


def OnInit():
    _run_all_probes()


def OnDeInit():
    pass


def OnMidiMsg(event):
    pass
