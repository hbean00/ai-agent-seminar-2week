"""Slack transport objects shaped like the discord.py ones they replaced.

The bot used to pass ``discord.Message`` / ``discord.abc.Messageable`` around
and call ``.reply()``, ``.send()`` and ``.edit()`` on them. Those three verbs
are the whole of what ``main.py``, ``status.py`` and ``outputs.py`` ever asked
of the Discord library -- everything else in this repository (the CLI runner,
the warm pool, job directories, sessions, timings) never imported it at all.

So the port keeps the verbs and swaps what is underneath. ``outputs.py`` still
writes ``await channel.send(text, files=[...])``; it just reaches
``chat.postMessage`` / ``files.uploadV2`` instead of a Discord route. This is
one implementation, not an abstraction layer with two backends -- Discord is
gone -- but keeping the shape is what let the existing tests, which already
fake these objects by duck typing, survive the move.

Everything a job produces lands in the *thread* of the message that asked for
it. Discord replies stay in the channel and read fine interleaved; Slack has
real threads, and without them a 10-minute job's ack, progress edits and
attachments would be scattered through the channel between other people's
messages.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ChatFile:
    """An upload, replacing ``discord.File``.

    Exactly one of ``path`` / ``data`` is set: job artifacts come off disk,
    while the ``response.md`` fallback for an over-long body is built in
    memory.
    """

    filename: str
    path: Path | None = None
    data: bytes | None = None

    def read_bytes(self) -> bytes:
        if self.data is not None:
            return self.data
        if self.path is None:
            raise ValueError(f"ChatFile {self.filename!r} has neither path nor data")
        return self.path.read_bytes()


class SentMessage:
    """A message this bot posted, which it may later edit.

    Discord identifies a message by id; Slack identifies it by the channel
    plus its ``ts``. Both are needed for every edit, so both are kept here.
    """

    def __init__(self, client: Any, channel_id: str, ts: str) -> None:
        self._client = client
        self.channel_id = channel_id
        self.ts = ts

    async def edit(self, content: str) -> None:
        # Keyword-compatible with the discord.py call it replaces
        # (``ack.edit(content=...)``), so the call sites did not have to move.
        await self._client.chat_update(channel=self.channel_id, ts=self.ts, text=content)


class ChatChannel:
    """Somewhere to post, plus the thread everything about one job belongs to."""

    def __init__(self, client: Any, channel_id: str, thread_ts: str | None = None) -> None:
        self._client = client
        self.id = channel_id
        self.thread_ts = thread_ts

    async def send(
        self,
        content: str | None = None,
        *,
        files: list[ChatFile] | None = None,
        file: ChatFile | None = None,
    ) -> SentMessage | None:
        """Post text, files, or both. Returns the text message, when there is one.

        Text and uploads go as two separate calls even when both are given,
        because the text is the half a caller may still need to edit: the ack
        that carries the working GIF is edited for the rest of the job's life,
        and files.uploadV2 hands back a file rather than an editable message.
        Passing the text as ``initial_comment`` instead would post it inside
        the upload and strand every later edit.
        """
        uploads = list(files or [])
        if file is not None:
            uploads.append(file)

        sent: SentMessage | None = None
        if content is not None:
            response = await self._client.chat_postMessage(
                channel=self.id,
                text=content,
                thread_ts=self.thread_ts,
            )
            sent = SentMessage(self._client, self.id, response["ts"])

        if uploads:
            await self._upload(uploads)

        return sent

    async def _upload(self, uploads: list[ChatFile]) -> None:
        # Reading off disk is blocking I/O, and this runs on the event loop
        # that also carries the Socket Mode websocket -- the same reason
        # discord.File construction was pushed off the loop here before.
        file_uploads = [
            {"filename": upload.filename, "content": await asyncio.to_thread(upload.read_bytes)}
            for upload in uploads
        ]
        await self._client.files_upload_v2(
            channel=self.id,
            file_uploads=file_uploads,
            thread_ts=self.thread_ts,
        )


class IncomingMessage:
    """One inbound Slack message event, shaped like ``discord.Message``.

    ``is_dm`` reads the event's ``channel_type``: Slack says ``im`` for a
    direct message, where discord.py handed over a ``DMChannel`` instance.
    """

    def __init__(self, client: Any, event: dict) -> None:
        self._client = client
        self.text: str = str(event.get("text") or "")
        self.author_id: str = str(event.get("user") or "")
        # A message from any app -- including this one -- carries bot_id. Both
        # keys are checked because Slack marks message_changed/bot_message
        # subtypes with only one of them.
        self.is_bot: bool = bool(event.get("bot_id")) or event.get("subtype") == "bot_message"
        self.is_dm: bool = event.get("channel_type") == "im"
        self.ts: str = str(event.get("ts") or "")
        # A message already inside a thread keeps that thread; a top-level one
        # starts its own, so a job never posts into the channel root.
        thread_ts = str(event.get("thread_ts") or self.ts)
        self.channel = ChatChannel(client, str(event.get("channel") or ""), thread_ts)

    async def reply(self, content: str, *, file: ChatFile | None = None) -> SentMessage | None:
        return await self.channel.send(content, file=file)


class ScheduledMessage:
    """Stands in for the user message a scheduled job never had.

    ``_dispatch_job`` asks a message for two things -- ``.channel`` and
    ``.reply()`` -- so a schedule only needs to supply those. It carries no
    author and is never passed to ``is_authorized``: a schedule was authorised
    once, when the owner registered it.

    The channel it hands over already has ``thread_ts`` set to the header the
    scheduler posted, so the ack, the progress edits and the attachments all
    land under one "예약 작업" root instead of three loose channel messages.
    """

    def __init__(self, channel: ChatChannel, prompt: str) -> None:
        self.channel = channel
        self.text = prompt

    async def reply(self, content: str, *, file: ChatFile | None = None) -> SentMessage | None:
        return await self.channel.send(content, file=file)
