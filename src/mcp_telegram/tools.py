# ruff: noqa: CPY001, RUF001
from __future__ import annotations

import logging
import sys
import typing as t
from functools import singledispatch

from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ConfigDict
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
    """List available dialogs, chats and channels."""

    unread: bool = False
    archived: bool = False
    ignore_pinned: bool = False


@tool_runner.register
async def list_dialogs(
    args: ListDialogs,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[ListDialogs] args[%s]", args)

    response: list[TextContent] = []
    async with create_client() as client:
        dialog: custom.dialog.Dialog
        async for dialog in client.iter_dialogs(archived=args.archived, ignore_pinned=args.ignore_pinned):
            if args.unread and dialog.unread_count == 0:
                continue
            msg = (
                f"name='{dialog.name}' id={dialog.id} "
                f"unread={dialog.unread_count} mentions={dialog.unread_mentions_count}"
            )
            response.append(TextContent(type="text", text=msg))

    return response


### ListMessages ###


class ListMessages(ToolArgs):
    """
    List messages in a given dialog, chat or channel. The messages are listed in order from newest to oldest.

    If `unread` is set to `True`, only unread messages will be listed. Once a message is read, it will not be
    listed again.

    If `limit` is set, only the last `limit` messages will be listed. If `unread` is set, the limit will be
    the minimum between the unread messages and the limit.

    Each message is returned as `[YYYY-MM-DD HH:MM] <author>: <text>`, where the author is `Я` for
    messages sent by the account owner. Media is marked in brackets, e.g. `[фото]` or
    `[файл: report.pdf, 2 МБ]`.
    """

    dialog_id: int
    unread: bool = False
    limit: int = 100


@tool_runner.register
async def list_messages(
    args: ListMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[ListMessages] args[%s]", args)

    response: list[TextContent] = []
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
        }
        if args.unread:
            iter_messages_args["limit"] = min(dialog.unread_count, args.limit)
        else:
            iter_messages_args["limit"] = args.limit

        logger.debug("iter_messages_args: %s", iter_messages_args)
        async for message in client.iter_messages(**iter_messages_args):
            logger.debug("message: %s", type(message))
            if not isinstance(message, custom.Message) or message.action:
                continue
            body = " ".join(part for part in (media_label(message), message.text) if part)
            if body:
                response.append(TextContent(type="text", text=f"{message_header(message)}: {body}"))

    return response


def display_name(entity: t.Any) -> str:  # noqa: ANN401
    if entity is None:
        return ""
    name = utils.get_display_name(entity)
    username = getattr(entity, "username", None)
    return f"{name} (@{username})" if username else name


def message_header(message: custom.Message) -> str:
    date = message.date.astimezone().strftime("%Y-%m-%d %H:%M")
    is_me = message.out or getattr(message.sender, "is_self", False)
    author = "Я" if is_me else (display_name(message.sender) or message.post_author or "?")
    header = f"[{date}] {author}"
    if message.forward:
        origin = message.forward.from_name or display_name(message.forward.sender or message.forward.chat)
        header += f" (переслано от {origin or '?'})"
    return header


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
