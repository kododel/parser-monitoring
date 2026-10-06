"""Смоук-тест. Работает полностью офлайн, на сохранённой странице.

Запуск из корня проекта:
    python tests/smoke_test.py

Сеть не нужна: страница лежит в tests/fixtures/catalogue.html.
Поэтому тест можно гонять в CI и на машине без интернета.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser.config import (  # noqa: E402
    Selectors,
    SiteConfig,
    _parse_chat_ids,
    load_settings,
    load_sites,
)
from parser.fetcher import Fetcher  # noqa: E402
from parser.models import PriceChange, Product, RunResult  # noqa: E402
from parser.notifier import build_report  # noqa: E402
from parser.prices import detect_currency, format_price, parse_price  # noqa: E402
from parser.report import build_workbook  # noqa: E402
from parser.scraper import extract_external_id, find_next_page, parse_products  # noqa: E402
from parser.storage import Storage  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def test_prices() -> None:
    print("\n[1/8] Разбор цен")

    cases = {
        "£51.77": (51.77, "GBP"),
        "$1,234.56": (1234.56, "USD"),
        "1 234,56 ₽": (1234.56, "RUB"),
        "12,50 €": (12.5, "EUR"),
        "1.234": (1234.0, "UNKNOWN"),
        "1,234": (1234.0, "UNKNOWN"),
        "2 490 руб": (2490.0, "RUB"),
        "10 zł": (10.0, "PLN"),
        # Ловушка: «Br» (белорусский рубль) не должен находиться внутри слова
        "Brave New World £9.99": (9.99, "GBP"),
    }
    for raw, expected in cases.items():
        got = parse_price(raw)
        check(f"{raw!r} -> {expected}", got == expected, f"получено {got}")

    for bad in ["цена договорная", "", None, "по запросу"]:
        check(f"{bad!r} -> None", parse_price(bad) is None, f"получено {parse_price(bad)}")

    check("валюта по символу", detect_currency("100 ₽") == "RUB")
    check("валюта по коду", detect_currency("100 RUB") == "RUB")
    check("неизвестная валюта", detect_currency("100") == "UNKNOWN")

    check("формат RUB", format_price(1234.5, "RUB") == "1 234,50 ₽", format_price(1234.5, "RUB"))
    check("формат GBP", format_price(51.77, "GBP") == "£51.77", format_price(51.77, "GBP"))
    check("формат EUR", format_price(12.5, "EUR") == "€12,50", format_price(12.5, "EUR"))


def make_site_config(**overrides) -> SiteConfig:
    base = SiteConfig(
        key="test",
        title="Тестовый сайт",
        base_url="https://example.com/",
        start_url="https://example.com/catalogue/page-1.html",
        selectors=Selectors(
            item="article.product_pod",
            link="h3 a",
            price="p.price_color",
            title_attr="title",
            availability="p.instock.availability",
            rating="p.star-rating",
            next_page="li.next a",
        ),
        max_pages=2,
        request_delay=0.0,
        respect_robots=False,
        in_stock_text="in stock",
    )
    return replace(base, **overrides)


def test_scraper() -> None:
    print("\n[2/8] Разбор страницы")

    html = (FIXTURES / "catalogue.html").read_text(encoding="utf-8")
    config = make_site_config()
    result = parse_products(html, config=config, page_url=config.start_url)

    by_title = {p.title: p for p in result.products}

    check("разобрано 4 товара из 6 карточек", len(result.products) == 4, str(len(result.products)))
    check("пропущено 2 карточки", len(result.skipped) == 2, str(result.skipped))
    check("заголовок взят из атрибута title", "Обычная книга" in by_title)

    обычная = by_title.get("Обычная книга")
    check("цена разобрана", обычная is not None and обычная.price == 51.77)
    check("валюта определена", обычная is not None and обычная.currency == "GBP")
    check("наличие: в наличии", обычная is not None and обычная.in_stock is True)
    check("рейтинг из классов", обычная is not None and обычная.rating == "Three")
    check(
        "ссылка стала абсолютной",
        обычная is not None and обычная.url == "https://example.com/catalogue/kniga-obychnaya_1/index.html",
        обычная.url if обычная else "нет товара",
    )

    нет_в_наличии = by_title.get("Книга не в наличии")
    check(
        "наличие: нет в наличии",
        нет_в_наличии is not None and нет_в_наличии.in_stock is False,
    )

    в_евро = by_title.get("Книга в евро")
    check("европейский формат цены", в_евро is not None and в_евро.price == 1234.56)
    check("валюта EUR", в_евро is not None and в_евро.currency == "EUR")

    без_блока = by_title.get("Книга без блока наличия")
    check(
        "без блока наличия считаем «в наличии»",
        без_блока is not None and без_блока.in_stock is True,
    )

    check(
        "в причинах пропуска есть цена",
        any("цену" in reason for reason in result.skipped),
        str(result.skipped),
    )
    check(
        "в причинах пропуска есть ссылка",
        any("ссылк" in reason for reason in result.skipped),
        str(result.skipped),
    )

    next_page = find_next_page(html, config=config, page_url=config.start_url)
    check(
        "найдена следующая страница",
        next_page == "https://example.com/catalogue/page-2.html",
        str(next_page),
    )

    check(
        "если селектора следующей страницы нет — None",
        find_next_page(html, config=make_site_config(selectors=replace(config.selectors, next_page=None)), page_url=config.start_url)
        is None,
    )

    check(
        "external_id берётся из пути",
        extract_external_id("https://example.com/catalogue/kniga_1/index.html?x=1")
        == "catalogue/kniga_1/index.html",
        extract_external_id("https://example.com/catalogue/kniga_1/index.html?x=1"),
    )


def test_config() -> None:
    print("\n[3/8] Конфигурация")

    check("CHAT_IDS разбирается", _parse_chat_ids("111,222") == [111, 222])
    check("отрицательный id (группа)", _parse_chat_ids("-1001234567890") == [-1001234567890])
    check("мусор в CHAT_IDS пропускается", _parse_chat_ids("111, abc, ") == [111])

    sites = load_sites(PROJECT_ROOT / "config" / "sites.json")
    check("в конфиге есть сайт books", "books" in sites)
    books = sites["books"]
    check("адрес сайта прочитан", books.base_url.startswith("https://"))
    check("селектор карточки прочитан", books.selectors.item == "article.product_pod")
    check("robots.txt учитывается", books.respect_robots is True)

    check(
        "относительная ссылка склеивается",
        books.absolute("catalogue/x/index.html")
        == "https://books.toscrape.com/catalogue/x/index.html",
        books.absolute("catalogue/x/index.html"),
    )
    check("домен извлекается", books.domain == "books.toscrape.com", books.domain)

    settings = load_settings()
    check("настройки загружаются", settings.timeout > 0)
    check("без токена Telegram не готов", settings.telegram_ready is False)
    check("проблемы конфига перечислены", len(settings.problems()) == 2, str(settings.problems()))


def test_fetcher_robots() -> None:
    print("\n[4/8] robots.txt")

    class DenyingRobots:
        def can_fetch(self, user_agent: str, url: str) -> bool:
            return False

    fetcher = Fetcher(user_agent="test-bot", delay=0.0, respect_robots=True)
    fetcher._robots_for = lambda url: DenyingRobots()  # type: ignore[assignment]
    check("запрет robots.txt соблюдается", fetcher.can_fetch("https://example.com/x") is False)
    fetcher.close()

    permissive = Fetcher(user_agent="test-bot", delay=0.0, respect_robots=False)
    check(
        "при respect_robots=false обход разрешён",
        permissive.can_fetch("https://example.com/x") is True,
    )
    permissive.close()

    absent = Fetcher(user_agent="test-bot", delay=0.0, respect_robots=True)
    absent._robots_for = lambda url: None  # type: ignore[assignment]
    check("нет robots.txt — обход разрешён", absent.can_fetch("https://example.com/x") is True)
    absent.close()

    # Пауза между запросами не должна растягивать первый запрос
    throttled = Fetcher(user_agent="test-bot", delay=0.05)
    import time

    started = time.monotonic()
    throttled._throttle()
    check("первый запрос без паузы", time.monotonic() - started < 0.05)
    throttled.close()


def test_storage(tmp: Path) -> None:
    print("\n[5/8] Хранилище и история цен")

    storage = Storage(tmp / "test.db")

    первый = Product(
        site="test", external_id="a", title="Товар А", url="https://x/a",
        price=100.0, currency="RUB", in_stock=True, rating="Five",
    )
    второй = Product(
        site="test", external_id="b", title="Товар Б", url="https://x/b",
        price=50.0, currency="RUB", in_stock=True,
    )

    new_count, changes = storage.save_run([первый, второй])
    check("первый прогон: 2 новых товара", new_count == 2, str(new_count))
    check("первый прогон: изменений нет", changes == [], str(changes))

    # Тот же товар, но подешевел
    new_count, changes = storage.save_run([replace(первый, price=90.0), второй])
    check("второй прогон: новых нет", new_count == 0, str(new_count))
    check("изменение поймано", len(changes) == 1, str(len(changes)))
    check("старая цена сохранена", changes and changes[0].old_price == 100.0)
    check("новая цена сохранена", changes and changes[0].new_price == 90.0)
    check("подешевело", changes and changes[0].went_down is True)
    check("процент посчитан", changes and changes[0].percent == -10.0, str(changes[0].percent if changes else None))
    check("стрелка вниз", changes and changes[0].arrow == "🔻")

    # Дребезг меньше копейки изменением не считаем
    new_count, changes = storage.save_run([replace(первый, price=90.005), второй])
    check("дребезг меньше 0.01 не считается изменением", changes == [], str(changes))

    # Смена валюты — не изменение цены
    new_count, changes = storage.save_run([replace(первый, price=90.0, currency="USD"), второй])
    check("смена валюты не считается изменением", changes == [], str(changes))

    # Смена статуса наличия при той же цене
    new_count, changes = storage.save_run([replace(первый, price=90.0, currency="USD", in_stock=False), второй])
    check("смена наличия не считается изменением цены", changes == [], str(changes))

    # Прогонов было пять, в каждом по два товара — значит, замеров десять.
    # История пишется на каждый замер, а не только при изменении цены.
    stats = storage.stats()
    check("статистика: 2 товара", stats["products"] == 2, str(stats["products"]))
    check("статистика: 1 изменение", stats["changes"] == 1, str(stats["changes"]))
    check("статистика: замеров 10", stats["measurements"] == 10, str(stats["measurements"]))
    check("статистика: за 7 дней 1", stats["changes_week"] == 1)
    check("статистика: есть дата последнего сбора", bool(stats["last_run"]))

    products = storage.all_products()
    check("товары читаются", len(products) == 2)
    a = next(p for p in products if p["external_id"] == "a")
    check("последняя цена в товаре", a["last_price"] == 90.0, str(a["last_price"]))
    check("последнее наличие в товаре", a["last_in_stock"] == 0, str(a["last_in_stock"]))

    history = storage.history()
    check("история пишется на каждый замер", len(history) == 10, str(len(history)))

    all_changes = storage.all_changes()
    check("изменение попало в отдельную таблицу", len(all_changes) == 1)
    check("в изменении есть название товара", all_changes[0]["title"] == "Товар А")

    storage.close()


def test_report(tmp: Path) -> None:
    print("\n[6/8] Выгрузка в Excel")

    storage = Storage(tmp / "report.db")
    # Время замера задаём вручную: два прогона внутри одной секунды дали бы
    # одинаковую метку, и график построить было бы не из чего.
    storage.save_run(
        [
            Product(site="test", external_id="a", title="Товар А", url="https://x/a",
                    price=100.0, currency="RUB", in_stock=True, rating="Five"),
            Product(site="test", external_id="b", title="Товар Б", url="https://x/b",
                    price=50.0, currency="USD", in_stock=False),
        ],
        captured_at="2026-10-01T10:00:00+00:00",
    )
    storage.save_run(
        [
            Product(site="test", external_id="a", title="Товар А", url="https://x/a",
                    price=80.0, currency="RUB", in_stock=True, rating="Five"),
            Product(site="test", external_id="b", title="Товар Б", url="https://x/b",
                    price=50.0, currency="USD", in_stock=False),
        ],
        captured_at="2026-10-02T10:00:00+00:00",
    )

    workbook = build_workbook(storage)
    names = workbook.sheetnames
    check("лист «Товары» есть", "Товары" in names, str(names))
    check("лист «Изменения» есть", "Изменения" in names)
    check("лист «История цен» есть", "История цен" in names)
    check("лист «Динамика» есть", "Динамика" in names)
    check("лист «Сводка» есть", "Сводка" in names)

    sheet = workbook["Товары"]
    check("шапка товаров на месте", sheet.cell(row=1, column=1).value == "№")
    check("два товара в выгрузке", sheet.max_row == 3, str(sheet.max_row))
    check("цена выгружена", sheet.cell(row=2, column=3).value in (80.0, 50.0))

    changes_sheet = workbook["Изменения"]
    check("изменение выгружено", changes_sheet.max_row == 2, str(changes_sheet.max_row))
    check("шапка изменений", changes_sheet.cell(row=1, column=4).value == "Было")

    dynamics = workbook["Динамика"]
    check("на листе «Динамика» построен график", len(dynamics._charts) == 1, str(len(dynamics._charts)))

    summary = workbook["Сводка"]
    check("сводка: заголовок", summary.cell(row=1, column=1).value == "Сводка по мониторингу")
    check("сводка: товаров 2", summary.cell(row=3, column=2).value == 2)

    storage.close()


def test_notifier() -> None:
    print("\n[7/8] Текст уведомления")

    product = Product(site="test", external_id="a", title="Товар А", url="https://x/a",
                      price=90.0, currency="RUB", in_stock=True)
    result = RunResult(site="test", collected=10, new_products=2)
    result.changes.append(PriceChange(product=product, old_price=100.0, new_price=90.0))

    text = build_report(result, "Тестовый сайт")
    check("в отчёте есть название сайта", "Тестовый сайт" in text)
    check("в отчёте есть число товаров", "Собрано товаров: 10" in text)
    check("в отчёте есть новые товары", "Впервые увидели: 2" in text)
    check("в отчёте есть изменение", "Товар А" in text)
    check("в отчёте есть процент", "-10.0%" in text, text)
    check("в отчёте есть стрелка вниз", "🔻" in text)

    empty = RunResult(site="test", collected=5)
    empty_text = build_report(empty, "Сайт")
    check("без изменений так и написано", "Цены не изменились" in empty_text)
    check("новых товаров нет — строки нет", "Впервые увидели" not in empty_text)

    with_errors = RunResult(site="test", collected=0, errors=["страница не загрузилась"])
    errors_text = build_report(with_errors, "Сайт")
    check("ошибки попадают в отчёт", "Проблем при сборе: 1" in errors_text)


def test_end_to_end(tmp: Path) -> None:
    print("\n[8/8] Сквозной прогон на фикстуре")

    html = (FIXTURES / "catalogue.html").read_text(encoding="utf-8")
    config = make_site_config()

    # Подменяем загрузку страницы: сеть в тестах не нужна
    pages = {
        config.start_url: html,
        "https://example.com/catalogue/page-2.html": html,
    }

    class FakeFetcher:
        def try_get_html(self, url: str, *, check_robots: bool = True) -> str | None:
            return pages.get(url)

        def close(self) -> None:
            pass

    from parser.monitor import collect, run_once

    products, errors = collect(config, FakeFetcher())  # type: ignore[arg-type]
    # Фикстура отдаётся как обе страницы, поэтому 4 товара собираются дважды
    check("обойдены две страницы", len(products) == 8, str(len(products)))
    # В фикстуре намеренно две битые карточки, и они попадаются на каждой странице
    check("битые карточки попали в проблемы", len(errors) == 4, str(errors))
    check(
        "проблемы описывают причину",
        all("цену" in e or "ссылк" in e for e in errors),
        str(errors),
    )

    storage = Storage(tmp / "e2e.db")
    result = run_once(config, storage, FakeFetcher())  # type: ignore[arg-type]
    check("сквозной прогон: собрано 8 карточек", result.collected == 8, str(result.collected))
    # Уникальных товаров 4: обе страницы отдают один и тот же набор
    check("сквозной прогон: 4 уникальных товара", result.new_products == 4, str(result.new_products))
    check("сквозной прогон: без изменений", result.changes == [])
    check("has_news истинно при новых товарах", result.has_news is True)

    # Второй прогон — ни новых, ни изменений
    second = run_once(config, storage, FakeFetcher())  # type: ignore[arg-type]
    check("второй прогон: новых нет", second.new_products == 0, str(second.new_products))
    check("второй прогон: has_news ложно", second.has_news is False)

    storage.close()


def main() -> int:
    print("=" * 62)
    print("Смоук-тест: парсер и мониторинг цен")
    print("=" * 62)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        test_prices()
        test_scraper()
        test_config()
        test_fetcher_robots()
        test_storage(tmp)
        test_report(tmp)
        test_notifier()
        test_end_to_end(tmp)

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} из {CHECKS}")
        for name in FAILURES:
            print(f"  • {name}")
        return 1

    print(f"ВСЁ ХОРОШО: {CHECKS} проверок пройдено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
