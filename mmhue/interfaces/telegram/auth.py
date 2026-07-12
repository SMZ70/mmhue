"""Telegram user allow-list guard."""

from __future__ import annotations

from functools import wraps
from typing import Callable, Any

from telegram import Update
from loguru import logger

from mmhue.config import settings

Handler = Callable[..., Any]


def restricted(handler: Handler) -> Handler:
    """Decorator that works on both plain functions and instance methods.

    Finds the Update object by type so it doesn't matter whether self is
    the first argument or not.
    """

    @wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        update: Update | None = next(
            (a for a in args if isinstance(a, Update)), kwargs.get("update")
        )
        if update is None:
            return
        user = update.effective_user
        if not user:
            return
        if settings.telegram_allowed_user_ids and user.id not in settings.telegram_allowed_user_ids:
            logger.warning("Unauthorized access attempt from user {}", user.id)
            if update.message:
                await update.message.reply_text("Not authorized.")
            return
        return await handler(*args, **kwargs)

    return wrapper
