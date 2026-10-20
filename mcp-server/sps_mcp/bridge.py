"""Bounded, authenticated local named-pipe client. No COM or shell execution."""
from __future__ import annotations

import ctypes
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
PIPE_NAME = re.compile(r"SPS-[A-Za-z0-9_-]{1,180}\Z")
METHODS = {
    "status": set(), "layers": set(), "project.get": set(),
    "project.open": {"path"}, "project.save": {"path", "overwrite"},
    "job.start": {"action"}, "job.cancel": set(), "result.list": set(),
    "result.export_model": {"path", "overwrite"},
    "result.export_report": {"path", "overwrite"},
}


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def strict_json(data: str | bytes):
    def reject(value):
        raise ValueError("Nonfinite JSON")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(data, parse_constant=reject, object_pairs_hook=unique)


def reject_reparse(path: Path):
    """Reject symlink/junction components, including the existing parent chain."""
    for part in (path, *path.parents):
        if not part.exists() and not part.is_symlink():
            continue
        meta = part.lstat()
        if stat.S_ISLNK(meta.st_mode) or getattr(meta, "st_file_attributes", 0) & 0x400:
            raise BridgeError("UNSAFE_PATH", "Rejected a path that contains a symbolic link or junction.")


def process_alive(pid: int) -> bool:
    if os.name != "nt":
        return False
    import win32api
    import win32process
    try:
        handle = win32api.OpenProcess(0x1000, False, pid)
        try:
            return win32process.GetExitCodeProcess(handle) == 259
        finally:
            handle.Close()
    except Exception:
        return False


def default_sessions_dir() -> Path:
    # SHGetFolderPath consults the current user's Windows profile, not an MCP argument.
    if os.name != "nt":
        raise BridgeError("WINDOWS_REQUIRED", "The ArcMap connection requires Windows.")
    from win32com.shell import shell, shellcon
    return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_LOCAL_APPDATA, None, 0)) / "SpatialPredictionStudio" / "mcp" / "sessions"


@dataclass(frozen=True)
class Session:
    session_id: str
    pid: int
    pipe_name: str
    token: str = field(repr=False)
    created_utc: str = ""
    addin_version: str = ""

    def public(self):
        return {"session_id": self.session_id, "pid": self.pid,
                "created_utc": self.created_utc, "addin_version": self.addin_version}


class SessionRegistry:
    def __init__(self, directory: Path | None = None,
                 alive: Callable[[int], bool] = process_alive):
        self.directory = Path(directory) if directory is not None else default_sessions_dir()
        self.alive = alive

    def get(self, session_id: str) -> Session:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise BridgeError("SESSION_ID", "Invalid session ID.")
        path = self.directory / (session_id + ".json")
        reject_reparse(path)
        try:
            if path.stat().st_size > 16 * 1024:
                raise ValueError("Session too large")
            obj = strict_json(path.read_bytes().decode("utf-8-sig"))
            if not isinstance(obj, dict) or obj.get("schema_version") != 1:
                raise ValueError("Invalid schema")
            if obj.get("session_id") != session_id:
                raise ValueError("Session mismatch")
            pid, pipe, token = obj.get("pid"), obj.get("pipe_name"), obj.get("token")
            if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
                raise ValueError("Invalid pid")
            if not isinstance(pipe, str) or not PIPE_NAME.fullmatch(pipe):
                raise ValueError("Invalid pipe")
            if not isinstance(token, str) or not 16 <= len(token) <= 512 or any(ord(c) < 33 for c in token):
                raise ValueError("Invalid token")
            if not self.alive(pid):
                raise BridgeError("SESSION_STALE", "The ArcMap session is no longer running.")
            return Session(session_id, pid, pipe, token,
                           str(obj.get("created_utc", ""))[:100],
                           str(obj.get("addin_version", ""))[:100])
        except BridgeError:
            raise
        except (OSError, ValueError, UnicodeError, TypeError):
            raise BridgeError("SESSION_INVALID", "The session record could not be read or is invalid.") from None

    def discover(self):
        reject_reparse(self.directory)
        sessions, ignored = [], 0
        if not self.directory.exists():
            return {"sessions": [], "ignored_records": 0}
        paths = sorted(self.directory.glob("*.json"))
        if len(paths) > 256:
            raise BridgeError("SESSION_LIMIT", "Session record limit exceeded.")
        for path in paths:
            try:
                sessions.append(self.get(path.stem).public())
            except BridgeError:
                ignored += 1
        return {"sessions": sessions, "ignored_records": ignored}


