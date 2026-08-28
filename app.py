"""Минимальный бот-витрина с оплатой через CryptoBot.

Логика целиком: кнопки → счёт → оплата → автоматическая выдача.
Баланса и корзины нет намеренно — клиент платит сразу за товар.

Запуск локально:  python app.py
На Render:        тот же python app.py, адрес и порт подхватятся сами.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import config
import cryptopay
import db
from items import BY_ID, ITEMS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bot")

dp = Dispatcher()


# ─────────────────────────────── Клавиатуры ───────────────────────────────


def menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for item in ITEMS:
        kb.button(text=item.title, callback_data=f"item:{item.id}")
    kb.adjust(1)
    if config.SUPPORT_USERNAME:
        kb.row(
            InlineKeyboardButton(
                text="💬 Поддержка",
                url=f"https://t.me/{config.SUPPORT_USERNAME}",
            )
        )
    return kb.as_markup()


def item_kb(item_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить", callback_data=f"pay:{item_id}")
    kb.button(text="◀️ Назад", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def invoice_kb(pay_url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🪙 Перейти к оплате", url=pay_url)
    kb.button(text="◀️ В меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


GREETING = (
    "👋 Добро пожаловать в <b>{shop}</b>!\n\n"
    "Выберите, что вас интересует. Оплата — криптовалютой "
    "через @CryptoBot, доступ приходит сюда автоматически сразу после оплаты."
)


# ─────────────────────────────── Хендлеры ────────────────────────────────


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        GREETING.format(shop=config.SHOP_NAME), reply_markup=menu_kb()
    )


@dp.callback_query(F.data == "menu")
async def back_to_menu(call: CallbackQuery) -> None:
    await call.message.edit_text(
        GREETING.format(shop=config.SHOP_NAME), reply_markup=menu_kb()
    )
    await call.answer()


@dp.callback_query(F.data.startswith("item:"))
async def show_item(call: CallbackQuery) -> None:
    item = BY_ID.get(call.data.split(":", 1)[1])
    if item is None:
        await call.answer("Товар недоступен", show_alert=True)
        return

    await call.message.edit_text(
        f"{item.desc}\n\n💰 Цена: <b>{item.price:g} ₽</b>",
        reply_markup=item_kb(item.id),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("pay:"))
async def create_payment(call: CallbackQuery) -> None:
    item = BY_ID.get(call.data.split(":", 1)[1])
    if item is None:
        await call.answer("Товар недоступен", show_alert=True)
        return

    await call.answer("Создаю счёт…")

    try:
        invoice = await cryptopay.create_invoice(
            amount=item.price,
            description=f"{item.title} — {config.SHOP_NAME}",
            payload=json.dumps({"user_id": call.from_user.id, "item_id": item.id}),
        )
    except cryptopay.CryptoPayError as exc:
        log.error("Не удалось создать счёт: %s", exc)
        await call.message.answer(
            "❌ Не получилось создать счёт. Попробуйте позже "
            "или напишите в поддержку."
        )
        return

    await db.create_order(
        invoice_id=invoice["invoice_id"],
        user_id=call.from_user.id,
        username=call.from_user.username,
        item_id=item.id,
        item_title=item.title,
        amount=item.price,
    )

    await call.message.edit_text(
        f"🧾 <b>Счёт на оплату</b>\n\n"
        f"Товар: {item.title}\n"
        f"Сумма: <b>{item.price:g} ₽</b>\n"
        f"Счёт действует {config.INVOICE_TTL_MIN} мин.\n\n"
        "Нажмите кнопку, оплатите — и доступ придёт сюда автоматически.",
        reply_markup=invoice_kb(invoice["pay_url"]),
    )


@dp.message(Command("stats"))
async def stats(message: Message) -> None:
    if message.from_user.id not in config.ADMIN_IDS:
        return

    data = await db.stats()
    lines = [
        "📊 <b>Статистика</b>\n",
        f"Оплачено заказов: <b>{data['paid']}</b>",
        f"Выручка: <b>{data['revenue']:g} ₽</b>",
        f"Ожидают оплаты: {data['pending']}",
    ]

    orders = await db.recent(5)
    if orders:
        lines.append("\n<b>Последние продажи:</b>")
        for order in orders:
            who = f"@{order.username}" if order.username else f"id{order.user_id}"
            lines.append(f"• {order.item_title} — {who}")

    try:
        balances = await cryptopay.get_balance()
        if balances:
            lines.append("\n<b>Баланс CryptoBot:</b>")
            for bal in balances:
                if float(bal.get("available", 0)) > 0:
                    lines.append(f"• {bal.get('currency_code')}: {bal.get('available')}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"\n⚠️ CryptoBot не отвечает: {exc}")

    await message.answer("\n".join(lines))


# ─────────────────────────────── Выдача ──────────────────────────────────


async def deliver(bot: Bot, invoice_id: str, source: str) -> bool:
    """Выдать товар по оплаченному счёту.

    Возвращает True, только если выдача произошла именно сейчас.
    Повторные вызовы по тому же счёту ничего не делают — это защита
    от дублей, когда вебхук и фоновая сверка сработали одновременно.
    """
    order = await db.mark_paid(invoice_id)
    if order is None:
        return False

    item = BY_ID.get(order.item_id)
    payload = item.payload if item else "Оплата получена. Свяжитесь с поддержкой."

    try:
        await bot.send_message(order.user_id, payload)
    except Exception as exc:  # noqa: BLE001
        log.error("Не смог отправить товар пользователю %s: %s", order.user_id, exc)

    who = f"@{order.username}" if order.username else f"id{order.user_id}"
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"💰 <b>Новая продажа</b>\n\n"
                f"Товар: {order.item_title}\n"
                f"Сумма: <b>{float(order.amount):g} ₽</b>\n"
                f"Покупатель: {who}\n"
                f"Счёт: <code>{invoice_id}</code>",
            )
        except Exception:  # noqa: BLE001
            pass

    log.info("Выдан заказ по счёту %s (%s)", invoice_id, source)
    return True


async def reconcile_loop(bot: Bot) -> None:
    """Страховка: раз в минуту проверяем неоплаченные счета.

    Нужна на случай, если вебхук не доехал — сервис спал, сеть моргнула,
    CryptoBot лежал. Без неё клиент платит, а товар не приходит.
    """
    await asyncio.sleep(10)
    while True:
        try:
            pending = await db.pending_invoice_ids()
            if pending:
                paid = await cryptopay.get_paid_invoices(pending)
                for invoice_id in paid:
                    await deliver(bot, invoice_id, source="сверка")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Сверка не удалась: %s", exc)
        await asyncio.sleep(config.RECONCILE_SECONDS)


# ─────────────────────────────── HTTP ────────────────────────────────────


async def healthz(request: web.Request) -> web.Response:
    """Пинг для UptimeRobot, чтобы бесплатный сервис не засыпал."""
    return web.json_response({"status": "ok"})


async def cryptobot_hook(request: web.Request) -> web.Response:
    body = await request.read()
    signature = request.headers.get("crypto-pay-api-signature", "")

    if not cryptopay.verify_signature(body, signature):
        log.warning("Вебхук CryptoBot с неверной подписью — отклонён")
        return web.json_response({"ok": False}, status=403)

    invoice_id = cryptopay.parse_paid(body)
    if invoice_id:
        await deliver(request.app["bot"], invoice_id, source="вебхук")

    return web.json_response({"ok": True})


# ─────────────────────────────── Запуск ──────────────────────────────────


async def on_startup(bot: Bot) -> None:
    await db.init()
    await bot.set_my_commands([BotCommand(command="start", description="Меню")])

    if config.PUBLIC_URL:
        url = config.PUBLIC_URL + config.TELEGRAM_PATH
        await bot.set_webhook(
            url,
            secret_token=config.WEBHOOK_SECRET,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        log.info("Режим: вебхук. Telegram шлёт обновления на %s", url)
        log.info(
            "УКАЖИТЕ ЭТОТ АДРЕС В НАСТРОЙКАХ ПРИЛОЖЕНИЯ CRYPTOBOT: %s",
            config.PUBLIC_URL + config.CRYPTOBOT_PATH,
        )
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Режим: polling (PUBLIC_URL не задан)")
        log.info(
            "Вебхук CryptoBot работать не будет, но оплаты долетят "
            "через сверку за ~%s сек.",
            config.RECONCILE_SECONDS,
        )


async def main() -> None:
    problems = config.validate()
    if problems:
        for problem in problems:
            log.error("КОНФИГ: %s", problem)
        log.error("Заполните переменные окружения и запустите снова.")
        sys.exit(1)

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    await on_startup(bot)

    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", healthz)
    app.router.add_get("/healthz", healthz)
    app.router.add_post(config.CRYPTOBOT_PATH, cryptobot_hook)

    if config.PUBLIC_URL:
        SimpleRequestHandler(
            dispatcher=dp, bot=bot, secret_token=config.WEBHOOK_SECRET
        ).register(app, path=config.TELEGRAM_PATH)
        setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, config.HOST, config.PORT).start()
    log.info("HTTP слушает порт %s", config.PORT)

    task = asyncio.create_task(reconcile_loop(bot))

    try:
        if config.PUBLIC_URL:
            await asyncio.Event().wait()
        else:
            await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runner.cleanup()
        await cryptopay.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
