import io
import math
import unittest
import wave
from array import array

from wechat_receiver.audio_codec import (
    SILK_HEADER, _encode, _packets, prepare_voice_data, voice_to_wav,
)


def wav_fixture(rate=16000, channels=1, milliseconds=240):
    samples = array("h", (int(4000 * math.sin(i * 440 * 2 * math.pi / rate))
                          for i in range(rate * milliseconds // 1000)
                          for _ in range(channels)))
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(samples.tobytes())
    return output.getvalue()


class AudioCodecTests(unittest.TestCase):
    def test_wav_mono_stereo_and_44100_round_trip(self):
        for rate, channels in ((16000, 1), (44100, 2), (48000, 2)):
            with self.subTest(rate=rate, channels=channels):
                encoded, duration = prepare_voice_data(wav_fixture(rate, channels, 241))
                self.assertTrue(encoded.startswith(SILK_HEADER))
                self.assertEqual(duration, 260)
                self.assertEqual(len(_packets(encoded)), 13)
                with wave.open(io.BytesIO(voice_to_wav(encoded)), "rb") as stream:
                    self.assertEqual(stream.getframerate(), 24000)
                    self.assertEqual(stream.getnframes(), 6240)
                    self.assertEqual(stream.getnchannels(), 1)

    def test_multiframe_packet_is_not_mistaken_for_20ms(self):
        pcm = array("h", (int(5000 * math.sin(i / 8)) for i in range(24000 // 5)))
        original = _encode(pcm.tobytes(), 24000, packet_ms=100)
        self.assertEqual(len(_packets(original)), 2)
        encoded, duration = prepare_voice_data(original)
        self.assertEqual(duration, 200)
        self.assertEqual(len(_packets(encoded)), 10)
        self.assertEqual(prepare_voice_data(encoded), (encoded, 200))

    def test_standard_silk_header_and_terminator_normalized(self):
        encoded, duration = prepare_voice_data(wav_fixture())
        self.assertEqual(prepare_voice_data(encoded[1:] + b"\xff\xff"),
                         (encoded, duration))

    def test_corruption_and_duration_bounds(self):
        encoded, _ = prepare_voice_data(wav_fixture())
        for bad in (b"", b"#!SILK_V3", encoded[:-1],
                    SILK_HEADER + b"\x20\x00short", SILK_HEADER + b"\x01\x00\xff",
                    wav_fixture()[:-10], b"RIFFbroken"):
            with self.subTest(size=len(bad)), self.assertRaises(ValueError):
                prepare_voice_data(bad)
        with self.assertRaises(ValueError):
            prepare_voice_data(SILK_HEADER + b"\0\0" * 3001)
        with self.assertRaises(ValueError):
            prepare_voice_data(wav_fixture(milliseconds=60_001))


if __name__ == "__main__":
    unittest.main()
