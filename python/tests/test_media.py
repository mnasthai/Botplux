from __future__ import annotations

from dataclasses import replace
import unittest

from wechat_receiver.media import parse_media_metadata
from wechat_receiver.normalize import normalize


def media_message(message_type: int, content: str, *, status: str = "ok"):
    message = normalize(
        {
            "kind": "item",
            "source": "receive_batch",
            "seq": 1,
            "msg_type": message_type,
            "from": "room@chatroom",
            "to": "wxid_self",
            "content": f"wxid_member:\n{content}",
            "from_read": {"status": "ok"},
            "to_read": {"status": "ok"},
            "content_read": {"status": status},
        },
        session_id="session",
        self_id="wxid_self",
    )
    assert message is not None
    return message


class MediaMetadataTests(unittest.TestCase):
    def test_image_metadata_is_bounded_candidates_and_never_acquired(self) -> None:
        message = media_message(
            3,
            '<msg><img length="123" hdlength="456" cdnthumblength="12" '
            'cdnmidwidth="640" cdnmidheight="480" md5="opaque-md5" '
            'aeskey="secret-key" cdnthumburl="file:///C:/private.jpg" '
            'cdnmidimgurl="https://example.invalid/mid" cdnbigimgurl="..\\local" /></msg>',
        )

        result = parse_media_metadata(message)

        assert result is not None and result.image is not None
        self.assertEqual("image", result.media_kind)
        self.assertEqual("parsed", result.metadata_state)
        self.assertEqual("not_acquired", result.acquisition_state)
        self.assertEqual(123, result.image.byte_length_candidate)
        self.assertEqual(456, result.image.hd_byte_length_candidate)
        self.assertEqual(12, result.image.thumbnail_byte_length_candidate)
        self.assertEqual((640, 480), (result.image.width_candidate, result.image.height_candidate))
        self.assertEqual("file:///C:/private.jpg", result.image.cdn_thumb_reference)
        self.assertEqual("..\\local", result.image.cdn_big_reference)
        image_repr = repr(result.image)
        self.assertNotIn("secret-key", image_repr)
        self.assertNotIn("private.jpg", image_repr)
        self.assertNotIn("example.invalid", image_repr)
        self.assertNotIn("secret-key", repr(result))
        self.assertNotIn("private.jpg", repr(result))

    def test_voice_metadata_keeps_unit_and_format_as_candidates(self) -> None:
        message = media_message(
            34,
            '<msg><voicemsg voicelength="2345" length="8192" voiceformat="4" '
            'clientmsgid="client-secret" bufid="buffer-secret" /></msg>',
        )

        result = parse_media_metadata(message)

        assert result is not None and result.voice is not None
        self.assertEqual("voice", result.media_kind)
        self.assertEqual("parsed", result.metadata_state)
        self.assertEqual(2345, result.voice.duration_ms_candidate)
        self.assertEqual(8192, result.voice.byte_length_candidate)
        self.assertEqual(4, result.voice.format_code_candidate)
        self.assertEqual("not_acquired", result.acquisition_state)
        self.assertNotIn("client-secret", repr(result.voice))
        self.assertNotIn("buffer-secret", repr(result.voice))
        self.assertNotIn("client-secret", repr(result))

    def test_group_prefix_is_removed_by_normalization_before_xml_parse(self) -> None:
        message = media_message(3, '<msg><img length="7" /></msg>')
        self.assertEqual('<msg><img length="7" /></msg>', message.content)
        result = parse_media_metadata(message)
        assert result is not None and result.image is not None
        self.assertEqual(7, result.image.byte_length_candidate)

    def test_incomplete_content_is_not_parsed_from_raw_content(self) -> None:
        message = media_message(34, '<msg><voicemsg voicelength="10" /></msg>', status="truncated")
        self.assertIsNone(message.content)
        self.assertIsNotNone(message.raw_content)

        result = parse_media_metadata(message)

        assert result is not None
        self.assertEqual("content_unavailable", result.metadata_state)
        self.assertEqual("truncated", result.content_read_status)
        self.assertIsNone(result.voice)

        inconsistent = replace(message, content='<msg><voicemsg voicelength="10" /></msg>')
        inconsistent_result = parse_media_metadata(inconsistent)
        assert inconsistent_result is not None
        self.assertEqual("content_unavailable", inconsistent_result.metadata_state)
        self.assertIsNone(inconsistent_result.voice)

    def test_rejects_dtd_invalid_xml_wrong_root_and_nested_payload(self) -> None:
        cases = (
            ("unsafe_xml", '<!DOCTYPE msg [<!ENTITY x "1">]><msg><img length="&x;" /></msg>'),
            ("invalid_xml", "<msg><img></msg>"),
            ("unexpected_root", "<other><img /></other>"),
            ("missing_payload", "<msg><outer><img /></outer></msg>"),
            ("ambiguous_payload", "<msg><img /><img /></msg>"),
        )
        for expected, xml in cases:
            with self.subTest(expected=expected):
                result = parse_media_metadata(media_message(3, xml))
                assert result is not None
                self.assertEqual(expected, result.metadata_state)
                self.assertEqual("not_acquired", result.acquisition_state)

    def test_rejects_oversized_xml_before_parsing(self) -> None:
        result = parse_media_metadata(media_message(3, "<msg>" + "x" * 17_000 + "</msg>"))
        assert result is not None
        self.assertEqual("too_large", result.metadata_state)

    def test_invalid_numbers_and_long_sensitive_values_are_omitted_with_notes(self) -> None:
        message = media_message(
            34,
            '<msg><voicemsg voicelength="-1" length="99999999999999999999" '
            f'voiceformat="four" clientmsgid="{"x" * 300}" /></msg>',
        )

        result = parse_media_metadata(message)

        assert result is not None and result.voice is not None
        self.assertIsNone(result.voice.duration_ms_candidate)
        self.assertIsNone(result.voice.byte_length_candidate)
        self.assertIsNone(result.voice.format_code_candidate)
        self.assertIsNone(result.voice.client_message_id_candidate)
        self.assertEqual(
            (
                "invalid_integer_attribute:voicelength",
                "invalid_integer_attribute:length",
                "invalid_integer_attribute:voiceformat",
                "attribute_too_long:clientmsgid",
            ),
            result.quality_notes,
        )

    def test_empty_missing_and_non_media_are_distinct(self) -> None:
        empty = media_message(3, "")
        missing = replace(empty, content=None, raw_content=None)
        non_media = replace(empty, message_type=1, message_kind="text")

        empty_result = parse_media_metadata(empty)
        missing_result = parse_media_metadata(missing)
        assert empty_result is not None and missing_result is not None
        self.assertEqual("empty", empty_result.metadata_state)
        self.assertEqual("content_unavailable", missing_result.metadata_state)
        self.assertIsNone(parse_media_metadata(non_media))


if __name__ == "__main__":
    unittest.main()
