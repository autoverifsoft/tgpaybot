"""Работа с CryptoBot (Crypto Pay API).

Счёт выставляется сразу в рублях: клиент видит «299 ₽» и платит любой
монетой из списка, пересчёт делает сам CryptoBot.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

import aiohttp

import config

log = logging.getLogger(__name__)

_session: aiohttp.ClientSession | None = None


async def _http() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"Crypto-Pay-API-Token": config.CRYPTOBOT_TOKEN},
        )
    return _session


class CryptoPayError(Exception):
    pass


async def _call(method: str, payload: dict | None = None) -> dict:
    session = await _http()
    try:
        async with session.post(f"{config.CRYPTOBOT_API}/{method}", json=payload or {}) as resp:
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise CryptoPayError(f"CryptoBot недоступен: {exc}") from exc

    if not data.get("ok"):
        raise CryptoPayError(str(data.get("error")))
    return data["result"]


async def create_invoice(amount: float, description: str, payload: str) -> dict:
    """Создать счёт. Возвращает {'invoice_id': ..., 'pay_url': ...}."""
    body = {
        "currency_type": "fiat",
        "fiat": config.CRYPTOBOT_FIAT,
        "amount": f"{amount:.2f}",
        "description": description[:1024],
        "payload": payload,
        "expires_in": config.INVOICE_TTL_MIN * 60,
        "allow_comments": False,
        "allow_anonymous": False,
    }
    assets = [a.strip().upper() for a in config.CRYPTOBOT_ASSETS.split(",") if a.strip()]
    if assets:
        body["accepted_assets"] = ",".join(assets)

    result = await _call("createInvoice", body)
    return {
        "invoice_id": str(result["invoice_id"]),
        "pay_url": result.get("bot_invoice_url") or result.get("pay_url"),
    }


async def get_paid_invoices(invoice_ids: list[str]) -> list[str]:
    """Из переданных счетов вернуть те, что уже оплачены."""
    if not invoice_ids:
        return []
    result = await _call(
        "getInvoices", {"invoice_ids": ",".join(invoice_ids), "count": 1000}
    )
    items = result.get("items", []) if isinstance(result, dict) else result
    return [str(i["invoice_id"]) for i in items if i.get("status") == "paid"]


async def get_balance() -> list[dict]:
    return await _call("getBalance")


def verify_signature(body: bytes, signature: str) -> bool:
    """Подпись вебхука: HMAC-SHA256(SHA256(токен), тело запроса)."""
    if not signature or not config.CRYPTOBOT_TOKEN:
        return False
    secret = hashlib.sha256(config.CRYPTOBOT_TOKEN.encode()).digest()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_paid(body: bytes) -> str | None:
    """Достать invoice_id из уведомления об оплате."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("CryptoBot прислал невалидный JSON")
        return None

    if data.get("update_type") != "invoice_paid":
        return None

    invoice = data.get("payload") or {}
    invoice_id = invoice.get("invoice_id")
    return str(invoice_id) if invoice_id is not None else None


async def close() -> None:
    if _session and not _session.closed:
        await _session.close()