class NamedPipeTransport:
    def __init__(self, timeout_seconds=35.0):
        if not 0.05 <= timeout_seconds <= 120:
            raise ValueError("Invalid pipe timeout")
        self.timeout_seconds = timeout_seconds

    def exchange(self, session: Session, payload: bytes) -> bytes:
        if os.name != "nt":
            raise BridgeError("WINDOWS_REQUIRED", "The ArcMap connection requires Windows.")
        import pywintypes
        import win32con
        import win32event
        import win32file
        import win32pipe
        deadline = time.monotonic() + self.timeout_seconds
        handle = None
        pending = None
        cancel_io = ctypes.WinDLL("kernel32", use_last_error=True).CancelIoEx
        cancel_io.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cancel_io.restype = ctypes.c_int

        def io_wait(overlap):
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            if time.monotonic() >= deadline or win32event.WaitForSingleObject(overlap.hEvent, remaining) != win32event.WAIT_OBJECT_0:
                # This handle belongs only to this call. Never cancel another caller's I/O.
                cancel_io(int(handle), None)
                win32event.WaitForSingleObject(overlap.hEvent, 1000)
                raise BridgeError("BRIDGE_TIMEOUT", "ArcMap response timed out. Do not retry a write operation automatically.")
            return win32file.GetOverlappedResult(handle, overlap, False)

        try:
            while handle is None:
                try:
                    handle = win32file.CreateFile(
                        "\\\\.\\pipe\\" + session.pipe_name,
                        win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None,
                        win32con.OPEN_EXISTING,
                        win32con.FILE_FLAG_OVERLAPPED | 0x100000 | 0x10000, None)
                except pywintypes.error as error:
                    if error.winerror != 231:
                        raise BridgeError("BRIDGE_UNAVAILABLE", "Could not open the local ArcMap connection.") from None
                    if time.monotonic() >= deadline:
                        raise BridgeError("BRIDGE_TIMEOUT", "The local ArcMap connection is busy.") from None
                    try:
                        win32pipe.WaitNamedPipe("\\\\.\\pipe\\" + session.pipe_name, 100)
                    except pywintypes.error:
                        pass
            if win32pipe.GetNamedPipeServerProcessId(handle) != session.pid:
                raise BridgeError("SESSION_MISMATCH", "The ArcMap process ID of the pipe does not match.")
            pending = pywintypes.OVERLAPPED()
            pending.hEvent = win32event.CreateEvent(None, True, False, None)
            try:
                code, _ = win32file.WriteFile(handle, payload, pending)
                if code not in (0, 997):
                    raise BridgeError("BRIDGE_WRITE", "Could not write the local request.")
                count = io_wait(pending)
                if count != len(payload):
                    raise BridgeError("BRIDGE_WRITE", "The local request was only partly written.")
            finally:
                if pending is not None:
                    pending.hEvent.Close()
                    pending = None
            data = bytearray()
            while True:
                pending = pywintypes.OVERLAPPED()
                pending.hEvent = win32event.CreateEvent(None, True, False, None)
                try:
                    code, buffer = win32file.ReadFile(handle, 64 * 1024, pending)
                    if code not in (0, 997, 234):
                        raise BridgeError("BRIDGE_READ", "Could not read the local response.")
                    count = io_wait(pending)
                    data.extend(bytes(buffer[:count]))
                finally:
                    pending.hEvent.Close()
                    pending = None
                if len(data) > MAX_RESPONSE:
                    raise BridgeError("RESPONSE_LIMIT", "The ArcMap response exceeds the size limit.")
                if b"\n" in data:
                    line, trailing = bytes(data).split(b"\n", 1)
                    if trailing.strip():
                        raise BridgeError("BRIDGE_PROTOCOL", "Received more than one response.")
                    return line
                if count == 0:
                    raise BridgeError("BRIDGE_PROTOCOL", "The ArcMap response was incomplete.")
        except BridgeError:
            raise
        except Exception:
            raise BridgeError("BRIDGE_IO", "The ArcMap connection was lost or the response could not be read.") from None
        finally:
            if handle is not None:
                cancel_io(int(handle), None)
                handle.Close()
            if pending is not None:
                win32event.WaitForSingleObject(pending.hEvent, 1000)
                pending.hEvent.Close()


class Bridge:
    def __init__(self, registry=None, transport=None):
        self.registry = registry if registry is not None else SessionRegistry()
        self.transport = transport if transport is not None else NamedPipeTransport()

    def call(self, session_id: str, method: str, params: dict | None = None):
        params = {} if params is None else params
        if method not in METHODS or not isinstance(params, dict) or set(params) != METHODS[method]:
            raise BridgeError("METHOD_PARAMS", "Invalid local operation or parameters.")
        session = self.registry.get(session_id)
        request_id = uuid.uuid4().hex
        payload = json.dumps({"id": request_id, "token": session.token,
                              "method": method, "params": params},
                             ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        if len(payload) > MAX_REQUEST:
            raise BridgeError("REQUEST_LIMIT", "The request exceeds the size limit.")
        raw = self.transport.exchange(session, payload)
        if len(raw) > MAX_RESPONSE:
            raise BridgeError("RESPONSE_LIMIT", "The response exceeds the size limit.")
        try:
            obj = strict_json(raw.decode("utf-8"))
            if not isinstance(obj, dict) or obj.get("id") != request_id or type(obj.get("ok")) is not bool:
                raise ValueError("Envelope mismatch")
            if obj["ok"]:
                if "result" not in obj:
                    raise ValueError("Result absent")
                return obj["result"]
            error = obj.get("error", {})
            code = error.get("code", "BRIDGE_ERROR")
            message = error.get("message", "The ArcMap operation could not be completed.")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", code):
                code = "BRIDGE_ERROR"
            if not isinstance(message, str):
                message = "The ArcMap operation could not be completed."
            raise BridgeError(code, message.replace(session.token, "[hidden]")[:2000])
        except BridgeError:
            raise
        except (ValueError, UnicodeError, TypeError, AttributeError):
            raise BridgeError("BRIDGE_PROTOCOL", "Invalid ArcMap response.") from None

