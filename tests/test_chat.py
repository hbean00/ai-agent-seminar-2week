import asyncio
import tempfile
import unittest
from pathlib import Path

from src.chat import ChatChannel, ChatFile, IncomingMessage, SentMessage


class FakeSlackClient:
    """Records the Web API calls chat.py makes, the way the real client takes them."""

    def __init__(self, ts: str = "1700000000.000100"):
        self.ts = ts
        self.posted = []
        self.updated = []
        self.uploaded = []

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ok": True, "ts": self.ts}

    async def chat_update(self, **kwargs):
        self.updated.append(kwargs)
        return {"ok": True}

    async def files_upload_v2(self, **kwargs):
        self.uploaded.append(kwargs)
        return {"ok": True}


def _event(**overrides):
    event = {
        "type": "message",
        "text": "안녕",
        "user": "U0OWNER",
        "channel": "C0ALLOWED",
        "channel_type": "channel",
        "ts": "1700000000.000001",
    }
    event.update(overrides)
    return event


class ChatFileTests(unittest.TestCase):
    def test_reads_bytes_from_a_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "art.svg"
            path.write_bytes(b"<svg/>")
            self.assertEqual(ChatFile("art.svg", path=path).read_bytes(), b"<svg/>")

    def test_reads_bytes_held_in_memory(self):
        self.assertEqual(ChatFile("response.md", data=b"hi").read_bytes(), b"hi")

    def test_a_file_with_neither_source_raises_rather_than_uploading_nothing(self):
        with self.assertRaises(ValueError):
            ChatFile("empty.txt").read_bytes()


class ChatChannelTests(unittest.TestCase):
    def test_send_posts_text_into_the_thread_and_returns_an_editable_message(self):
        client = FakeSlackClient()
        channel = ChatChannel(client, "C0ALLOWED", "1700000000.000001")

        sent = asyncio.run(channel.send("작업중입니다."))

        self.assertEqual(len(client.posted), 1)
        self.assertEqual(client.posted[0]["channel"], "C0ALLOWED")
        self.assertEqual(client.posted[0]["text"], "작업중입니다.")
        self.assertEqual(client.posted[0]["thread_ts"], "1700000000.000001")
        self.assertIsInstance(sent, SentMessage)
        self.assertEqual(sent.ts, client.ts)

    def test_send_with_only_files_uploads_and_returns_nothing_to_edit(self):
        client = FakeSlackClient()
        channel = ChatChannel(client, "C0ALLOWED", "1700000000.000001")

        sent = asyncio.run(channel.send(files=[ChatFile("a.txt", data=b"a")]))

        self.assertIsNone(sent)
        self.assertEqual(len(client.posted), 0)
        self.assertEqual(len(client.uploaded), 1)
        self.assertEqual(client.uploaded[0]["thread_ts"], "1700000000.000001")
        self.assertEqual(client.uploaded[0]["file_uploads"], [{"filename": "a.txt", "content": b"a"}])

    def test_text_and_files_go_as_two_calls_so_the_text_stays_editable(self):
        # The ack that carries the working GIF is edited for the rest of the
        # job. Posting the text as the upload's initial_comment would leave
        # nothing to edit, which is the regression this pins.
        client = FakeSlackClient()
        channel = ChatChannel(client, "C0ALLOWED", None)

        sent = asyncio.run(channel.send("본문", files=[ChatFile("response.md", data=b"body")]))

        self.assertIsNotNone(sent)
        self.assertEqual(len(client.posted), 1)
        self.assertEqual(len(client.uploaded), 1)
        self.assertNotIn("initial_comment", client.uploaded[0])

    def test_send_with_nothing_to_say_makes_no_api_call(self):
        client = FakeSlackClient()
        channel = ChatChannel(client, "C0ALLOWED")

        self.assertIsNone(asyncio.run(channel.send()))
        self.assertEqual(client.posted, [])
        self.assertEqual(client.uploaded, [])

    def test_multiple_files_ride_one_upload_call(self):
        client = FakeSlackClient()
        channel = ChatChannel(client, "C0ALLOWED")
        files = [ChatFile(f"f{i}.txt", data=b"x") for i in range(3)]

        asyncio.run(channel.send(files=files))

        self.assertEqual(len(client.uploaded), 1)
        self.assertEqual(len(client.uploaded[0]["file_uploads"]), 3)


