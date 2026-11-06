# ruff: noqa: CPY001, RUF001
from __future__ import annotations

import json
import logging
import os
import sys
import typing as t
from datetime import datetime
from functools import singledispatch
from pathlib import Path

from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ConfigDict, Field
from telethon import TelegramClient, custom, functions, types, utils  # type: ignore[import-untyped]

from .telegram import create_client

logger = logging.getLogger(__name__)


# How to add a new tool:
#
# 1. Create a new class that inherits from ToolArgs
#    ```python
#    class NewTool(ToolArgs):
#        """Description of the new tool."""
#        pass
#    ```
#    Attributes of the class will be used as arguments for the tool.
#    The class docstring will be used as the tool description.
#
# 2. Implement the tool_runner function for the new class
#    ```python
#    @tool_runner.register
#    async def new_tool(args: NewTool) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
#        pass
#    ```
#    The function should return a sequence of TextContent, ImageContent or EmbeddedResource.
#    The function should be async and accept a single argument of the new class.
#
# 3. Done! Restart the client and the new tool should be available.


class ToolArgs(BaseModel):
    model_config = ConfigDict()


@singledispatch
async def tool_runner(
    args,  # noqa: ANN001
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    raise NotImplementedError(f"Unsupported type: {type(args)}")


def tool_description(args: type[ToolArgs]) -> Tool:
    return Tool(
        name=args.__name__,
        description=args.__doc__,
        inputSchema=args.model_json_schema(),
    )


def tool_args(tool: Tool, *args, **kwargs) -> ToolArgs:  # noqa: ANN002, ANN003
    return sys.modules[__name__].__dict__[tool.name](*args, **kwargs)


### ListDialogs ###


DialogType = t.Literal["user", "bot", "group", "supergroup", "channel"]


class ListDialogs(ToolArgs):
    """
    List dialogs (private chats, groups, channels), most recent first.

    Returns one JSON object per line: id, name, type (user, bot, group, supergroup, channel),
    username, unread, mentions. Use `query` to find a dialog by name or @username instead of
    listing everything, and `types` to keep only some kinds, e.g. `["user"]` for private chats.
    If more results are available, the last line is `{"next_offset": N}`.
    """

    query: str | None = Field(None, description="Case-insensitive substring of the dialog name or @username")
    types: list[DialogType] | None = Field(None, description='Only return dialogs of these types, e.g. ["user"]')
    unread: bool = False
    archived: bool = False
    ignore_pinned: bool = False
    limit: int = Field(50, description="Maximum number of dialogs to return")
    offset: int = Field(0, description="Number of matching dialogs to skip, for paging")


def dialog_type(dialog: custom.dialog.Dialog) -> str:
    if dialog.is_user:
        return "bot" if getattr(dialog.entity, "bot", False) else "user"
    if dialog.is_channel:
        return "supergroup" if getattr(dialog.entity, "megagroup", False) else "channel"
    return "group"


@tool_runner.register
async def list_dialogs(
    args: ListDialogs,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[ListDialogs] args[%s]", args)

    query = (args.query or "").lstrip("@").casefold()
    lines: list[str] = []
    matched = 0
    async with create_client() as client:
        dialog: custom.dialog.Dialog
        async for dialog in client.iter_dialogs(archived=args.archived, ignore_pinned=args.ignore_pinned):
            if args.unread and dialog.unread_count == 0:
                continue
            kind = dialog_type(dialog)
            if args.types and kind not in args.types:
                continue
            username = getattr(dialog.entity, "username", None)
            if query and query not in f"{dialog.name}\n{username or ''}".casefold():
                continue
            matched += 1
            if matched <= args.offset:
                continue
            if len(lines) == args.limit:
                lines.append(json_line({"next_offset": args.offset + args.limit}))
                break
            lines.append(
                json_line(
                    {
                        "id": dialog.id,
                        "name": dialog.name,
                        "type": kind,
                        "username": username,
                        "unread": dialog.unread_count,
                        "mentions": dialog.unread_mentions_count,
                    }
                )
            )

    return [TextContent(type="text", text="\n".join(lines) or json_line({"count": 0}))]


### ListMessages ###


class ListMessages(ToolArgs):
    """
    List messages in a dialog, chat or channel, newest first.

    Returns one JSON object per line: id, date (local time), from (`Я` for the account owner),
    out, reply_to (id of the replied message), forwarded_from, media (e.g. `фото`,
    `файл: report.pdf, 2 МБ`) and text. Fields that are empty are omitted.
    Use GetMessages to fetch a `reply_to` target that is not in the listed page.

    To page back through history, pass the id from the last line `{"next_offset_id": N}` as
    `offset_id`. `from_date` and `to_date` limit the listing to a period. To read the conversation
    around a message (e.g. a search hit), pass its id as `around_id`: the message itself and up to
    `context` messages before and after it are returned. If `unread` is set, only unread messages
    are listed, up to `limit`. An empty result is `{"count": 0}`.
    """

    dialog_id: int
    unread: bool = False
    limit: int = Field(100, description="Maximum number of messages to return")
    offset_id: int | None = Field(None, description="Only return messages older than this message id")
    from_date: str | None = Field(None, description="Only return messages sent at or after this date, ISO 8601")
    to_date: str | None = Field(None, description="Only return messages sent before this date, ISO 8601")
    search: str | None = Field(None, description="Only return messages containing this text")
    around_id: int | None = Field(None, description="Return the messages around this message id instead of a page")
    context: int = Field(5, description="With around_id, how many messages to return on each side")


@tool_runner.register
async def list_messages(
    args: ListMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[ListMessages] args[%s]", args)

    lines: list[str] = []
    async with create_client() as client:
        result = await client(functions.messages.GetPeerDialogsRequest(peers=[args.dialog_id]))
        if not result:
            raise ValueError(f"Channel not found: {args.dialog_id}")

        if not isinstance(result, types.messages.PeerDialogs):
            raise TypeError(f"Unexpected result: {type(result)}")

        dialog = result.dialogs[-1]

        if args.around_id is not None:
            lines = [json_line(message_record(m)) for m in await messages_around(client, args)]
            return [TextContent(type="text", text="\n".join(lines) or json_line({"count": 0}))]

        from_date = parse_date(args.from_date)
        iter_messages_args: dict[str, t.Any] = {
            "entity": args.dialog_id,
            "reverse": False,
            "limit": min(dialog.unread_count, args.limit) if args.unread else args.limit,
            "offset_id": args.offset_id or 0,
            "offset_date": parse_date(args.to_date),
            "search": args.search,
        }

        logger.debug("iter_messages_args: %s", iter_messages_args)
        last_id = None
        count = 0
        async for message in client.iter_messages(**iter_messages_args):
            if from_date and message.date < from_date:
                break
            count += 1
            last_id = message.id
            if not isinstance(message, custom.Message) or message.action:
                continue
            record = message_record(message)
            if "media" in record or "text" in record:
                lines.append(json_line(record))
        if last_id is not None and count == iter_messages_args["limit"]:
            lines.append(json_line({"next_offset_id": last_id}))

    return [TextContent(type="text", text="\n".join(lines) or json_line({"count": 0}))]


async def messages_around(client: TelegramClient, args: ListMessages) -> list[custom.Message]:
    # Message ids of private chats are shared across all of them, so neighbours
    # cannot be computed as around_id ± N and have to be fetched from the dialog.
    newer = [
        m async for m in client.iter_messages(args.dialog_id, min_id=args.around_id, limit=args.context, reverse=True)
    ]
    target = await client.get_messages(args.dialog_id, ids=args.around_id)
    older = [m async for m in client.iter_messages(args.dialog_id, offset_id=args.around_id, limit=args.context)]
    return [m for m in [*reversed(newer), target, *older] if isinstance(m, custom.Message) and not m.action]


### SearchMessages ###


class SearchMessages(ToolArgs):
    """
    Search messages by text across all dialogs at once, newest first.

    Returns one JSON object per line in the ListMessages format plus `chat_id` and `chat`
    (the dialog name). Read the conversation around a hit with ListMessages and `around_id`.
    If the last line is `{"more": true}`, there are more hits: narrow the query or the dates.
    An empty result is `{"count": 0}`.
    """

    query: str = Field(description="Text to search for")
    limit: int = Field(50, description="Maximum number of messages to return")
    from_date: str | None = Field(None, description="Only return messages sent at or after this date, ISO 8601")
    to_date: str | None = Field(None, description="Only return messages sent before this date, ISO 8601")


@tool_runner.register
async def search_messages(
    args: SearchMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[SearchMessages] args[%s]", args)

    from_date = parse_date(args.from_date)
    lines: list[str] = []
    count = 0
    async with create_client() as client:
        # entity=None makes Telethon use messages.searchGlobal
        async for message in client.iter_messages(
            None, search=args.query, limit=args.limit, offset_date=parse_date(args.to_date)
        ):
            if from_date and message.date < from_date:
                break
            count += 1
            if not isinstance(message, custom.Message) or message.action:
                continue
            record = {"chat_id": message.chat_id, "chat": display_name(message.chat), **message_record(message)}
            lines.append(json_line(record))
    if count == args.limit:
        lines.append(json_line({"more": True}))

    return [TextContent(type="text", text="\n".join(lines) or json_line({"count": 0}))]


### DownloadMedia ###


class DownloadMedia(ToolArgs):
    """
    Download the file, photo or other media of a message to the local download folder.

    Returns `{"path", "name", "size"}`; open the file from `path`. Files larger than
    `max_size_mb` are refused, raise it explicitly for a big file.
    """

    dialog_id: int
    message_id: int
    max_size_mb: int = Field(50, description="Refuse files larger than this many megabytes")


def download_dir() -> Path:
    path = Path(os.environ.get("TELEGRAM_DOWNLOAD_DIR", Path.home() / "Claude" / "Telegram")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_file_name(name: str | None) -> str | None:
    # The name comes from the sender, so keep only a plain file name inside the download folder.
    name = Path((name or "").replace("\\", "/")).name.lstrip(".").strip()
    return name or None


@tool_runner.register
async def download_media(
    args: DownloadMedia,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[DownloadMedia] args[%s]", args)

    async with create_client() as client:
        message = await client.get_messages(args.dialog_id, ids=args.message_id)
        if not isinstance(message, custom.Message) or not message.media or not message.file:
            raise ValueError(f"Message {args.message_id} has no downloadable media")
        if isinstance(message.media, types.MessageMediaWebPage):
            raise ValueError(f"Message {args.message_id} only has a link preview")

        size = message.file.size or 0
        if size > args.max_size_mb * 1024 * 1024:
            raise ValueError(f"File is {human_size(size)}, larger than max_size_mb={args.max_size_mb}")

        name = safe_file_name(message.file.name)
        prefix = f"{abs(args.dialog_id)}_{args.message_id}"
        # Without a name Telethon appends the right extension to the path itself.
        target = download_dir() / (f"{prefix}_{name}" if name else prefix)
        path = await client.download_media(message, file=str(target))

    return [TextContent(type="text", text=json_line({"path": path, "name": name, "size": human_size(size)}))]


### GetMessages ###


class GetMessages(ToolArgs):
    """
    Get specific messages of a dialog by id, e.g. the message a `reply_to` points at.

    Returns one JSON object per line in the same format as ListMessages. An id that does not
    exist or was deleted is returned as `{"id": N, "missing": true}`.
    """

    dialog_id: int
    ids: list[int] = Field(description="Message ids to fetch, at most 100")


@tool_runner.register
async def get_messages(
    args: GetMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[GetMessages] args[%s]", args)

    if len(args.ids) > 100:  # noqa: PLR2004
        raise ValueError("At most 100 ids per call")

    lines: list[str] = []
    async with create_client() as client:
        found = {
            message.id: message
            for message in await client.get_messages(args.dialog_id, ids=args.ids)
            if isinstance(message, custom.Message) and not message.action
        }
        for message_id in args.ids:
            message = found.get(message_id)
            lines.append(json_line(message_record(message) if message else {"id": message_id, "missing": True}))

    return [TextContent(type="text", text="\n".join(lines))]


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    date = datetime.fromisoformat(value)
    return date if date.tzinfo else date.astimezone()  # naive dates are local time


def json_line(record: dict[str, t.Any]) -> str:
    return json.dumps({k: v for k, v in record.items() if v is not None and v != ""}, ensure_ascii=False)


def display_name(entity: t.Any) -> str:  # noqa: ANN401
    if entity is None:
        return ""
    name = utils.get_display_name(entity)
    username = getattr(entity, "username", None)
    return f"{name} (@{username})" if username else name


def message_record(message: custom.Message) -> dict[str, t.Any]:
    is_me = message.out or getattr(message.sender, "is_self", False)
    forwarded_from = None
    if message.forward:
        forward = message.forward
        forwarded_from = forward.from_name or display_name(forward.sender or forward.chat) or "?"
    label = media_label(message)
    return {
        "id": message.id,
        "date": message.date.astimezone().strftime("%Y-%m-%d %H:%M"),
        "from": "Я" if is_me else (display_name(message.sender) or message.post_author or "?"),
        "out": bool(is_me),
        "reply_to": message.reply_to_msg_id,
        "forwarded_from": forwarded_from,
        "media": label[1:-1] if label else None,
        "text": message.text,
    }


def human_size(size: int | None) -> str:
    if not size:
        return ""
    value = float(size)
    for unit in ("Б", "КБ", "МБ"):
        if value < 1024:  # noqa: PLR2004
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"


def media_label(message: custom.Message) -> str:  # noqa: C901, PLR0911
    if isinstance(message.media, types.MessageMediaWebPage):
        return ""  # link preview, the link itself is in the text
    file = message.file
    if message.photo:
        return "[фото]"
    if message.sticker:
        return f"[стикер {file.emoji}]" if file and file.emoji else "[стикер]"
    if message.voice:
        return f"[голосовое, {file.duration} с]" if file and file.duration else "[голосовое]"
    if message.video_note:
        return "[видеокружок]"
    if message.gif:
        return "[GIF]"
    if message.audio:
        track = " — ".join(part for part in (file.performer, file.title) if part) or file.name
        return f"[аудио: {track}]" if track else "[аудио]"
    if message.video:
        return f"[видео: {file.name}]" if file.name else "[видео]"
    if message.document:
        details = ", ".join(part for part in (file.name, human_size(file.size)) if part)
        return f"[файл: {details}]" if details else "[файл]"
    if message.contact:
        contact = message.contact
        return f"[контакт: {' '.join(p for p in (contact.first_name, contact.last_name) if p)}]"
    if message.geo:
        return "[геопозиция]"
    if message.poll:
        question = message.poll.poll.question
        return f"[опрос: {getattr(question, 'text', question)}]"
    if message.media:
        return "[медиа]"
    return ""
