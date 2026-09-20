import io
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot


class NotifierEncodingTests(unittest.TestCase):
    def setUp(self):
        self.notifier = bot.TelegramNotifier()
        self.notifier.token = "offline-test-token"
        self.notifier.chat_id = "offline-test-chat"
        self.notifier.print_messages = True
        self.message = "🟢 [ENTRY] 金银行情提醒\n🔵 [CLOSE] 结束跟踪"
        self.response = Mock(status_code=200)
        self.response.json.return_value = {"ok": True}

    def test_gbk_console_preserves_telegram_text_and_prints_readable_fallback(self):
        buffer = io.BytesIO()
        with io.TextIOWrapper(buffer, encoding="gbk") as output, \
             patch("sys.stdout", output), patch("bot.requests.post", return_value=self.response) as post:
            self.assertTrue(self.notifier.send(self.message))
            output.flush()
            printed = buffer.getvalue().decode("gbk")
        self.assertIn("金银行情提醒", printed)
        self.assertIn("\\U0001f7e2", printed)
        self.assertEqual(post.call_args.kwargs["json"]["text"], self.message)

    def test_utf8_console_preserves_emoji(self):
        buffer = io.BytesIO()
        with io.TextIOWrapper(buffer, encoding="utf-8", newline="\n") as output, \
             patch("sys.stdout", output), patch("bot.requests.post", return_value=self.response):
            self.assertTrue(self.notifier.send(self.message))
            output.flush()
            self.assertIn(self.message, buffer.getvalue().decode("utf-8"))

    def test_gbk_printing_does_not_turn_delivery_failure_into_success(self):
        self.response.status_code = 429
        with io.TextIOWrapper(io.BytesIO(), encoding="gbk") as output, \
             patch("sys.stdout", output), patch("bot.requests.post", return_value=self.response), \
             self.assertLogs("alert_bot", level="WARNING"):
            self.assertFalse(self.notifier.send(self.message))

    def test_stringio_capture_without_encoding_is_supported(self):
        with patch("sys.stdout", io.StringIO()) as output, \
             patch("bot.requests.post", return_value=self.response):
            self.assertTrue(self.notifier.send(self.message))
            self.assertIn(self.message, output.getvalue())

    def test_unrelated_output_errors_are_not_swallowed(self):
        output = Mock(encoding="utf-8")
        output.write.side_effect = OSError("output unavailable")
        with patch("sys.stdout", output), patch("bot.requests.post") as post:
            with self.assertRaises(OSError):
                self.notifier.send(self.message)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
