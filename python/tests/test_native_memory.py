from __future__ import annotations

import struct
import unittest

from wechat_receiver.native_memory import NativeMemoryError, NativeProcessMemory, read_native_string


class FakeApi:
    def __init__(
        self, memory: dict[int, bytes] | None = None, *, short_reads: bool = False,
        image_name: str = "Weixin.exe", modules: list[tuple[str, str, int, int]] | None = None,
        version: str = "4.1.13.12",
    ):
        self.memory = memory or {}
        self.short_reads = short_reads
        self.image_name = image_name
        self.module_list = modules if modules is not None else [
            ("Weixin.dll", r"C:\Program Files\Tencent\WeChat\Weixin.dll", 0x180000000, 0x1000),
        ]
        self.version = version
        self.closed = False

    def open_process(self, pid: int, access: int) -> object:
        self.pid = pid
        self.access = access
        return "handle"

    def close_handle(self, handle: object) -> None:
        self.closed = True

    def image_path(self, handle: object) -> str:
        return rf"C:\Program Files\Tencent\WeChat\{self.image_name}"

    def modules(self, handle: object) -> list[tuple[str, str, int, int]]:
        return self.module_list

    def file_version(self, path: str) -> str:
        return self.version

    def read_process_memory(self, handle: object, address: int, size: int) -> bytes:
        data = self.memory.get(address, b"")
        return data[:max(0, size - 1)] if self.short_reads else data[:size]


def descriptor(inline_or_pointer: bytes, length: int, capacity: int) -> bytes:
    return inline_or_pointer.ljust(16, b"\0") + struct.pack("<QQ", length, capacity)


class NativeProcessMemoryTests(unittest.TestCase):
    def reader(self, memory: dict[int, bytes] | None = None, *, short_reads: bool = False):
        return NativeProcessMemory(42, _api=FakeApi(memory, short_reads=short_reads))

    def test_rejects_short_read_and_address_range(self) -> None:
        reader = self.reader({0x1000: b"abcdefgh"}, short_reads=True)
        with self.assertRaisesRegex(NativeMemoryError, "short"):
            reader.read(0x1000, 8)
        with self.assertRaisesRegex(ValueError, "address"):
            reader.read(0, 1)

    def test_identity_gate_failures_close_the_open_handle(self) -> None:
        cases = (
            ("non-Weixin executable", {"image_name": "notepad.exe"}, "not a Weixin.exe"),
            ("missing Weixin module", {"modules": []}, "exactly one loaded Weixin.dll"),
            ("multiple Weixin modules", {
                "modules": [
                    ("Weixin.dll", "one.dll", 0x180000000, 0x1000),
                    ("Weixin.dll", "two.dll", 0x180010000, 0x1000),
                ],
            }, "exactly one loaded Weixin.dll"),
            ("version mismatch", {"version": "4.1.13.13"}, "unsupported Weixin.dll version"),
        )
        for name, options, error in cases:
            with self.subTest(name=name):
                fake = FakeApi(**options)
                with self.assertRaisesRegex(NativeMemoryError, error):
                    NativeProcessMemory(42, _api=fake)
                self.assertTrue(fake.closed)

    def test_native_string_rejects_invalid_layout_utf8_and_nul(self) -> None:
        with self.assertRaisesRegex(NativeMemoryError, "length exceeds capacity"):
            read_native_string(lambda address, size: descriptor(b"abc", 4, 3), 0x1000)
        with self.assertRaisesRegex(NativeMemoryError, "strict UTF-8"):
            read_native_string(lambda address, size: descriptor(b"\xff", 1, 15), 0x1000)
        with self.assertRaisesRegex(NativeMemoryError, "NUL"):
            read_native_string(lambda address, size: descriptor(b"a\0", 2, 15), 0x1000)

    def test_native_string_reads_heap_bytes_through_supplied_reader(self) -> None:
        heap = 0x2000
        calls: list[tuple[int, int]] = []
        memory = {0x1000: descriptor(struct.pack("<Q", heap), 5, 16), heap: b"Alice"}

        def read(address: int, size: int) -> bytes:
            calls.append((address, size))
            return memory[address][:size]

        self.assertEqual(read_native_string(read, 0x1000), "Alice")
        self.assertEqual(calls, [(0x1000, 32), (heap, 5)])

    def test_close_is_idempotent_and_rejects_future_reads(self) -> None:
        fake = FakeApi({0x1000: b"x"})
        reader = NativeProcessMemory(42, _api=fake)
        reader.close()
        reader.close()
        self.assertTrue(fake.closed)
        with self.assertRaisesRegex(NativeMemoryError, "closed"):
            reader.read(0x1000, 1)


if __name__ == "__main__":
    unittest.main()
