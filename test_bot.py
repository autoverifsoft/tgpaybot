"""Проверка бота без Telegram и без CryptoBot.

Запуск: python test_bot.py
Проверяет главное: товар выдаётся один раз и подпись вебхука работает.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile

os.environ["BOT_TOKEN"] = "1:TEST"
os.environ["ADMIN_IDS"] = "999"
os.environ["CRYPTOBOT_TOKEN"] = "test-app-token"
os.environ["PUBLIC_URL"] = ""
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/t.db"

import config  # noqa: E402
import cryptopay  # noqa: E402
import db  # noqa: E402
from items import BY_ID, ITEMS  # noqa: E402

PASSED = FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ✅ {label}")
    else:
        FAILED += 1
        print(f"  ❌ {label} {detail}")


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kw) -> None:
        self.sent.append((chat_id, text))


def sign(body: bytes) -> str:
    secret = hashlib.sha256(config.CRYPTOBOT_TOKEN.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def paid_body(invoice_id: str) -> bytes:
    return json.dumps(
        {
            "update_type": "invoice_paid",
            "payload": {"invoice_id": invoice_id, "status": "paid"},
        }
    ).encode()


async def main() -> None:
    import app as bot_app

    await db.init()

    print("\n▶ Конфигурация товаров")
    check("товары загружены", len(ITEMS) > 0, f"найдено {len(ITEMS)}")
    check("идентификаторы уникальны", len(BY_ID) == len(ITEMS))
    check("у всех товаров положительная цена", all(i.price > 0 for i in ITEMS))
    check("у всех товаров задана выдача", all(i.payload.strip() for i in ITEMS))
    check("конфиг валиден", config.validate() == [])

    print("\n▶ Подпись вебхука")
    body = paid_body("777")
    check("верная подпись принимается", cryptopay.verify_signature(body, sign(body)))
    check("чужая подпись отвергается", not cryptopay.verify_signature(body, "abc"))
    check(
        "подмена тела ломает подпись",
        not cryptopay.verify_signature(paid_body("778"), sign(body)),
    )
    check("invoice_id извлекается", cryptopay.parse_paid(body) == "777")
    check(
        "неоплаченные апдейты игнорируются",
        cryptopay.parse_paid(
            json.dumps({"update_type": "invoice_expired", "payload": {}}).encode()
        )
        is None,
    )
    check("битый JSON не роняет", cryptopay.parse_paid(b"{{{") is None)

    print("\n▶ Заказ и выдача товара")
    item = ITEMS[0]
    await db.create_order("777", 12345, "client", item.id, item.title, item.price)

    pending = await db.pending_invoice_ids()
    check("новый заказ попал в список ожидающих", "777" in pending)

    fake = FakeBot()
    delivered = await bot_app.deliver(fake, "777", "тест")
    check("выдача произошла", delivered)

    client_msgs = [t for uid, t in fake.sent if uid == 12345]
    admin_msgs = [t for uid, t in fake.sent if uid == 999]
    check("клиент получил товар", len(client_msgs) == 1)
    check(
        "клиенту ушёл именно payload товара",
        client_msgs and client_msgs[0] == item.payload,
    )
    check("админ получил уведомление о продаже", len(admin_msgs) == 1)

    print("\n▶ Защита от повторной выдачи")
    again = await bot_app.deliver(fake, "777", "дубль")
    check("повторная выдача заблокирована", not again)
    check(
        "второе сообщение клиенту НЕ ушло",
        len([t for uid, t in fake.sent if uid == 12345]) == 1,
    )

    pending = await db.pending_invoice_ids()
    check("оплаченный заказ ушёл из ожидающих", "777" not in pending)

    print("\n▶ Одновременные вебхук и сверка")
    await db.create_order("888", 555, None, item.id, item.title, item.price)
    fake2 = FakeBot()
    results = await asyncio.gather(
        bot_app.deliver(fake2, "888", "вебхук"),
        bot_app.deliver(fake2, "888", "сверка"),
    )
    check(
        "из двух параллельных выдач сработала ровно одна",
        sum(1 for r in results if r) == 1,
        f"результаты: {results}",
    )
    check(
        "клиент получил товар один раз",
        len([t for uid, t in fake2.sent if uid == 555]) == 1,
    )

    print("\n▶ Статистика")
    data = await db.stats()
    check("посчитано 2 оплаченных заказа", data["paid"] == 2, str(data))
    check(
        "выручка сложилась верно",
        abs(data["revenue"] - item.price * 2) < 0.01,
        str(data),
    )

    print("\n▶ Неизвестный счёт")
    check("выдача по чужому счёту не проходит", not await bot_app.deliver(fake, "нет", "тест"))

    await db.engine.dispose()

    print("\n" + "─" * 46)
    print(f"Пройдено: {PASSED}   Провалено: {FAILED}")
    if FAILED:
        sys.exit(1)
    print("Все проверки прошли ✅")


if __name__ == "__main__":
    asyncio.run(main())
