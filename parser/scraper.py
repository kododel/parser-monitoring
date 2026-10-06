"""Разбор HTML в товары по селекторам из конфига.

Вся привязка к конкретному сайту живёт в config/sites.json. Здесь —
только общая логика: найти карточки, вытащить поля, собрать ссылку.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from parser.config import SiteConfig
from parser.models import Product
from parser.prices import parse_price

logger = logging.getLogger(__name__)


@dataclass
class ParseResult:
    """Что удалось достать со страницы и что не удалось."""

    products: list[Product] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def extract_external_id(url: str) -> str:
    """Стабильный идентификатор товара из ссылки.

    Идентификатор не должен меняться между запусками, поэтому берём путь
    без домена и без параметров: `catalogue/kniga_1000/index.html`.
    """
    path = urlparse(url).path.strip("/")
    return path or url


def _extract_rating(item: Tag, selector: str | None) -> str | None:
    """Рейтинг обычно спрятан в классах: `<p class="star-rating Three">`."""
    if not selector:
        return None

    node = item.select_one(selector)
    if node is None:
        return None

    classes = [str(c) for c in (node.get("class") or [])]
    if len(classes) > 1:
        return classes[-1]

    text = node.get_text(strip=True)
    return text or None


def parse_products(html: str, *, config: SiteConfig, page_url: str) -> ParseResult:
    """Разбирает страницу со списком товаров."""
    soup = BeautifulSoup(html, "lxml")
    result = ParseResult()

    items = soup.select(config.selectors.item)
    if not items:
        result.skipped.append(
            f"на странице {page_url} не найдено ни одной карточки "
            f"по селектору «{config.selectors.item}» — возможно, сайт изменил вёрстку"
        )
        return result

    for item in items:
        link_node = item.select_one(config.selectors.link)
        if link_node is None or not link_node.get("href"):
            result.skipped.append("карточка без ссылки на товар")
            continue

        url = urljoin(page_url, str(link_node.get("href")))

        # Заголовок: либо из атрибута (полный текст), либо из содержимого тега
        title = ""
        if config.selectors.title_attr:
            title = str(link_node.get(config.selectors.title_attr) or "").strip()
        if not title:
            title = link_node.get_text(strip=True)

        price_node = item.select_one(config.selectors.price)
        parsed_price = parse_price(price_node.get_text(strip=True) if price_node else None)
        if parsed_price is None:
            result.skipped.append(f"не удалось разобрать цену у «{title or url}»")
            continue

        price, currency = parsed_price

        in_stock = True
        if config.selectors.availability:
            availability_node = item.select_one(config.selectors.availability)
            # Если блока наличия на карточке нет, считаем товар доступным.
            # Пометить «нет в наличии» весь каталог из-за изменившейся вёрстки
            # хуже, чем показать лишний товар: заказчик увидит ложную тревогу
            # и перестанет доверять мониторингу.
            if availability_node is not None:
                availability_text = availability_node.get_text(" ", strip=True).lower()
                in_stock = config.in_stock_text.lower() in availability_text

        result.products.append(
            Product(
                site=config.key,
                external_id=extract_external_id(url),
                title=title or "Без названия",
                url=url,
                price=price,
                currency=currency,
                in_stock=in_stock,
                rating=_extract_rating(item, config.selectors.rating),
            )
        )

    if result.skipped:
        logger.warning(
            "На странице %s пропущено карточек: %d", page_url, len(result.skipped)
        )

    return result


def find_next_page(html: str, *, config: SiteConfig, page_url: str) -> str | None:
    """Ссылка на следующую страницу списка, если она есть."""
    if not config.selectors.next_page:
        return None

    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one(config.selectors.next_page)
    if node is None or not node.get("href"):
        return None

    href = str(node.get("href"))
    # Некоторые сайты ставят заглушку вместо ссылки
    if href.startswith("#") or href.lower().startswith("javascript:"):
        return None

    return urljoin(page_url, href)
