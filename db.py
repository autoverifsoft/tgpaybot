"""Хранилище заказов.

Одна таблица. Единственная её задача — помнить, какие счета уже оплачены
и выданы, чтобы повторный вебхук не отправил товар дважды.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    Numeric,
    String,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import config


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    item_id: Mapped[str] = mapped_column(String(32))
    item_title: Mapped[str] = mapped_column(String(128))
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    # pending → paid
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


if config.DATABASE_URL.startswith("sqlite") and ":///" in config.DATABASE_URL:
    _dir = os.path.dirname(config.DATABASE_URL.split(":///", 1)[1])
    if _dir:
        os.makedirs(_dir, exist_ok=True)

_connect_args: dict = {}
if config.IS_POSTGRES and config.PG_REQUIRE_SSL:
    # Бесплатные Postgres (Neon, Render) требуют TLS
    _connect_args["ssl"] = True

engine = create_async_engine(
    config.DATABASE_URL,
    pool_pre_ping=config.IS_POSTGRES,
    connect_args=_connect_args,
)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def create_order(
    invoice_id: str,
    user_id: int,
    username: str | None,
    item_id: str,
    item_title: str,
    amount: float,
) -> None:
    async with Session() as session:
        session.add(
            Order(
                invoice_id=str(invoice_id),
                user_id=user_id,
                username=username,
                item_id=item_id,
                item_title=item_title,
                amount=amount,
            )
        )
        await session.commit()


async def mark_paid(invoice_id: str) -> Order | None:
    """Пометить счёт оплаченным.

    Возвращает заказ ТОЛЬКО если это первая отметка. При повторном
    вызове (дубль вебхука) вернёт None — и товар не уйдёт второй раз.
    Гонку закрывает условие status == 'pending' прямо в UPDATE.
    """
    async with Session() as session:
        result = await session.execute(
            update(Order)
            .where(Order.invoice_id == str(invoice_id), Order.status == "pending")
            .values(status="paid", paid_at=datetime.now(timezone.utc))
        )
        await session.commit()

        if not result.rowcount:
            return None

        return (
            await session.execute(
                select(Order).where(Order.invoice_id == str(invoice_id))
            )
        ).scalar_one_or_none()


async def pending_invoice_ids(limit: int = 100) -> list[str]:
    async with Session() as session:
        rows = await session.execute(
            select(Order.invoice_id)
            .where(Order.status == "pending")
            .order_by(Order.id.desc())
            .limit(limit)
        )
        return [r for r in rows.scalars()]


async def stats() -> dict:
    async with Session() as session:
        total = (
            await session.execute(
                select(func.count(Order.id)).where(Order.status == "paid")
            )
        ).scalar_one() or 0
        revenue = (
            await session.execute(
                select(func.coalesce(func.sum(Order.amount), 0)).where(
                    Order.status == "paid"
                )
            )
        ).scalar_one() or 0
        pending = (
            await session.execute(
                select(func.count(Order.id)).where(Order.status == "pending")
            )
        ).scalar_one() or 0
        return {"paid": total, "revenue": float(revenue), "pending": pending}


async def recent(limit: int = 10) -> list[Order]:
    async with Session() as session:
        rows = await session.execute(
            select(Order)
            .where(Order.status == "paid")
            .order_by(Order.id.desc())
            .limit(limit)
        )
        return list(rows.scalars())
