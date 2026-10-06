"""Настройки: секреты из .env, селекторы из config/sites.json.

Селекторы вынесены в отдельный файл, чтобы под новый сайт не приходилось
трогать код: заказчик или я меняем только JSON.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; PriceMonitorBot/1.0; +https://example.com/bot)"
)


@dataclass(frozen=True)
class Selectors:
    """CSS-селекторы для разбора страницы со списком товаров."""

    item: str
    link: str
    price: str
    title_attr: str | None = None
    availability: str | None = None
    rating: str | None = None
    next_page: str | None = None


@dataclass(frozen=True)
class SiteConfig:
    """Описание одного сайта."""

    key: str
    title: str
    base_url: str
    start_url: str
    selectors: Selectors
    max_pages: int = 1
    request_delay: float = 1.0
    respect_robots: bool = True
    in_stock_text: str = "in stock"

    def absolute(self, url: str) -> str:
        """Относительную ссылку превращает в абсолютную."""
        return urljoin(self.base_url, url)

    @property
    def domain(self) -> str:
        return self.base_url.split("//", 1)[-1].split("/", 1)[0]


@dataclass(frozen=True)
class Settings:
    """Общие настройки приложения."""

    db_path: Path
    export_dir: Path
    sites_file: Path
    telegram_token: str = ""
    telegram_chat_ids: list[int] = field(default_factory=list)
    timeout: int = 20
    user_agent: str = DEFAULT_USER_AGENT

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_ids)

    def problems(self) -> list[str]:
        issues: list[str] = []
        if not self.telegram_token:
            issues.append("Не задан TELEGRAM_TOKEN — уведомления отправляться не будут.")
        if not self.telegram_chat_ids:
            issues.append("Не задан TELEGRAM_CHAT_IDS — уведомления отправляться не будут.")
        return issues


def _parse_chat_ids(raw: str) -> list[int]:
    """'123,-456' -> [123, -456]. Мусор пропускаем, а не падаем."""
    ids: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            continue
    return ids


def load_settings() -> Settings:
    """Собирает общие настройки из окружения."""
    return Settings(
        db_path=Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "prices.db"))),
        export_dir=Path(os.getenv("EXPORT_DIR", str(BASE_DIR / "data" / "exports"))),
        sites_file=Path(os.getenv("SITES_FILE", str(BASE_DIR / "config" / "sites.json"))),
        telegram_token=os.getenv("TELEGRAM_TOKEN", "").strip(),
        telegram_chat_ids=_parse_chat_ids(os.getenv("TELEGRAM_CHAT_IDS", "")),
        timeout=int(os.getenv("REQUEST_TIMEOUT", "20")),
        user_agent=os.getenv("USER_AGENT", DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT,
    )


def _selectors_from_dict(raw: dict) -> Selectors:
    return Selectors(
        item=raw["item"],
        link=raw["link"],
        price=raw["price"],
        title_attr=raw.get("title_attr"),
        availability=raw.get("availability"),
        rating=raw.get("rating"),
        next_page=raw.get("next_page"),
    )


def load_sites(path: Path) -> dict[str, SiteConfig]:
    """Читает config/sites.json в объекты SiteConfig."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Не найден файл с описанием сайтов: {path}. "
            "Скопируйте config/sites.example.json или создайте свой."
        )

    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)

    sites: dict[str, SiteConfig] = {}
    for key, data in raw.items():
        try:
            selectors = _selectors_from_dict(data["selectors"])
        except KeyError as error:
            raise ValueError(
                f"В настройках сайта «{key}» не хватает обязательного селектора: {error}"
            ) from error

        sites[key] = SiteConfig(
            key=key,
            title=data.get("title", key),
            base_url=data["base_url"],
            start_url=data.get("start_url", data["base_url"]),
            selectors=selectors,
            max_pages=int(data.get("max_pages", 1)),
            request_delay=float(data.get("request_delay", 1.0)),
            respect_robots=bool(data.get("respect_robots", True)),
            in_stock_text=data.get("in_stock_text", "in stock"),
        )

    if not sites:
        raise ValueError(f"Файл {path} не содержит ни одного сайта.")

    return sites
