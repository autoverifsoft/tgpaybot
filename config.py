"""Настройки из переменных окружения."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _clean(value: str | None) -> str:
    return (value or "").strip()


BOT_TOKEN = _clean(os.getenv("BOT_TOKEN"))
CRYPTOBOT_TOKEN = _clean(os.getenv("CRYPTOBOT_TOKEN"))

ADMIN_IDS: list[int] = [
    int(x) for x in _clean(os.getenv("ADMIN_IDS")).replace(" ", "").split(",") if x.isdigit()
]

SHOP_NAME = _clean(os.getenv("SHOP_NAME")) or "Мой магазин"
SUPPORT_USERNAME = _clean(os.getenv("SUPPORT_USERNAME")).lstrip("@")

# Render подставляет RENDER_EXTERNAL_URL сам — тогда адрес указывать не нужно
PUBLIC_URL = (_clean(os.getenv("PUBLIC_URL")) or _clean(os.getenv("RENDER_EXTERNAL_URL"))).rstrip("/")

# Render передаёт порт через PORT. Слушать надо именно его, иначе сервис не поднимется
PORT = int(_clean(os.getenv("PORT")) or 8080)
HOST = "0.0.0.0"

# По умолчанию — файл рядом с ботом. На Render он стирается при перезапуске,
# поэтому в проде подставляют бесплатный Postgres (см. README).
DATABASE_URL = _clean(os.getenv("DATABASE_URL")) or "sqlite+aiosqlite:///./bot.db"

PG_REQUIRE_SSL = False


def _normalize_db_url(url: str) -> str:
    """Привести строку подключения к виду, понятному asyncpg.

    Neon и Render выдают ссылки вида
        postgresql://user:pass@host/db?sslmode=require&channel_binding=require
    Драйвер asyncpg таких параметров не знает и падает при старте, поэтому
    вырезаем их, а SSL включаем отдельно через connect_args.
    """
    global PG_REQUIRE_SSL

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    if "postgresql+asyncpg" not in url or "?" not in url:
        return url

    base, _, query = url.partition("?")
    kept: list[str] = []
    for part in query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0].lower()
        if key == "sslmode":
            value = part.split("=", 1)[1].lower() if "=" in part else ""
            PG_REQUIRE_SSL = value not in {"disable", "allow"}
        elif key in {"channel_binding", "options", "target_session_attrs"}:
            continue  # asyncpg их не принимает в URL
        else:
            kept.append(part)

    return base + ("?" + "&".join(kept) if kept else "")


DATABASE_URL = _normalize_db_url(DATABASE_URL)
IS_POSTGRES = DATABASE_URL.startswith("postgresql")

CRYPTOBOT_API = (
    "https://testnet-pay.crypt.bot/api"
    if _clean(os.getenv("CRYPTOBOT_NETWORK")) == "testnet"
    else "https://pay.crypt.bot/api"
)
CRYPTOBOT_FIAT = _clean(os.getenv("CRYPTOBOT_FIAT")) or "RUB"
CRYPTOBOT_ASSETS = _clean(os.getenv("CRYPTOBOT_ASSETS")) or "USDT,TON"
INVOICE_TTL_MIN = int(_clean(os.getenv("INVOICE_TTL_MIN")) or 60)

WEBHOOK_SECRET = _clean(os.getenv("WEBHOOK_SECRET")) or "change-me-please"
TELEGRAM_PATH = "/tg"
CRYPTOBOT_PATH = "/cryptobot"

# Как часто перепроверять неоплаченные счета (страховка на случай,
# если вебхук CryptoBot не доехал)
RECONCILE_SECONDS = 60


def validate() -> list[str]:
    """Вернуть список проблем в конфиге. Пустой список = всё в порядке."""
    problems: list[str] = []
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN не задан — возьмите токен у @BotFather")
    if not CRYPTOBOT_TOKEN:
        problems.append(
            "CRYPTOBOT_TOKEN не задан — @CryptoBot → Crypto Pay → Create App"
        )
    if not ADMIN_IDS:
        problems.append("ADMIN_IDS не задан — ваш числовой ID можно узнать у @userinfobot")
    return problems
