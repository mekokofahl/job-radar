import unittest
from unittest import mock

import notify

TOKEN = "123456:SECRET-TOKEN"
CFG = {"telegram": {"bot_token": TOKEN, "chat_id": "42"}}


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}
    def json(self):
        return self._body


class Chunking(unittest.TestCase):
    def test_short_messages_are_sent_whole(self):
        self.assertEqual(notify._chunks("hello"), ["hello"])

    def test_long_messages_are_split_under_the_telegram_limit(self):
        parts = notify._chunks("a line of text\n" * 1000)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) < 4096 for p in parts))
        self.assertEqual("".join(p + "\n" for p in parts).count("a line of text"), 1000)    # nothing lost

    def test_a_single_enormous_line_is_cut(self):
        self.assertTrue(all(len(p) < 4096 for p in notify._chunks("x" * 10000)))


class Sending(unittest.TestCase):
    def test_each_chunk_is_posted(self):
        with mock.patch.object(notify.requests, "post", return_value=FakeResponse()) as post:
            notify.send_telegram(CFG, "a line of text\n" * 1000)
        self.assertGreater(post.call_count, 1)

    def test_missing_credentials_raise_a_clear_error(self):
        with self.assertRaises(RuntimeError):
            notify.send_telegram({"telegram": {}}, "hi")

    def test_http_errors_never_expose_the_bot_token(self):
        with mock.patch.object(notify.requests, "post", return_value=FakeResponse(400, {"description": "Bad Request: message is too long"})):
            with self.assertRaises(RuntimeError) as ctx:
                notify.send_telegram(CFG, "x")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn("400", str(ctx.exception))

    def test_connection_errors_never_expose_the_bot_token(self):
        boom = notify.requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")
        with mock.patch.object(notify.requests, "post", side_effect=boom):
            with self.assertRaises(RuntimeError) as ctx:
                notify.send_telegram(CFG, "x")
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_escape_helper(self):
        self.assertEqual(notify.esc("<b>A & B</b>"), "&lt;b&gt;A &amp; B&lt;/b&gt;")
        self.assertEqual(notify.esc(None), "")


if __name__ == "__main__":
    unittest.main()
