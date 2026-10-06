"""Разбор цен из текста.

Сайты пишут цены как угодно: `£51.77`, `$1,234.56`, `1 234,56 ₽`, `12,50 €`.
Задача — не ошибиться в разделителе: `1.234` и `1,234` в одних странах
означают тысячу двести тридцать четыре, в других — один и две десятых.
"""

from __future__ import annotations

import re

# Символ -> код валюты
CURRENCY_SYMBOLS: dict[str, str] = {
    "₽": "RUB",
    "р.": "RUB",
    "руб": "RUB",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "₴": "UAH",
    "₸": "KZT",
    "¥": "JPY",
    "₺": "TRY",
    "zł": "PLN",
    "Br": "BYN",
}

# Коды, которые могут встретиться прямо в тексте
CURRENCY_CODES = {"RUB", "USD", "EUR", "GBP", "UAH", "KZT", "PLN", "TRY", "JPY", "BYN"}

UNKNOWN_CURRENCY = "UNKNOWN"

# Число: начинается с цифры, дальше цифры, пробелы и разделители
_NUMBER_RE = re.compile(r"\d[\d\s\u00a0.,]*")
_SPACES_RE = re.compile(r"[\s\u00a0\u202f]")


def detect_currency(raw: str) -> str:
    """Определяет валюту по символу или коду в тексте."""
    lowered = raw.lower()

    # Сначала ищем код валюты — он однозначнее символа
    for code in CURRENCY_CODES:
        if re.search(rf"\b{code.lower()}\b", lowered):
            return code

    # Затем символы, длинные раньше коротких (zł важнее, чем $).
    # Буквенные символы ищем по границам слова: иначе «Br» нашёлся бы
    # внутри английского слова вроде «Brave» и цена уехала бы в BYN.
    for symbol in sorted(CURRENCY_SYMBOLS, key=len, reverse=True):
        if symbol.isalpha():
            if re.search(rf"(?<!\w){re.escape(symbol)}(?!\w)", lowered):
                return CURRENCY_SYMBOLS[symbol]
        elif symbol in lowered:
            return CURRENCY_SYMBOLS[symbol]

    return UNKNOWN_CURRENCY


def _normalize_number(digits: str) -> float | None:
    """Превращает числовую часть в float, разбираясь с разделителями."""
    text = _SPACES_RE.sub("", digits)
    if not text:
        return None

    has_comma = "," in text
    has_dot = "." in text

    if has_comma and has_dot:
        # Десятичный разделитель — тот, что стоит позже
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma or has_dot:
        separator = "," if has_comma else "."
        parts = text.split(separator)

        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            # Один разделитель и 1–2 знака после него — это дробная часть
            text = f"{parts[0]}.{parts[1]}"
        else:
            # Иначе разделитель — тысячный: 1.234 -> 1234, 1,234,567 -> 1234567
            text = text.replace(separator, "")

    try:
        value = float(text)
    except ValueError:
        return None

    return value if value >= 0 else None


def parse_price(raw: str | None) -> tuple[float, str] | None:
    """Разбирает строку с ценой.

    Возвращает `(сумма, код валюты)` или None, если числа в строке нет.

    >>> parse_price("£51.77")
    (51.77, 'GBP')
    >>> parse_price("1 234,56 ₽")
    (1234.56, 'RUB')
    >>> parse_price("$1,234.56")
    (1234.56, 'USD')
    """
    if not raw:
        return None

    match = _NUMBER_RE.search(raw)
    if not match:
        return None

    value = _normalize_number(match.group(0))
    if value is None:
        return None

    return value, detect_currency(raw)


# Валюты, у которых символ пишется после суммы: «1 234,56 ₽», «12,50 zł»
SYMBOL_AFTER = {"RUB", "UAH", "KZT", "BYN", "PLN", "TRY"}

# Валюты с запятой в качестве десятичного разделителя
COMMA_DECIMAL = {"RUB", "UAH", "KZT", "BYN", "EUR", "PLN", "TRY"}

CURRENCY_SYMBOLS_DISPLAY: dict[str, str] = {
    "RUB": "₽",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "UAH": "₴",
    "KZT": "₸",
    "PLN": "zł",
    "TRY": "₺",
    "JPY": "¥",
    "BYN": "Br",
}


def format_price(value: float, currency: str) -> str:
    """Красивый вывод цены для сообщений и отчётов.

    >>> format_price(1234.5, "RUB")
    '1 234,50 ₽'
    >>> format_price(51.77, "GBP")
    '£51.77'
    """
    amount = f"{value:,.2f}".replace(",", " ")

    if currency in COMMA_DECIMAL:
        amount = amount.replace(".", ",")

    symbol = CURRENCY_SYMBOLS_DISPLAY.get(currency)
    if symbol is None:
        return f"{amount} {currency}"

    if currency in SYMBOL_AFTER:
        return f"{amount} {symbol}"

    return f"{symbol}{amount}"
