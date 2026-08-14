"""
存量卡密迁移脚本

功能：
把 xy_cards.data_content（每行一条卡密的文本块）拆分为 xy_card_secrets 明细行，
按文本卡密（content_type=0）导入。

约定（对应方案 docs/card-secret-stock-plan.md §2/§4）：
- 幂等键为 (card_id, content)：重复执行不产生重复明细
- 迁移不清空旧 data_content，保留历史兼容回退路径
- 仅迁移 type='data' 的卡券；空 data_content 不产生明细
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.card import Card
from common.models.card_secret import CardSecret


async def migrate_data_content_to_secrets(session: AsyncSession) -> int:
    """把存量 data 型卡券的 data_content 拆分为卡密明细（幂等）

    Args:
        session: 数据库会话（调用方负责事务边界，本函数只 flush 不 commit）

    Returns:
        本次新增的明细条数
    """
    cards = (
        await session.execute(
            select(Card).where(Card.type == "data", Card.data_content.isnot(None))
        )
    ).scalars().all()

    added = 0
    for card in cards:
        lines = [line.strip() for line in (card.data_content or "").split("\n") if line.strip()]
        if not lines:
            continue

        # 幂等去重：已存在的 (card_id, content) 跳过
        existing = (
            await session.execute(
                select(CardSecret.content).where(CardSecret.card_id == card.id)
            )
        ).scalars().all()
        seen = set(existing)

        for line in lines:
            if line in seen:
                continue
            seen.add(line)
            session.add(CardSecret(card_id=card.id, user_id=card.user_id, content=line))
            added += 1

    await session.flush()
    if added:
        logger.info(f"存量卡密迁移完成：新增明细 {added} 条")
    return added
