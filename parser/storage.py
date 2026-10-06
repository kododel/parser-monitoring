"""Хранилище на SQLite: товары, история цен и зафиксированные изменения.

История пишется всегда, а не только при изменении цены: без неё нельзя
построить график и ответить на вопрос «а сколько это стоило в марте».
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from parser.models import PriceChange, Product

logger = logging.getLogger(__name__)

# Минимальная разница, которую считаем изменением цены.
# Защищает от дребезга вида 199.9999999 против 200.0
PRICE_EPSILON = 0.01

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT    NOT NULL,
    external_id   TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    rating        TEXT,
    first_seen_at TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL,
    UNIQUE (site, external_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    price       REAL    NOT NULL,
    currency    TEXT    NOT NULL,
    in_stock    INTEGER NOT NULL,
    captured_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_product
    ON price_history (product_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS price_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    old_price   REAL    NOT NULL,
    new_price   REAL    NOT NULL,
    currency    TEXT    NOT NULL,
    detected_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_changes_detected
    ON price_changes (detected_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_local(iso: str) -> datetime:
    """ISO-строка (UTC) -> локальное время."""
    try:
        return datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return datetime.now()


class Storage:
    """Обёртка над соединением SQLite."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # --- служебное ---

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- запись ---

    def _upsert_product(self, product: Product, seen_at: str) -> tuple[int, bool]:
        """Возвращает (id товара, был ли он новым)."""
        cursor = self._conn.execute(
            "SELECT id FROM products WHERE site = ? AND external_id = ?",
            (product.site, product.external_id),
        )
        row = cursor.fetchone()

        if row is not None:
            product_id = int(row["id"])
            self._conn.execute(
                """
                UPDATE products
                   SET title = ?, url = ?, rating = ?, last_seen_at = ?
                 WHERE id = ?
                """,
                (product.title, product.url, product.rating, seen_at, product_id),
            )
            return product_id, False

        cursor = self._conn.execute(
            """
            INSERT INTO products
                (site, external_id, title, url, rating, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product.site,
                product.external_id,
                product.title,
                product.url,
                product.rating,
                seen_at,
                seen_at,
            ),
        )
        return int(cursor.lastrowid or 0), True

    def _last_price(self, product_id: int) -> tuple[float, str] | None:
        cursor = self._conn.execute(
            """
            SELECT price, currency
              FROM price_history
             WHERE product_id = ?
             ORDER BY id DESC
             LIMIT 1
            """,
            (product_id,),
        )
        row = cursor.fetchone()
        return (float(row["price"]), str(row["currency"])) if row else None

    def save_run(
        self, products: list[Product], captured_at: str | None = None
    ) -> tuple[int, list[PriceChange]]:
        """Сохраняет результаты сбора.

        Возвращает (сколько товаров увидели впервые, список изменений цены).
        `captured_at` можно задать вручную — это нужно тестам и переносу
        старых выгрузок в базу.
        """
        seen_at = captured_at or _now()
        new_count = 0
        changes: list[PriceChange] = []

        for product in products:
            product_id, is_new = self._upsert_product(product, seen_at)
            if is_new:
                new_count += 1

            previous = self._last_price(product_id)
            if previous is not None:
                old_price, old_currency = previous
                # Сравнивать цены в разных валютах бессмысленно
                if old_currency == product.currency and abs(old_price - product.price) >= PRICE_EPSILON:
                    changes.append(
                        PriceChange(
                            product=product, old_price=old_price, new_price=product.price
                        )
                    )
                    self._conn.execute(
                        """
                        INSERT INTO price_changes
                            (product_id, old_price, new_price, currency, detected_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (product_id, old_price, product.price, product.currency, seen_at),
                    )
                elif old_currency != product.currency:
                    logger.warning(
                        "У товара «%s» сменилась валюта: %s -> %s. Изменение цены не считаем.",
                        product.title,
                        old_currency,
                        product.currency,
                    )

            self._conn.execute(
                """
                INSERT INTO price_history (product_id, price, currency, in_stock, captured_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (product_id, product.price, product.currency, int(product.in_stock), seen_at),
            )

        self._conn.commit()
        return new_count, changes

    # --- чтение ---

    def stats(self) -> dict[str, Any]:
        """Сводка по базе."""
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM products")
        total_products = int(cur.fetchone()["c"])

        cur = self._conn.execute("SELECT COUNT(*) AS c FROM price_history")
        total_measurements = int(cur.fetchone()["c"])

        cur = self._conn.execute("SELECT COUNT(*) AS c FROM price_changes")
        total_changes = int(cur.fetchone()["c"])

        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        cur = self._conn.execute(
            "SELECT COUNT(*) AS c FROM price_changes WHERE detected_at >= ?", (since,)
        )
        changes_week = int(cur.fetchone()["c"])

        cur = self._conn.execute(
            "SELECT MAX(captured_at) AS last FROM price_history"
        )
        row = cur.fetchone()
        last_run = row["last"] if row else None

        return {
            "products": total_products,
            "measurements": total_measurements,
            "changes": total_changes,
            "changes_week": changes_week,
            "last_run": last_run,
        }

    def all_products(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT p.*,
                       (SELECT price FROM price_history h
                         WHERE h.product_id = p.id
                         ORDER BY h.id DESC LIMIT 1) AS last_price,
                       (SELECT currency FROM price_history h
                         WHERE h.product_id = p.id
                         ORDER BY h.id DESC LIMIT 1) AS last_currency,
                       (SELECT in_stock FROM price_history h
                         WHERE h.product_id = p.id
                         ORDER BY h.id DESC LIMIT 1) AS last_in_stock
                  FROM products p
                 ORDER BY p.site, p.title
                """
            )
        )

    def all_changes(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT c.*, p.title, p.url, p.site
                  FROM price_changes c
                  JOIN products p ON p.id = c.product_id
                 ORDER BY c.detected_at DESC
                """
            )
        )

    def history(self, limit_per_product: int = 30) -> list[sqlite3.Row]:
        """История цен: последние N замеров по каждому товару."""
        return list(
            self._conn.execute(
                """
                SELECT h.*, p.title, p.site
                  FROM price_history h
                  JOIN products p ON p.id = h.product_id
                 WHERE h.id IN (
                       SELECT id FROM price_history h2
                        WHERE h2.product_id = h.product_id
                        ORDER BY h2.id DESC
                        LIMIT ?
                 )
                 ORDER BY h.product_id, h.captured_at
                """,
                (limit_per_product,),
            )
        )
