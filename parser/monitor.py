"""Сценарий сбора: обойти страницы, разобрать, сохранить.

Модуль намеренно не знает ничего про уведомления и отчёты —
за это отвечают notifier и report. Так сбор можно тестировать отдельно.
"""

from __future__ import annotations

import logging

from parser.config import SiteConfig
from parser.fetcher import Fetcher
from parser.models import Product, RunResult
from parser.scraper import find_next_page, parse_products
from parser.storage import Storage

logger = logging.getLogger(__name__)


def collect(config: SiteConfig, fetcher: Fetcher) -> tuple[list[Product], list[str]]:
    """Обходит страницы сайта и собирает товары.

    Возвращает (товары, список проблем). Падение на одной странице
    не отменяет уже собранные данные.
    """
    products: list[Product] = []
    errors: list[str] = []
    page_url = config.start_url
    visited: set[str] = set()

    for page_number in range(1, config.max_pages + 1):
        if page_url in visited:
            logger.warning("Ссылка на следующую страницу зациклилась: %s", page_url)
            break
        visited.add(page_url)

        html = fetcher.try_get_html(page_url)
        if html is None:
            errors.append(f"страница {page_number} не загрузилась: {page_url}")
            break

        page = parse_products(html, config=config, page_url=page_url)
        products.extend(page.products)
        errors.extend(page.skipped)

        logger.info(
            "Страница %d: товаров %d, пропущено %d",
            page_number,
            len(page.products),
            len(page.skipped),
        )

        next_url = find_next_page(html, config=config, page_url=page_url)
        if next_url is None:
            logger.info("Последняя страница: %d", page_number)
            break

        page_url = next_url

    return products, errors


def run_once(config: SiteConfig, storage: Storage, fetcher: Fetcher) -> RunResult:
    """Один цикл: собрать -> сохранить -> вернуть изменения."""
    products, errors = collect(config, fetcher)

    result = RunResult(site=config.key, collected=len(products), errors=errors)
    if products:
        result.new_products, result.changes = storage.save_run(products)

    return result
