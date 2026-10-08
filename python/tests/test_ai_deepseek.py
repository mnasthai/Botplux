"""Contract tests for the stateless DeepSeek provider."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler

from wechat_receiver.ai.deepseek_provider import DeepSeekError, DeepSeekProvider


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]


class DeepSeekProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.key_file = Path(self.temporary.name) / "deepseek.key"
        self.key_file.write_text("fake-key", encoding="utf-8")
        self.provider = DeepSeekProvider(self.key_file)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_posts_only_system_and_current_user_without_tools_or_history(self) -> None:
        response = _Response(b'{"choices":[{"finish_reason":"stop","message":{"content":"answer"}}]}')
        with patch("wechat_receiver.ai.deepseek_provider._direct_urlopen", return_value=response) as opener:
            self.assertEqual("answer", self.provider.complete("current", instructions="rules"))
        request = opener.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            [{"role": "system", "content": "rules"}, {"role": "user", "content": "current"}],
            payload["messages"],
        )
        self.assertEqual("none", payload["tool_choice"])
        self.assertEqual({"type": "disabled"}, payload["thinking"])
        self.assertFalse(payload["stream"])
        self.assertNotIn("tools", payload)
        self.assertEqual([1024 * 1024 + 1], response.read_sizes)

    def test_failure_is_safe_and_never_retries(self) -> None:
        error = HTTPError("https://api.deepseek.com/chat/completions", 401, "Unauthorized", {}, None)
        with patch("wechat_receiver.ai.deepseek_provider._direct_urlopen", side_effect=error) as opener:
            with self.assertRaisesRegex(DeepSeekError, "HTTP status 401") as caught:
                self.provider.complete("hello")
        self.assertEqual(1, opener.call_count)
        self.assertNotIn("fake-key", str(caught.exception))
        self.assertEqual("deepseek_http_401", caught.exception.code)

    def test_timeout_is_safe_and_never_retries(self) -> None:
        with patch("wechat_receiver.ai.deepseek_provider._direct_urlopen", side_effect=socket.timeout) as opener:
            with self.assertRaisesRegex(DeepSeekError, "could not be completed") as caught:
                self.provider.complete("hello")
        self.assertEqual(1, opener.call_count)
        self.assertNotIn("fake-key", str(caught.exception))
        self.assertEqual("deepseek_timeout", caught.exception.code)

    def test_inherited_proxy_is_ignored_without_changing_the_environment(self) -> None:
        proxy = 'http://127.0.0.1:7892'
        response = _Response(b'{"choices":[{"finish_reason":"stop","message":{"content":"answer"}}]}')
        with patch.dict(os.environ, {'HTTPS_PROXY': proxy}), \
                patch('urllib.request.getproxies', return_value={'https': proxy}), \
                patch('urllib.request.OpenerDirector.open', autospec=True, return_value=response) as open_request:
            self.assertEqual('answer', self.provider.complete('hello'))
            opener = open_request.call_args.args[0]
            self.assertFalse(any(isinstance(handler, ProxyHandler) and handler.proxies for handler in opener.handlers))
            self.assertEqual(proxy, os.environ['HTTPS_PROXY'])

    def test_connection_refused_has_a_safe_error_code_without_leaking_reason(self) -> None:
        error = URLError(ConnectionRefusedError(10061, 'sensitive network detail fake-key'))
        with patch('wechat_receiver.ai.deepseek_provider._direct_urlopen', side_effect=error) as opener:
            with self.assertRaises(DeepSeekError) as caught:
                self.provider.complete('hello')
        self.assertEqual('deepseek_connection_refused', caught.exception.code)
        self.assertEqual(1, opener.call_count)
        self.assertNotIn('sensitive', str(caught.exception))
        self.assertNotIn('fake-key', str(caught.exception))

    def test_invalid_or_unusable_responses_fail(self) -> None:
        cases = (
            b"not json",
            b'{"choices":[]}',
            b'{"choices":["not a choice"]}',
            b'{"choices":[{"message":"not a message"}]}',
            b'{"choices":[{"finish_reason":"length","message":{"content":"cut off"}}]}',
            b'{"choices":[{"finish_reason":"stop","message":{"refusal":"no","content":""}}]}',
            b'{"choices":[{"finish_reason":"stop","message":{"tool_calls":[{}],"content":null}}]}',
            b'{"choices":[{"finish_reason":"stop","message":{"content":"   "}}]}',
        )
        for body in cases:
            with self.subTest(body=body), patch(
                "wechat_receiver.ai.deepseek_provider._direct_urlopen", return_value=_Response(body)
            ):
                with self.assertRaises(DeepSeekError):
                    self.provider.complete("hello")

    def test_key_file_is_private_and_rejects_blank_newline_or_placeholder(self) -> None:
        self.assertTrue(self.provider.configured())
        for value in ("", "fake-key\nsecond", "YOUR_API_KEY"):
            self.key_file.write_text(value, encoding="utf-8")
            self.assertFalse(self.provider.configured())
            with self.assertRaises(DeepSeekError):
                self.provider.complete("hello")
