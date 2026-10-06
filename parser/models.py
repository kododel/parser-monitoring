"""Модели данных.

Отдельные неизменяемые структуры вместо словарей: видно, какие поля
реально есть у товара, и опечатку ловит не рантайм, а чтение кода.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Product:
    """Товар, каким он был на сайте в момент сбора."""

    site: str
    external_id: str
    title: str
    url: str
    price: float
    currency: str
    in_stock: bool
    rating: str | None = None

    @property
    def short_title(self) -> str:
        """Заголовок для сообщений и таблиц."""
        return self.title if len(self.title) <= 60 else self.title[:57] + "…"


@dataclass(frozen=True)
class PriceChange:
    """Изменение цены относительно предыдущего замера."""

    product: Product
    old_price: float
    new_price: float

    @property
    def diff(self) -> float:
        """Разница в валюте товара. Отрицательная — подешевело."""
        return round(self.new_price - self.old_price, 2)

    @property
    def percent(self) -> float:
        """Разница в процентах. 0, если старая цена была нулевой."""
        if self.old_price == 0:
            return 0.0
        return round((self.new_price - self.old_price) / self.old_price * 100, 2)

    @property
    def went_down(self) -> bool:
        return self.new_price < self.old_price

    @property
    def arrow(self) -> str:
        return "🔻" if self.went_down else "🔺"


@dataclass
class RunResult:
    """Итог одного прогона сбора."""

    site: str
    collected: int = 0
    new_products: int = 0
    changes: list[PriceChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_news(self) -> bool:
        return bool(self.changes or self.new_products)

    @property
    def dropped(self) -> list[PriceChange]:
        """Товары, которые подешевели."""
        return [c for c in self.changes if c.went_down]
