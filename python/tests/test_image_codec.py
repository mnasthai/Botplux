import struct
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import zlib

from wechat_receiver import image_codec


def _nal(nal_type: int, payload: bytes = b"\x80", *, short_code: bool = False) -> bytes:
    start_code = b"\0\0\1" if short_code else b"\0\0\0\1"
    return start_code + bytes((nal_type << 1, 1)) + payload


def _hevc(*, pictures: int = 1, padding: int = 32) -> bytes:
    return (
        _nal(32) + _nal(33) + _nal(34)
        + b"".join(_nal(19, b"\x80" + b"x" * padding) for _ in range(pictures))
    )


def _wxgf(partitions: list[bytes], width: int = 2, height: int = 2) -> bytes:
    header = b"wxgf" + bytes((19,)) + b"\0\2"
    header += width.to_bytes(2, "big") + height.to_bytes(2, "big") + b"\0" * 8
    return header + b"".join(len(item).to_bytes(4, "big") + item for item in partitions)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        len(payload).to_bytes(4, "big") + kind + payload
        + (zlib.crc32(kind + payload) & 0xffffffff).to_bytes(4, "big")
    )


def _png(width: int = 2, height: int = 2) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\0" + b"\0\0\0" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(pixels)) + _chunk(b"IEND", b"")
    )


def _ue(value: int) -> str:
    encoded = value + 1
    suffix = f"{encoded:b}"
    return "0" * (len(suffix) - 1) + suffix


