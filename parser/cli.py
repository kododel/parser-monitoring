"""Командный интерфейс.

    python -m parser.cli sites                  # что настроено
    python -m parser.cli parse  --site books    # собрать и сохранить
    python -m parser.cli watch                  # собрать и уведомить в Telegram
    python -m parser.cli report                 # выгрузить Excel
    python -m parser.cli stats                  # сводка по базе
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

from parser.config import Settings, SiteConfig, load_settings, load_sites
from parser.fetcher import Fetcher
from parser.models import RunResult
from parser.monitor import run_once
from parser.notifier import notify
from parser.report import export_to_excel
from parser.storage import Storage, to_local

logger = logging.getLogger("parser")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def pick_sites(sites: dict[str, SiteConfig], wanted: str) -> list[SiteConfig]:
    """Выбирает сайты по имени. 'all' — все."""
    if wanted == "all":
        return list(sites.values())

    if wanted not in sites:
        available = ", ".join(sorted(sites)) or "нет ни одного"
        raise SystemExit(f"Сайт «{wanted}» не описан в конфиге. Доступные: {available}")

    return [sites[wanted]]


def make_fetcher(config: SiteConfig, settings: Settings) -> Fetcher:
    return Fetcher(
        user_agent=settings.user_agent,
        delay=config.request_delay,
        timeout=settings.timeout,
        respect_robots=config.respect_robots,
    )


def print_result(result: RunResult, config: SiteConfig) -> None:
    """Короткий отчёт в консоль."""
    print(f"\n— {config.title}")
    print(f"  собрано товаров: {result.collected}")
    if result.new_products:
        print(f"  новых: {result.new_products}")

    if result.changes:
        print(f"  изменения цен ({len(result.changes)}):")
        for change in result.changes[:10]:
            print(
                f"    {change.arrow} {change.product.short_title}: "
                f"{change.old_price:g} → {change.new_price:g} "
                f"({change.percent:+.1f}%)"
            )
        if len(result.changes) > 10:
            print(f"    …и ещё {len(result.changes) - 10}")
    else:
        print("  цены не изменились")

    if result.errors:
        print(f"  ⚠️ проблем: {len(result.errors)}")
        for error in result.errors[:5]:
            print(f"    • {error}")


def command_sites(args: argparse.Namespace, settings: Settings) -> int:
    sites = load_sites(settings.sites_file)
    print(f"Файл конфигурации: {settings.sites_file}\n")
    for key, config in sites.items():
        print(f"  {key}")
        print(f"    название:      {config.title}")
        print(f"    адрес:         {config.start_url}")
        print(f"    страниц:       {config.max_pages}")
        print(f"    пауза:         {config.request_delay} с")
        print(f"    robots.txt:    {'учитывается' if config.respect_robots else 'игнорируется'}")
    return 0


def command_parse(args: argparse.Namespace, settings: Settings) -> int:
    sites = load_sites(settings.sites_file)
    selected = pick_sites(sites, args.site)

    total_collected = 0
    total_changes = 0

    with Storage(settings.db_path) as storage:
        for config in selected:
            if args.pages is not None:
                config = replace(config, max_pages=args.pages)

            with make_fetcher(config, settings) as fetcher:
                result = run_once(config, storage, fetcher)

            print_result(result, config)
            total_collected += result.collected
            total_changes += len(result.changes)

    print(f"\nИтого: {total_collected} товаров, {total_changes} изменений")
    print(f"База: {settings.db_path}")
    return 0 if total_collected else 1


def command_watch(args: argparse.Namespace, settings: Settings) -> int:
    sites = load_sites(settings.sites_file)
    selected = pick_sites(sites, args.site)

    if not settings.telegram_ready:
        for problem in settings.problems():
            logger.error(problem)
        logger.error("Заполните .env по образцу .env.example.")
        return 2

    exit_code = 0
    with Storage(settings.db_path) as storage:
        for config in selected:
            if args.pages is not None:
                config = replace(config, max_pages=args.pages)

            with make_fetcher(config, settings) as fetcher:
                result = run_once(config, storage, fetcher)

            print_result(result, config)

            delivered = notify(
                token=settings.telegram_token,
                chat_ids=settings.telegram_chat_ids,
                result=result,
                site_title=config.title,
                only_when_changed=not args.always,
            )
            print(f"  уведомлений доставлено: {delivered}")

            if not result.collected:
                exit_code = 1

    return exit_code


def command_report(args: argparse.Namespace, settings: Settings) -> int:
    with Storage(settings.db_path) as storage:
        stats = storage.stats()
        if not stats["products"]:
            logger.error("В базе нет товаров. Сначала выполните: python -m parser.cli parse")
            return 1

        path = export_to_excel(storage, settings.export_dir)

    print(f"Файл готов: {path}")
    print(f"  товаров:   {stats['products']}")
    print(f"  замеров:   {stats['measurements']}")
    print(f"  изменений: {stats['changes']}")
    return 0


def command_stats(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.db_path.exists():
        logger.error("Базы ещё нет: %s. Сначала выполните parse.", settings.db_path)
        return 1

    with Storage(settings.db_path) as storage:
        stats = storage.stats()
        changes = storage.all_changes()

    print(f"База: {settings.db_path}\n")
    print(f"  товаров отслеживается:  {stats['products']}")
    print(f"  всего замеров цены:     {stats['measurements']}")
    print(f"  изменений за всё время: {stats['changes']}")
    print(f"  изменений за 7 дней:    {stats['changes_week']}")

    if stats["last_run"]:
        print(f"  последний сбор:         {to_local(stats['last_run']).strftime('%d.%m.%Y %H:%M')}")
    else:
        print("  последний сбор:         ещё не было")

    if changes:
        print("\nПоследние изменения:")
        for row in changes[:10]:
            diff = round(row["new_price"] - row["old_price"], 2)
            arrow = "🔻" if diff < 0 else "🔺"
            print(f"  {arrow} {row['title'][:50]}: {row['old_price']:g} → {row['new_price']:g}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parser",
        description="Мониторинг цен: сбор, история, уведомления, выгрузка в Excel.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")

    # Опции сбора нужны только командам parse и watch, поэтому живут
    # в родительском парсере: `parser parse --site books --pages 2`.
    collect_options = argparse.ArgumentParser(add_help=False)
    collect_options.add_argument(
        "--site",
        default="all",
        help="ключ сайта из config/sites.json или all (по умолчанию)",
    )
    collect_options.add_argument(
        "--pages", type=int, default=None, help="ограничить число страниц (для проверки)"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("sites", help="показать настроенные сайты")
    subparsers.add_parser(
        "parse", parents=[collect_options], help="собрать цены и сохранить в базу"
    )
    subparsers.add_parser(
        "watch", parents=[collect_options], help="собрать и отправить уведомление в Telegram"
    )
    subparsers.add_parser("report", help="выгрузить всё в Excel")
    subparsers.add_parser("stats", help="показать сводку по базе")

    parser.epilog = (
        "Примеры:\n"
        "  python -m parser.cli sites\n"
        "  python -m parser.cli parse --site books --pages 2\n"
        "  python -m parser.cli watch\n"
        "  python -m parser.cli report\n"
    )
    return parser


COMMANDS = {
    "sites": command_sites,
    "parse": command_parse,
    "watch": command_watch,
    "report": command_report,
    "stats": command_stats,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    settings = load_settings()
    return COMMANDS[args.command](args, settings)


if __name__ == "__main__":
    sys.exit(main())