class SentMessageTests(unittest.TestCase):
    def test_edit_updates_by_channel_and_ts(self):
        # Discord identified a message by id alone; Slack needs both, and
        # losing either one silently edits nothing.
        client = FakeSlackClient()
        message = SentMessage(client, "C0ALLOWED", "1700000000.000100")

        asyncio.run(message.edit(content="완료"))

        self.assertEqual(
            client.updated,
            [{"channel": "C0ALLOWED", "ts": "1700000000.000100", "text": "완료"}],
        )


class IncomingMessageTests(unittest.TestCase):
    def test_reads_the_fields_authorization_and_routing_depend_on(self):
        msg = IncomingMessage(FakeSlackClient(), _event())

        self.assertEqual(msg.text, "안녕")
        self.assertEqual(msg.author_id, "U0OWNER")
        self.assertEqual(msg.channel.id, "C0ALLOWED")
        self.assertFalse(msg.is_bot)
        self.assertFalse(msg.is_dm)

    def test_a_direct_message_is_recognised_by_channel_type(self):
        self.assertTrue(IncomingMessage(FakeSlackClient(), _event(channel_type="im")).is_dm)

    def test_bot_messages_are_flagged_by_either_marker(self):
        self.assertTrue(IncomingMessage(FakeSlackClient(), _event(bot_id="B01")).is_bot)
        self.assertTrue(IncomingMessage(FakeSlackClient(), _event(subtype="bot_message")).is_bot)

    def test_a_top_level_message_starts_its_own_thread(self):
        msg = IncomingMessage(FakeSlackClient(), _event(ts="1700000000.000009"))
        self.assertEqual(msg.channel.thread_ts, "1700000000.000009")

    def test_a_message_already_in_a_thread_keeps_that_thread(self):
        # Otherwise a follow-up asked inside a thread would answer in the
        # channel root, away from the conversation it belongs to.
        msg = IncomingMessage(
            FakeSlackClient(),
            _event(ts="1700000000.000009", thread_ts="1700000000.000001"),
        )
        self.assertEqual(msg.channel.thread_ts, "1700000000.000001")

    def test_missing_fields_do_not_raise(self):
        # Slack sends message subtypes (edits, joins, tombstones) with fields
        # absent. Authorization rejects them; constructing one must not throw
        # before it gets the chance.
        msg = IncomingMessage(FakeSlackClient(), {"type": "message"})
        self.assertEqual(msg.text, "")
        self.assertEqual(msg.author_id, "")
        self.assertEqual(msg.channel.id, "")

    def test_reply_posts_through_the_channel(self):
        client = FakeSlackClient()
        msg = IncomingMessage(client, _event())

        sent = asyncio.run(msg.reply("세션을 초기화했습니다."))

        self.assertEqual(client.posted[0]["text"], "세션을 초기화했습니다.")
        self.assertEqual(client.posted[0]["thread_ts"], "1700000000.000001")
        self.assertIsInstance(sent, SentMessage)

    def test_reply_with_a_file_still_returns_the_editable_ack(self):
        client = FakeSlackClient()
        msg = IncomingMessage(client, _event())

        sent = asyncio.run(msg.reply("작업중입니다.", file=ChatFile("working_m.gif", data=b"gif")))

        self.assertIsInstance(sent, SentMessage)
        self.assertEqual(len(client.uploaded), 1)


if __name__ == "__main__":
    unittest.main()
