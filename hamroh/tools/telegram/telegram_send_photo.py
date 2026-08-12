"""``telegram_send_photo`` — send an allowlisted image as a Telegram photo.

Accepts relative paths produced by Hamroh's render tools and absolute paths
returned by Codex ``image_gen``.  Each path is locked to its dedicated root
with the same hardening pattern as ``telegram_send_memory_document``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from telegram import InputFile

from ..base import BaseTool, OutboundDelivery, ToolResult, deliver_bookkeeping

if TYPE_CHECKING:
    from ...storage.generated_image_store import GeneratedImageStore
    from ...storage.render_store import RenderStore

log = logging.getLogger(__name__)

#: Telegram caption hard limit (photos use a smaller cap than documents).
_CAPTION_LIMIT = 1024


@dataclass(frozen=True)
class _ResolvedPhoto:
    """A Telegram upload value plus its safe display filename."""

    upload: Path | InputFile
    filename: str


class SendPhotoArgs(BaseModel):
    chat_id: int = Field(
        description=(
            "Numeric Telegram chat id (e.g. -1001234567890 for a group, a "
            "positive int for a DM). Not an @username."
        )
    )
    path: str = Field(
        description=(
            "Either a relative path returned by render_html/render_latex, or "
            "the absolute path under $CODEX_HOME/generated_images returned "
            "by image_gen. Other absolute paths, '..', and symlinks are rejected."
        ),
    )
    caption: str | None = Field(
        default=None,
        max_length=_CAPTION_LIMIT,
        description="Optional plain-text caption shown under the photo (max 1024 chars).",
    )
    reply_to_message_id: int | None = Field(
        default=None,
        description=(
            "Optional. Quote-reply the photo to this message id; omit for a "
            "standalone send."
        ),
    )


class TelegramSendPhotoTool(BaseTool[SendPhotoArgs]):
    name = "telegram_send_photo"
    description = (
        "Deliver an allowlisted image to a chat as an inline Telegram photo "
        "with preview. Pass either the relative path returned by render_html "
        "or render_latex, or the absolute $CODEX_HOME/generated_images path "
        "returned by image_gen. For an arbitrary file as a download use "
        "telegram_send_memory_document; for plain text use "
        "telegram_send_message. Paths are locked to the renders and generated-"
        "images roots; sends immediately."
    )
    args_model = SendPhotoArgs

    async def run(self, args: SendPhotoArgs) -> ToolResult:
        if self.ctx.bot is None:
            return ToolResult(content="bot not configured", is_error=True)
        resolved = await _resolve_photo(
            self.ctx.render_store,
            self.ctx.generated_image_store,
            args.path,
        )
        if isinstance(resolved, ToolResult):
            return resolved

        sent = await self.ctx.bot.send_photo(
            chat_id=args.chat_id,
            photo=resolved.upload,
            caption=args.caption,
            reply_to_message_id=args.reply_to_message_id,
        )
        message_id = sent.message_id
        log.info(
            "hot-path stage=delivered chat=%s msg=%s photo=%s",
            args.chat_id,
            message_id,
            args.path,
        )

        await deliver_bookkeeping(
            self.ctx,
            OutboundDelivery(
                chat_id=args.chat_id,
                message_id=message_id,
                reply_to_id=args.reply_to_message_id,
                transcript_text=_transcript_text(args.path, args.caption),
            ),
        )
        return _build_result(args, message_id, resolved.filename)


def _build_result(args: SendPhotoArgs, message_id: int, filename: str) -> ToolResult:
    """Assemble the success result for a delivered photo."""
    return ToolResult(
        content=f"sent photo message_id={message_id} ({filename})",
        data={
            "message_id": message_id,
            "chat_id": args.chat_id,
            "filename": filename,
            "path": args.path,
        },
    )


async def _resolve_photo(
    render_store: RenderStore | None,
    generated_image_store: GeneratedImageStore | None,
    path: str,
) -> _ResolvedPhoto | ToolResult:
    """Resolve a render-relative or image-gen absolute path.

    Returns a ready-to-upload value on success, or an error ``ToolResult``
    when the path is rejected by a store's safety checks or is missing.
    """
    is_generated = Path(path).is_absolute()
    if is_generated and generated_image_store is None:
        return ToolResult(content="generated image store unavailable", is_error=True)
    if not is_generated and render_store is None:
        return ToolResult(content="render store unavailable", is_error=True)

    try:
        if is_generated:
            assert generated_image_store is not None
            generated = await asyncio.to_thread(generated_image_store.read, path)
            return _ResolvedPhoto(
                upload=InputFile(generated.content, filename=generated.filename),
                filename=generated.filename,
            )
        else:
            assert render_store is not None
            resolved = await asyncio.to_thread(render_store.resolve_path, path)
    except Exception as exc:
        return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
    if not resolved.exists() or not resolved.is_file():
        return ToolResult(content=f"photo not found: {path}", is_error=True)
    return _ResolvedPhoto(upload=resolved, filename=resolved.name)


def _transcript_text(path: str, caption: str | None) -> str:
    """Render the transcript line for a delivered photo, with optional caption."""
    if caption:
        return f"[photo] {path} — {caption}"
    return f"[photo] {path}"
