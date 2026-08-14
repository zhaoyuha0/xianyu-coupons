"""
卡密明细服务

功能：
1. take_one：并发安全地原子取一条可用卡密并占位（FOR UPDATE SKIP LOCKED）
2. release：发货失败回滚，卡密退回可用状态
3. add_batch / add_batch_images：文本/二维码图片卡密批量补货（含去重）
4. stock_of：可用库存统计（status=0 的记录数）
5. list_usage_records：已用卡密记录分页查询（owner_scope 隔离）

对应方案：docs/card-secret-stock-plan.md §3.1

原则：先取密占位，发货成功才算数；IM 发送失败必须 release 回滚。
"""
from __future__ import annotations

from typing import Optional

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.card import Card
from common.models.card_secret import CardSecret
from common.utils.time_utils import get_beijing_now_naive

# 卡密状态语义
STATUS_AVAILABLE = 0  # 可用
STATUS_USED = 1  # 已用
STATUS_VOID = 2  # 作废

# 卡密形态
CONTENT_TYPE_TEXT = 0  # 文本卡密
CONTENT_TYPE_IMAGE = 1  # 二维码图片卡密


async def take_one(
    session: AsyncSession,
    card_id: int,
    order_id: str,
) -> Optional[CardSecret]:
    """并发安全地取一条可用卡密并占位（同事务）

    使用 SELECT ... FOR UPDATE SKIP LOCKED，多订单同时卖出时同一张卡密
    不会被发出两次（SQLite 下 FOR UPDATE 为 no-op，并发语义由 MySQL 保证）。

    Args:
        session: 数据库会话（调用方负责事务边界）
        card_id: 卡密分类ID（xy_cards.id）
        order_id: 消费该卡密的订单号

    Returns:
        取到的卡密（status 已置 1、order_id/used_at 已写入）；库存空返回 None
    """
    stmt = (
        select(CardSecret)
        .where(CardSecret.card_id == card_id, CardSecret.status == STATUS_AVAILABLE)
        .order_by(CardSecret.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        logger.info(f"卡密分类 {card_id} 库存已空，取密失败（订单 {order_id}）")
        return None

    row.status = STATUS_USED
    row.order_id = order_id
    row.used_at = get_beijing_now_naive()
    await session.flush()
    logger.info(f"卡密分类 {card_id} 取出卡密 {row.id}（订单 {order_id}）")
    return row


async def release(session: AsyncSession, secret_id: int) -> None:
    """发货失败回滚：卡密退回可用状态，避免发失败还扣库存

    对不存在的 id 静默处理（幂等），不影响发货异常路径。

    Args:
        session: 数据库会话（调用方负责事务边界）
        secret_id: 卡密明细ID
    """
    await session.execute(
        update(CardSecret)
        .where(CardSecret.id == secret_id)
        .values(status=STATUS_AVAILABLE, order_id=None, used_at=None)
    )
    await session.flush()
    logger.info(f"卡密 {secret_id} 已回滚为可用状态")


def _split_secret_lines(content: str) -> list[str]:
    """把多行文本拆分为卡密列表：逐行 strip、过滤空行。"""
    return [line.strip() for line in (content or "").split("\n") if line.strip()]


async def add_batch(
    session: AsyncSession,
    card_id: int,
    user_id: int,
    content: str,
) -> int:
    """文本卡密批量补货：按行拆分入库，与存量/批次内重复的行跳过

    Args:
        session: 数据库会话（调用方负责事务边界）
        card_id: 卡密分类ID
        user_id: 操作用户ID（owner_scope 归属）
        content: 多行文本，每行一条卡密

    Returns:
        实际新增条数
    """
    lines = _split_secret_lines(content)
    if not lines:
        return 0

    # 存量内容去重（不分状态，避免同一卡密再次流入库存）
    existing = (
        await session.execute(
            select(CardSecret.content).where(CardSecret.card_id == card_id)
        )
    ).scalars().all()
    seen = set(existing)

    added = 0
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        session.add(CardSecret(card_id=card_id, user_id=user_id, content=line))
        added += 1

    await session.flush()
    logger.info(f"卡密分类 {card_id} 文本补货完成：新增 {added} 条，跳过 {len(lines) - added} 条")
    return added


async def add_batch_images(
    session: AsyncSession,
    card_id: int,
    user_id: int,
    images: list[tuple[str, str]],
) -> dict:
    """批量导入二维码图片卡密：同分类内按 (图片URL, image_hash) 去重（含批次内重复）

    接口层负责落盘与算哈希，本函数只负责去重与落库：
    每条建 content_type=1、status=0 的明细，content 存图片相对URL。
    去重键为 (content, image_hash) 二元组：同一图片重复上传（同 URL 同哈希）跳过。

    Args:
        session: 数据库会话（调用方负责事务边界）
        card_id: 卡密分类ID
        user_id: 操作用户ID（owner_scope 归属）
        images: (图片相对URL, 图片字节MD5) 二元组列表

    Returns:
        {"added": 新增条数, "skipped": 与存量或批次内重复的条数}
    """
    if not images:
        return {"added": 0, "skipped": 0}

    existing = await session.execute(
        select(CardSecret.content, CardSecret.image_hash).where(
            CardSecret.card_id == card_id,
            CardSecret.content_type == CONTENT_TYPE_IMAGE,
        )
    )
    seen = {(row[0], row[1]) for row in existing.all()}

    added = 0
    skipped = 0
    for url, image_hash in images:
        key = (url, image_hash)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        session.add(CardSecret(
            card_id=card_id,
            user_id=user_id,
            content=url,
            content_type=CONTENT_TYPE_IMAGE,
            image_hash=image_hash,
        ))
        added += 1

    await session.flush()
    logger.info(f"卡密分类 {card_id} 图片补货完成：新增 {added} 张，跳过 {skipped} 张")
    return {"added": added, "skipped": skipped}


async def stock_of(session: AsyncSession, card_id: int) -> int:
    """可用库存统计：仅 status=0 的记录数（与卡密形态无关）"""
    count = await session.scalar(
        select(func.count())
        .select_from(CardSecret)
        .where(CardSecret.card_id == card_id, CardSecret.status == STATUS_AVAILABLE)
    )
    return int(count or 0)


async def list_usage_records(
    session: AsyncSession,
    card_id: int,
    user_id: int,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[CardSecret], int]:
    """已用卡密记录分页查询（按 used_at 倒序，owner_scope 隔离）

    Args:
        session: 数据库会话
        card_id: 卡密分类ID
        user_id: 操作用户ID；分类不属于该用户时返回 ([], 0)
        page: 页码（从1开始）
        page_size: 每页数量

    Returns:
        (记录列表, 总条数)
    """
    # owner_scope 隔离：分类必须属于当前用户
    card = await session.scalar(
        select(Card.id).where(Card.id == card_id, Card.user_id == user_id)
    )
    if card is None:
        return [], 0

    base = select(CardSecret).where(
        CardSecret.card_id == card_id,
        CardSecret.status == STATUS_USED,
    )
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)

    page = max(page, 1)
    page_size = max(page_size, 1)
    stmt = (
        base.order_by(CardSecret.used_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = list((await session.execute(stmt)).scalars().all())
    return items, total
