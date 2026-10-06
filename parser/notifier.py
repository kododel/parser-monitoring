"""Уведомления в Telegram.

Используется голый Bot API через requests: для отправки сообщений
фреймворк не нужен, а зависимостей меньше.
"""

from __future__ import annotations

import logging

import requests

from parser.models import RunResult
from parser.prices import format_price

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Ограничение Telegram — 4096 символов на сообщение
TELEGRAM_LIMIT = 4096

# Сколько изменений показываем в сообщении, прежде чем свернуть в «и ещё N»
MAX_CHANGES_IN_MESSAGE = 10


def send_message(token: str, chat_id: int, text: str, *, timeout: int = 15) -> bool:
    """Отправляет сообщение. False — если Telegram отказал."""
    if len(text) > TELEGRAM_LIMIT:
        text = text[: TELEGRAM_LIMIT - 20] + "\n…сообщение обрезано"

    try:
        response = requests.post(
            API_URL.format(token=token),
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=timeout,
        )
    except requests.RequestException as error:
        logger.error("Telegram недоступен: %s", error)
        return False

    if response.status_code != 200:
        # Частая причина — бота не запускали или chat_id указан неверно
        logger.error(
            "Telegram отклонил сообщение для %s: %s %s",
            chat_id,
            response.status_code,
            response.text[:300],
        )
        return False

    return True


def build_report(result: RunResult, site_title: str) -> str:
    """Текст сообщения об итогах прогона."""
    lines = [f"📊 {site_title}", ""]
    lines.append(f"Собрано товаров: {result.collected}")
    if result.new_products:
        lines.append(f"🆕 Впервые увидели: {result.new_products}")

    if result.changes:
        lines.append("")
        lines.append(f"Изменения цен ({len(result.changes)}):")
        for change in result.changes[:MAX_CHANGES_IN_MESSAGE]:
            lines.append(f"{change.arrow} {change.product.short_title}")
            lines.append(
                f"    {format_price(change.old_price, change.product.currency)} → "
                f"{format_price(change.new_price, change.product.currency)} "
                f"({change.percent:+.1f}%)"
            )
        hidden = len(result.changes) - MAX_CHANGES_IN_MESSAGE
        if hidden > 0:
            lines.append(f"…и ещё {hidden}")
    else:
        lines.append("")
        lines.append("Цены не изменились.")

    if result.errors:
        lines.append("")
        lines.append(f"⚠️ Проблем при сборе: {len(result.errors)}")
        for error in result.errors[:3]:
            lines.append(f"• {error[:120]}")
        if len(result.errors) > 3:
            lines.append(f"• …и ещё {len(result.errors) - 3}")

    return "\n".join(lines)


def notify(
    *,
    token: str,
    chat_ids: list[int],
    result: RunResult,
    site_title: str,
    only_when_changed: bool = True,
) -> int:
    """Рассылает отчёт. Возвращает число успешно доставленных сообщений.

    При only_when_changed=True молчит, если ничего не изменилось и ошибок нет:
    иначе бот быстро надоедает, и его отключают вместе с полезными уведомлениями.
    """
    if only_when_changed and not result.has_news and not result.errors:
        logger.info("Изменений нет — уведомление не отправляем.")
        return 0

    text = build_report(result, site_title)
    delivered = 0
    for chat_id in chat_ids:
        if send_message(token, chat_id, text):
            delivered += 1

    return delivered
