"""Загрузка страниц.

Парсер, который бьёт по сайту без задержек и без проверки robots.txt,
приносит заказчику не данные, а блокировку по IP. Поэтому здесь:
повторы с нарастающей паузой, пауза между запросами и уважение к robots.txt.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Повторяем только то, что имеет смысл повторять
RETRY_STATUSES = (429, 500, 502, 503, 504)


class FetchError(RuntimeError):
    """Страницу не удалось получить."""


class Fetcher:
    """Обёртка над requests с вежливыми настройками."""

    def __init__(
        self,
        *,
        user_agent: str,
        delay: float = 1.0,
        timeout: int = 20,
        respect_robots: bool = True,
    ) -> None:
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.respect_robots = respect_robots

        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru,en;q=0.9",
            }
        )

        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.6,
            status_forcelist=RETRY_STATUSES,
            allowed_methods=("GET",),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=4)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

        self._robots_cache: dict[str, RobotFileParser | None] = {}
        self._last_request_at = 0.0

    # --- robots.txt ---

    def _robots_for(self, url: str) -> RobotFileParser | None:
        """Загружает и кэширует robots.txt для домена."""
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        if origin in self._robots_cache:
            return self._robots_cache[origin]

        robots_url = urljoin(origin, "/robots.txt")
        parser = RobotFileParser()
        parser.set_url(robots_url)

        try:
            response = self._session.get(robots_url, timeout=self.timeout)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            else:
                # Нет robots.txt — значит, ограничений нет
                logger.debug("robots.txt отсутствует (%s): %s", response.status_code, robots_url)
                parser = None
        except requests.RequestException as error:
            logger.warning("Не удалось прочитать robots.txt (%s): %s", robots_url, error)
            parser = None

        self._robots_cache[origin] = parser
        return parser

    def can_fetch(self, url: str) -> bool:
        """Разрешён ли адрес к обходу согласно robots.txt."""
        if not self.respect_robots:
            return True

        parser = self._robots_for(url)
        if parser is None:
            return True

        allowed = parser.can_fetch(self._session.headers["User-Agent"], url)
        if not allowed:
            logger.warning("robots.txt запрещает обход: %s", url)
        return allowed

    # --- загрузка ---

    def _throttle(self) -> None:
        """Держит паузу между запросами, чтобы не долбить сайт."""
        if self.delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def get_html(self, url: str, *, check_robots: bool = True) -> str:
        """Скачивает страницу и возвращает HTML.

        Бросает FetchError, если страницу получить не удалось.
        """
        if check_robots and not self.can_fetch(url):
            raise FetchError(f"robots.txt запрещает обход: {url}")

        self._throttle()
        try:
            response = self._session.get(url, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as error:
            raise FetchError(f"Не удалось загрузить {url}: {error}") from error
        finally:
            self._last_request_at = time.monotonic()

        # Если сервер не указал кодировку явно, requests подставляет
        # ISO-8859-1 — для русских сайтов это превращает текст в кашу.
        if response.encoding is None or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding

        return response.text

    def try_get_html(self, url: str, *, check_robots: bool = True) -> str | None:
        """Как get_html, но возвращает None вместо исключения."""
        try:
            return self.get_html(url, check_robots=check_robots)
        except FetchError as error:
            logger.error("%s", error)
            return None

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
