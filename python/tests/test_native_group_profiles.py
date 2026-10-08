import struct
import unittest

from wechat_receiver.native_group_profiles import read_group_member_profile
from wechat_receiver.native_memory import NativeMemoryError


class MemoryFixture:
    """Synthetic cache topology; no live process or local data is accessed."""
    base = 0x180000000
    target_version = "4.1.13.12"

    def __init__(self, nickname="群内名字"):
        self.data = {}
        self.next = 0x10000
        self.mutate = None
        self.read_counts = {}
        self.root = self.alloc(0x80)
        self.service = self.alloc(0x470)
        registry, cache = self.alloc(0x100), self.alloc(0xA0)
        self.group = self.alloc(0x120)
        self.member = self.alloc(0x30)
        self.username = self.alloc(32)
        self.nickname = self.alloc(32)
        self.putq(self.base + 0xB4B8270, self.root)
        for obj, rva in ((self.root, 0x8A3C688), (self.service, 0x8AB5948),
                         (registry, 0x8C5C7A8), (cache, 0x8C5CA38),
                         (self.group + 0x60, 0x8C64518), (self.member, 0x8C64488)):
            self.putq(obj, self.base + rva)
        self.putq(self.root + 0x68, self.service)
        self.data[self.service + 0x38] = 1
        self.text(self.service + 0x48, "wxid_bot")
        self.putq(self.service + 0x460, registry)
        self.chain(registry + 0x90, registry + 0x98, "ChatroomCache", cache)
        self.chain(cache + 0x38, cache + 0x40, "123@chatroom", self.group)
        self.text(self.group + 8, "123@chatroom")
        self.member_node = self.chain(self.group + 0xD8, self.group + 0xE0, "wxid_alice", self.member, shared=False)
        self.putq(self.member + 8, self.username)
        self.putq(self.member + 0x10, self.nickname)
        self.write(self.member + 0x28, struct.pack("<I", 3))
        self.text(self.username, "wxid_alice")
        self.nickname_data = self.text(self.nickname, nickname)

    def alloc(self, size):
        address = self.next
        self.next += size + 0x100
        self.write(address, bytes(size))
        return address

    def write(self, address, value):
        self.data.update((address + i, byte) for i, byte in enumerate(value))

    def putq(self, address, value):
        self.write(address, struct.pack("<Q", value))

    def text(self, address, value):
        raw = value.encode("utf-8")
        pointer = address
        if len(raw) < 16:
            union, capacity = raw.ljust(16, b"\0"), 15
        else:
            pointer = self.alloc(len(raw))
            self.write(pointer, raw)
            union, capacity = struct.pack("<Q", pointer) + bytes(8), len(raw)
        self.write(address, union + struct.pack("<QQ", len(raw), capacity))
        return pointer

    def chain(self, head_address, count_address, key, value, *, shared=True):
        head, node = self.alloc(16), self.alloc(0x40 if shared else 0x38)
        self.putq(head_address, head)
        self.putq(count_address, 1)
        self.putq(head, node)
        self.putq(head + 8, node)
        self.putq(node, head)
        self.putq(node + 8, head)
        self.text(node + 0x10, key)
        self.putq(node + 0x30, value)
        return node

    def read(self, address, size):
        key = (address, size)
        self.read_counts[key] = self.read_counts.get(key, 0) + 1
        if self.mutate is not None:
            self.mutate(address, size, self.read_counts[key])
        try:
            return bytes(self.data[address + i] for i in range(size))
        except KeyError:
            raise NativeMemoryError("fixture read outside allocated structure") from None


class NativeGroupProfileTests(unittest.TestCase):
    def lookup(self, memory, **overrides):
        params = dict(account_id="wxid_bot", group_id="123@chatroom", member_id="wxid_alice", observed_at_ms=100)
        return read_group_member_profile(memory, **(params | overrides))

    def test_reads_group_nickname_and_explicit_empty_value(self):
        self.assertEqual(self.lookup(MemoryFixture()).group_nickname, "群内名字")
        self.assertEqual(self.lookup(MemoryFixture("")).group_nickname, "")

    def test_unknown_group_member_or_absent_nickname_is_not_cached_as_empty(self):
        memory = MemoryFixture()
        self.assertIsNone(self.lookup(memory, group_id="456@chatroom"))
        self.assertIsNone(self.lookup(memory, member_id="wxid_bob"))
        memory.write(memory.member + 0x28, struct.pack("<I", 1))
        self.assertIsNone(self.lookup(memory))

    def test_rejects_wrong_account_version_and_mismatched_group_or_member(self):
        with self.assertRaisesRegex(NativeMemoryError, "account"):
            self.lookup(MemoryFixture(), account_id="wxid_other")
        memory = MemoryFixture()
        memory.target_version = "4.1.10.27"
        with self.assertRaisesRegex(NativeMemoryError, "version"):
            self.lookup(memory)
        for target in ("group", "username"):
            memory = MemoryFixture()
            memory.text(memory.group + 8 if target == "group" else memory.username, "other")
            with self.assertRaisesRegex(NativeMemoryError, "identity"):
                self.lookup(memory)

    def test_rejects_changed_heap_nickname_even_when_pointer_and_length_stay_equal(self):
        memory = MemoryFixture("这是一条存储在堆上的群昵称")
        def mutate(address, size, times):
            if address == memory.nickname_data and times == 2:
                memory.data[address] ^= 1
        memory.mutate = mutate
        with self.assertRaisesRegex(NativeMemoryError, "snapshot changed"):
            self.lookup(memory)

    def test_rejects_cycles_and_excessive_counts(self):
        memory = MemoryFixture()
        memory.putq(memory.member_node, memory.member_node)
        with self.assertRaises(NativeMemoryError):
            self.lookup(memory)
        memory = MemoryFixture()
        memory.putq(memory.group + 0xE0, 1_000_000)
        with self.assertRaisesRegex(NativeMemoryError, "bound"):
            self.lookup(memory)


if __name__ == "__main__":
    unittest.main()