def _sps(
    width: int, height: int, *, chroma: int = 1, crop=(0, 0, 0, 0)
) -> bytes:
    fields = "0000" + "000" + "1"  # VPS id, no sublayers, temporal nesting
    fields += "0" * 96  # general profile_tier_level
    fields += _ue(0) + _ue(chroma) + _ue(width) + _ue(height)
    fields += "1" + "".join(_ue(value) for value in crop)
    fields += "1"  # rbsp_stop_one_bit
    fields += "0" * (-len(fields) % 8)
    payload = int(fields, 2).to_bytes(len(fields) // 8, "big")
    return bytes((33 << 1, 1)) + payload


class ImageCodecTests(unittest.TestCase):
    def test_static_main_partition_decodes_to_validated_png(self):
        main = _hevc(padding=200)
        ancillary = _hevc(padding=1)
        container = _wxgf([main, ancillary])
        geometry = image_codec._HevcGeometry(2, 2, 2, 2, 2, 2)
        with patch.object(image_codec, "_sps_geometry", return_value=geometry), \
                patch.object(image_codec, "_decode_hevc", return_value=_png()) as decode:
            self.assertEqual(image_codec.decode_wxgf(container), _png())
        decode.assert_called_once_with(main, 2, 2, crop_chroma_padding=False)

        short_stream = b"".join(_nal(kind, short_code=True) for kind in (32, 33, 34, 19))
        with patch.object(image_codec, "_sps_geometry", return_value=geometry), \
                patch.object(image_codec, "_decode_hevc", return_value=_png()) as decode:
            image_codec.decode_wxgf(_wxgf([short_stream]))
        decode.assert_called_once_with(short_stream, 2, 2, crop_chroma_padding=False)

    def test_sps_geometry_allows_only_chroma_rounding_remainder(self):
        geometry = image_codec._parse_sps(_sps(1712, 1280, crop=(0, 3, 0, 0)))
        self.assertEqual(
            geometry,
            image_codec._HevcGeometry(1712, 1280, 1706, 1280, 2, 2),
        )
        self.assertTrue(image_codec._needs_chroma_crop(geometry, 1706, 1279))
        self.assertFalse(image_codec._needs_chroma_crop(geometry, 1706, 1280))
        for dimensions in ((1704, 1279), (1706, 1278), (1707, 1279)):
            with self.subTest(dimensions=dimensions), self.assertRaises(ValueError):
                image_codec._needs_chroma_crop(geometry, *dimensions)

        with self.assertRaisesRegex(ValueError, "decode limit"):
            image_codec._parse_sps(_sps(5000, 2))

    def test_rejects_animation_and_multiple_pictures(self):
        equal = _hevc(padding=20)
        with self.assertRaisesRegex(ValueError, "animated"):
            image_codec.decode_wxgf(_wxgf([equal, equal]))
        with self.assertRaisesRegex(ValueError, "animated"):
            image_codec.decode_wxgf(_wxgf([_hevc(pictures=2, padding=20)]))

    def test_header_partition_and_input_bounds(self):
        good = _wxgf([_hevc()])
        bad_inputs = [
            bytearray(good),
            b"",
            b"nope" + good[4:],
            good[:4] + b"\x0a" + good[5:],
            good[:4] + b"\xff" + good[5:],
            _wxgf([_hevc()], width=4097),
            _wxgf([_hevc()], width=4096, height=4096),
            good[:19] + (9999).to_bytes(4, "big") + good[23:],
        ]
        for bad in bad_inputs:
            with self.subTest(length=len(bad)), self.assertRaises(ValueError):
                image_codec.decode_wxgf(bad)  # type: ignore[arg-type]

    def test_missing_parameter_sets_and_truncated_nals_are_rejected(self):
        cases = [
            _nal(19),
            _nal(32) + _nal(33) + _nal(34) + b"\0\0\0\1\x26",
            _nal(32) + _nal(33) + _nal(34),
        ]
        for stream in cases:
            with self.subTest(length=len(stream)), self.assertRaises(ValueError):
                image_codec.decode_wxgf(_wxgf([stream]))

    def test_png_validation_checks_dimensions_crc_and_completion(self):
        valid = _png()
        image_codec._validate_png(valid, 2, 2)
        corrupt = bytearray(valid)
        corrupt[-1] ^= 1
        for bad, width, height in (
            (bytes(corrupt), 2, 2),
            (valid, 3, 2),
            (valid[:-12], 2, 2),
        ):
            with self.subTest(length=len(bad)), self.assertRaises(ValueError):
                image_codec._validate_png(bad, width, height)

    def test_ffmpeg_call_is_bounded_local_and_rejects_second_frame(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "ffmpeg.exe"
            executable.write_bytes(b"exe")

            calls = []

            def fake_run(arguments, **kwargs):
                calls.append((arguments, kwargs))
                output = Path(arguments[-1].replace("%02d", "01"))
                output.write_bytes(_png())
                return subprocess.CompletedProcess(arguments, 0)

            with patch.object(image_codec, "_ffmpeg_executable", return_value=executable), \
                    patch.object(image_codec.subprocess, "run", side_effect=fake_run):
                self.assertEqual(image_codec._decode_hevc(_hevc(), 2, 2), _png())

            arguments, kwargs = calls[0]
            self.assertIn("-protocol_whitelist", arguments)
            self.assertEqual(arguments[arguments.index("-protocol_whitelist") + 1], "file")
            self.assertEqual(kwargs["timeout"], 15)
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertNotIn("-vf", arguments)

            calls.clear()
            with patch.object(image_codec, "_ffmpeg_executable", return_value=executable), \
                    patch.object(image_codec.subprocess, "run", side_effect=fake_run):
                image_codec._decode_hevc(
                    _hevc(), 2, 2, crop_chroma_padding=True
                )
            arguments, _ = calls[0]
            self.assertEqual(
                arguments[arguments.index("-vf") + 1],
                "format=rgb24,crop=2:2:0:0",
            )

            def two_frames(arguments, **kwargs):
                for number in ("01", "02"):
                    Path(arguments[-1].replace("%02d", number)).write_bytes(_png())
                return subprocess.CompletedProcess(arguments, 0)

            with patch.object(image_codec, "_ffmpeg_executable", return_value=executable), \
                    patch.object(image_codec.subprocess, "run", side_effect=two_frames), \
                    self.assertRaisesRegex(ValueError, "animated"):
                image_codec._decode_hevc(_hevc(), 2, 2)


if __name__ == "__main__":
    unittest.main()
