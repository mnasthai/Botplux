"""Bounded, local-only Windows named-pipe transport for sender protocol v1."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import struct
import time
from typing import Any


MAX_FRAME_BYTES = 65_536
DEFAULT_PIPE = r"\\.\pipe\wechatbot-send-v1"


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


class TransportError(RuntimeError):
    def __init__(self, error_code: str, detail: str, *, stage: str, may_have_written: bool = False):
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail
        self.stage = stage
        self.may_have_written = may_have_written


class FrameError(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FrameError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise FrameError(f"non-finite JSON value: {value}")


def _validate_flat(message: object) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise FrameError("payload must be a JSON object")
    for key, value in message.items():
        if not isinstance(key, str) or isinstance(value, (dict, list, float)):
            raise FrameError("payload must be a flat JSON object without floating-point values")
        if value is not None and not isinstance(value, (str, int, bool)):
            raise FrameError("payload contains an unsupported JSON value")
        try:
            key.encode("utf-8", "strict")
            if isinstance(value, str):
                value.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise FrameError("payload contains a lone Unicode surrogate") from exc
    return message


def encode_message(message: dict[str, Any], *, max_payload: int = MAX_FRAME_BYTES) -> bytes:
    _validate_flat(message)
    try:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FrameError("message is not strict UTF-8 JSON") from exc
    if not payload or len(payload) > max_payload:
        raise FrameError(f"payload length must be between 1 and {max_payload} bytes")
    return struct.pack("<I", len(payload)) + payload


def decode_payload(payload: bytes, *, max_payload: int = MAX_FRAME_BYTES) -> dict[str, Any]:
    if not payload or len(payload) > max_payload:
        raise FrameError(f"payload length must be between 1 and {max_payload} bytes")
    try:
        value = json.loads(payload.decode("utf-8", "strict"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise FrameError("payload is not strict UTF-8 JSON") from exc
    return _validate_flat(value)


def decode_frame(frame: bytes, *, max_payload: int = MAX_FRAME_BYTES) -> dict[str, Any]:
    if len(frame) < 4:
        raise FrameError("frame is shorter than its length prefix")
    size = struct.unpack("<I", frame[:4])[0]
    if size < 1 or size > max_payload:
        raise FrameError("declared payload length is out of bounds")
    if len(frame) != size + 4:
        raise FrameError("frame length does not match its prefix")
    return decode_payload(frame[4:], max_payload=max_payload)


def validate_local_pipe_path(path: str) -> str:
    if not isinstance(path, str) or "\x00" in path or not path.startswith("\\\\.\\pipe\\"):
        raise ValueError(r"pipe path must use the local \\.\pipe\ namespace")
    name = path[len("\\\\.\\pipe\\"):]
    if not name or "\\" in name or "/" in name or name in {".", ".."}:
        raise ValueError("pipe name must be one non-empty local component")
    return path


class NamedPipeTransport:
    """Perform exactly one request/response exchange per pipe connection."""

    def __init__(self, pipe_path: str = DEFAULT_PIPE, *, timeout: float = 5.0, max_payload: int = MAX_FRAME_BYTES):
        self.pipe_path = validate_local_pipe_path(pipe_path)
        if not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
            raise ValueError("timeout must be a positive finite number")
        if not 1 <= max_payload <= MAX_FRAME_BYTES:
            raise ValueError(f"max_payload must be between 1 and {MAX_FRAME_BYTES}")
        self.timeout = float(timeout)
        self.max_payload = max_payload

    def exchange(self, message: dict[str, Any]) -> dict[str, Any]:
        try:
            frame = encode_message(message, max_payload=self.max_payload)
        except FrameError as exc:
            raise TransportError("invalid_request", str(exc), stage="encode") from exc
        if os.name != "nt":
            raise TransportError("pipe_unavailable", "Windows named pipes are unavailable on this platform", stage="connect")
        deadline = time.monotonic() + self.timeout
        handle = self._open(deadline)
        try:
            self._write_all(handle, frame, deadline)
            try:
                prefix = self._read_exact(handle, 4, deadline)
                size = struct.unpack("<I", prefix)[0]
                if size < 1 or size > self.max_payload:
                    raise TransportError("invalid_frame", "response payload length is out of bounds", stage="response", may_have_written=True)
                payload = self._read_exact(handle, size, deadline)
                try:
                    return decode_payload(payload, max_payload=self.max_payload)
                except FrameError as exc:
                    raise TransportError("invalid_json", str(exc), stage="response", may_have_written=True) from exc
            except TransportError as exc:
                if not exc.may_have_written:
                    raise TransportError(exc.error_code, exc.detail, stage=exc.stage, may_have_written=True) from exc
                raise
        finally:
            self._close_handle(handle)

    @staticmethod
    def _close_handle(handle) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)

    @staticmethod
    def _remaining_ms(deadline: float) -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0
        return max(1, min(0xFFFFFFFE, int(remaining * 1000)))

    def _open(self, deadline: float):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
        kernel32.WaitNamedPipeW.restype = wintypes.BOOL
        kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        wait = self._remaining_ms(deadline)
        if wait == 0:
            raise TransportError("timeout", "named pipe connection timed out", stage="connect")
        if not kernel32.WaitNamedPipeW(self.pipe_path, wait):
            code = ctypes.get_last_error()
            name = "timeout" if code in (121, 258) or wait == 0 else "pipe_unavailable"
            raise TransportError(name, f"named pipe unavailable (WinError {code})", stage="connect")
        handle = kernel32.CreateFileW(self.pipe_path, 0xC0000000, 0, None, 3, 0x40000000, None)
        if handle in (0, wintypes.HANDLE(-1).value):
            code = ctypes.get_last_error()
            raise TransportError("pipe_unavailable", f"cannot open named pipe (WinError {code})", stage="connect")
        return handle

    def _overlapped(self):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.POINTER(_OVERLAPPED)
        kernel32.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.ReadFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), pointer)
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = kernel32.ReadFile.argtypes
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetOverlappedResult.argtypes = (wintypes.HANDLE, pointer, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL)
        kernel32.GetOverlappedResult.restype = wintypes.BOOL
        kernel32.CancelIoEx.argtypes = (wintypes.HANDLE, pointer)
        kernel32.CancelIoEx.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        event = kernel32.CreateEventW(None, True, False, None)
        if not event:
            raise TransportError("winapi_error", "cannot create I/O event", stage="io")
        return kernel32, _OVERLAPPED(0, 0, 0, 0, event)

    def _io(self, handle, data, deadline: float, *, write: bool) -> int:
        if self._remaining_ms(deadline) == 0:
            raise TransportError("timeout", "named pipe I/O timed out before it started",
                                 stage="write" if write else "response", may_have_written=not write)
        kernel32, ov = self._overlapped()
        done = wintypes.DWORD()
        started = False
        completed = False
        try:
            buf = (ctypes.c_char * len(data)).from_buffer(data) if not write else ctypes.create_string_buffer(data)
            fn = kernel32.WriteFile if write else kernel32.ReadFile
            started = True
            ok = fn(handle, buf, len(data), ctypes.byref(done), ctypes.byref(ov))
            if ok:
                completed = True
                return done.value
            code = ctypes.get_last_error()
            if not write and code == 234 and done.value:  # ERROR_MORE_DATA on message-mode pipes
                completed = True
                return done.value
            if code != 997:  # ERROR_IO_PENDING
                completed = True
                raise TransportError("pipe_io_error", f"pipe I/O failed (WinError {code})", stage="write" if write else "response", may_have_written=not write)
            wait = kernel32.WaitForSingleObject(ov.hEvent, self._remaining_ms(deadline))
            if wait != 0:
                kernel32.CancelIoEx(handle, ctypes.byref(ov))
                # OVERLAPPED and its buffer must remain alive until the
                # cancelled operation has actually completed.
                kernel32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(done), True)
                completed = True
                if wait == 258:
                    raise TransportError("timeout", "named pipe I/O timed out", stage="write" if write else "response", may_have_written=True)
                raise TransportError("pipe_io_error", "waiting for named pipe I/O failed", stage="write" if write else "response", may_have_written=True)
            result = kernel32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(done), False)
            completed = True
            if not result:
                code = ctypes.get_last_error()
                raise TransportError("pipe_io_error", f"pipe I/O failed (WinError {code})", stage="write" if write else "response", may_have_written=True)
            return done.value
        finally:
            if started and not completed:
                kernel32.CancelIoEx(handle, ctypes.byref(ov))
                kernel32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(done), True)
            kernel32.CloseHandle(ov.hEvent)

    def _write_all(self, handle, frame: bytes, deadline: float) -> None:
        offset = 0
        while offset < len(frame):
            try:
                count = self._io(handle, frame[offset:], deadline, write=True)
            except TransportError as exc:
                if offset and not exc.may_have_written:
                    raise TransportError(exc.error_code, exc.detail, stage=exc.stage, may_have_written=True) from exc
                raise
            if count <= 0:
                raise TransportError("pipe_closed", "pipe closed during request write", stage="write", may_have_written=offset > 0)
            offset += count

    def _read_exact(self, handle, size: int, deadline: float) -> bytes:
        result = bytearray(size)
        offset = 0
        while offset < size:
            view = memoryview(result)[offset:]
            count = self._io(handle, view, deadline, write=False)
            if count <= 0:
                raise TransportError("pipe_closed", "pipe closed before a complete response", stage="response", may_have_written=True)
            offset += count
        return bytes(result)
