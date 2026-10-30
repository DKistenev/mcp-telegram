# ruff: noqa: CPY001, RUF001
from __future__ import annotations

import json
import logging
import sys
import typing as t
from datetime import datetime
from functools import singledispatch

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


class ListDialogs(ToolArgs):
    """
    List dialogs (private chats, groups, channels), most recent first.

    Returns one JSON object per line: id, name, type (user, bot, group, supergroup, channel),
    username, unread, mentions. Use `query` to find a dialog by name or @username instead of
    listing everything. If more results are available, the last line is `{"next_offset": N}`.
    """

    query: str | None = Field(None, description="Case-insensitive substring of the dialog name or @username")
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
                        "type": dialog_type(dialog),
                        "username": username,
                        "unread": dialog.unread_count,
                        "mentions": dialog.unread_mentions_count,
                    }
                )
            )

    return [TextContent(type="text", text="\n".join(lines) or "No dialogs found")]


### ListMessages ###


class ListMessages(ToolArgs):
    """
    List messages in a dialog, chat or channel, newest first.

    Returns one JSON object per line: id, date (local time), from (`Я` for the account owner),
    out, reply_to (id of the replied message), forwarded_from, media (e.g. `фото`,
    `файл: report.pdf, 2 МБ`) and text. Fields that are empty are omitted.

    To page back through history, pass the id from the last line `{"next_offset_id": N}` as
    `offset_id`, or use `offset_date` to start from a given date. If `unread` is set, only unread
    messages are listed, up to `limit`.
    """

    dialog_id: int
    unread: bool = False
    limit: int = Field(100, description="Maximum number of messages to return")
    offset_id: int | None = Field(None, description="Only return messages older than this message id")
    offset_date: str | None = Field(None, description="Only return messages older than this date, ISO 8601")
    search: str | None = Field(None, description="Only return messages containing this text")


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

        iter_messages_args: dict[str, t.Any] = {
            "entity": args.dialog_id,
            "reverse": False,
            "limit": min(dialog.unread_count, args.limit) if args.unread else args.limit,
            "offset_id": args.offset_id or 0,
            "offset_date": datetime.fromisoformat(args.offset_date) if args.offset_date else None,
            "search": args.search,
        }

        logger.debug("iter_messages_args: %s", iter_messages_args)
        last_id = None
        count = 0
        async for message in client.iter_messages(**iter_messages_args):
            count += 1
            last_id = message.id
            if not isinstance(message, custom.Message) or message.action:
                continue
            record = message_record(message)
            if "media" in record or "text" in record:
                lines.append(json_line(record))
        if last_id is not None and count == iter_messages_args["limit"]:
            lines.append(json_line({"next_offset_id": last_id}))

    return [TextContent(type="text", text="\n".join(lines) or "No messages found")]


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
