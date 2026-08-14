"""
卡密库存巡检任务（发货钩子的双保险）

功能：
1. 空库存补下架：库存=0 但仍绑定在售商品的卡密分类 → 调用下架服务兜底
   （防发货钩子漏单/进程崩溃导致的"无货还在卖"）
2. 补货联动上架（可选开关）：库存>0 且商品已下架的分类 → 自动重新上架
3. 顺带消费 Redis 上下架任务队列（stock_task_worker.drain_queue）

巡检范围：type=data 的自有（source=own）绑定；分销（dock_l1/l2）不参与。
下架/上架使用商品所属账号（xy_catalog_items.account_pk → xy_accounts）的凭据。

对应方案：docs/card-secret-stock-plan.md §3.6
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.card import Card
from common.models.card_item_relation import CardItemRelation
from common.models.xy_account import XYAccount
from common.models.xy_catalog_item import XYCatalogItem
from common.services import card_secret_service
from common.services.item_offline_service import batch_offline_items_from_xianyu
from common.services.item_relist_service import relist_item


async def is_item_online(session: AsyncSession, item_id: str) -> bool:
    """商品在售判定

    ⚠ 保守默认实现：本地商品库无在售状态字段，接入闲鱼商品状态查询前
    一律返回 False（宁可不补下架，绝不误下架在售商品）。
    发货钩子（库存空即投递下架任务）是下架主路径，本巡检仅作双保险。
    """
    # TODO: 接入闲鱼卖家后台商品状态查询后按平台实际在售状态判定
    return False


async def execute(session: AsyncSession, relist_enabled: bool = False) -> dict:
    """执行一轮库存巡检

    Args:
        session: 数据库会话
        relist_enabled: 补货联动上架开关；开启时"库存>0 且商品已下架"的分类自动重新上架

    Returns:
        本轮统计摘要 {"offline": 成功下架数, "relist": 成功上架数, "failed": 失败数}
    """
    summary = {"offline": 0, "relist": 0, "failed": 0}

    # 巡检范围：自有（source=own）绑定的 data 型卡密分类 + 绑定商品 + 所属账号
    stmt = (
        select(CardItemRelation, Card, XYCatalogItem, XYAccount)
        .join(Card, Card.id == CardItemRelation.card_id)
        .join(XYCatalogItem, XYCatalogItem.item_id == CardItemRelation.item_id)
        .join(XYAccount, XYAccount.id == XYCatalogItem.account_pk)
        .where(
            Card.type == "data",
            (CardItemRelation.source == "own") | (CardItemRelation.source.is_(None)),
        )
    )
    rows = (await session.execute(stmt)).all()

    # 同一商品可能被多条关联命中（历史数据），按 item_id 去重
    seen_items: set[str] = set()
    for relation, card, item, account in rows:
        if item.item_id in seen_items:
            continue
        seen_items.add(item.item_id)

        try:
            stock = await card_secret_service.stock_of(session, card.id)
            online = await is_item_online(session, item.item_id)

            if stock == 0 and online:
                # 空库存仍在售：补下架（发货钩子漏单兜底）
                logger.warning(
                    f"【库存巡检】卡密分类 {card.id}（{card.name}）库存已空但商品 "
                    f"{item.item_id} 仍在售，执行补下架"
                )
                result = await batch_offline_items_from_xianyu(
                    account.account_id, account.cookie, [item.item_id]
                )
                if isinstance(result, dict) and result.get("success"):
                    summary["offline"] += 1
                else:
                    summary["failed"] += 1
            elif stock > 0 and not online and relist_enabled:
                # 补货后商品仍下架：自动重新上架（开关开启时）
                logger.info(
                    f"【库存巡检】卡密分类 {card.id}（{card.name}）已补货（库存 {stock}），"
                    f"商品 {item.item_id} 自动重新上架"
                )
                new_item_id = await relist_item(session, account, item.item_id)
                if new_item_id:
                    summary["relist"] += 1
                else:
                    summary["failed"] += 1
        except Exception as e:
            # 单个商品处理失败不影响其它商品
            summary["failed"] += 1
            logger.error(f"【库存巡检】商品 {item.item_id} 处理失败: {e}")

    logger.info(
        f"【库存巡检】本轮完成：下架 {summary['offline']}，上架 {summary['relist']}，"
        f"失败 {summary['failed']}（扫描绑定 {len(seen_items)} 条）"
    )
    return summary


async def run(relist_enabled: bool = False) -> dict:
    """调度器入口：先消费上下架任务队列，再执行一轮巡检（独立会话）"""
    from common.db.session import async_session_maker

    async with async_session_maker() as session:
        # 1. 消费 Redis 上下架任务队列（worker）
        try:
            from app.services.scheduler import stock_task_worker

            drained = await stock_task_worker.drain_queue(session)
            if drained:
                logger.info(f"【库存巡检】本轮消费上下架任务 {drained} 条")
        except Exception as e:
            logger.warning(f"【库存巡检】任务队列消费异常（不影响巡检）: {e}")

        # 2. 库存巡检（双保险）
        return await execute(session, relist_enabled=relist_enabled)
