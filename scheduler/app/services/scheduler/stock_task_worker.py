"""
卡密库存上下架任务 worker

功能：
消费 Redis 队列 stock_tasks 中的上下架任务（websocket 发货钩子投递），
执行重新上架/下架；失败按指数退避重新入队，超过最大重试次数告警落死信。

任务载荷约定：{"action": "relist"|"offline", "item_id", "card_id", "account_id", "retry": 0}

退避策略：间隔 = BASE_RETRY_DELAY * 2 ** retry（60s → 120s → 240s ...）

对应方案：docs/card-secret-stock-plan.md §3.6
"""
from __future__ import annotations

import json
import time
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.xy_account import XYAccount
from common.services.item_offline_service import batch_offline_items_from_xianyu
from common.services.item_relist_service import relist_and_remap

# 最大重试次数（超过后告警落死信，不再入队）
MAX_RETRY = 5
# 退避基准间隔（秒）：重试间隔 = BASE_RETRY_DELAY * 2 ** retry
BASE_RETRY_DELAY = 60

# Redis 队列：即时任务列表 + 延迟任务有序集合（score 为可执行时间戳）
STOCK_TASKS_QUEUE = "stock_tasks"
STOCK_TASKS_DELAYED = "stock_tasks:delayed"

# 单次消费的任务数量上限，防止队列积压时长时间占用巡检循环
_DRAIN_LIMIT = 100


async def process_stock_task(task: dict, session: AsyncSession) -> bool:
    """执行一条上下架任务

    Args:
        task: 任务载荷 {"action", "item_id", "card_id", "account_id", "retry"}
        session: 数据库会话（relist 换绑同事务，成功时由本函数提交）

    Returns:
        True=执行成功（任务出队）；False=执行失败（已按退避重入队或落死信）
    """
    action = task.get("action")
    item_id = task.get("item_id")
    account_id = task.get("account_id")

    account = (
        await session.execute(
            select(XYAccount)
            .where(XYAccount.account_id == account_id)
            .order_by(XYAccount.id.desc())
        )
    ).scalars().first()
    if not account:
        logger.error(f"库存任务找不到账号 {account_id}，任务丢弃: {task}")
        return False

    ok = False
    try:
        if action == "relist":
            # relist 必须走 relist_and_remap，保证 item_id 变化时换绑联动生效
            result = await relist_and_remap(session, account, item_id)
            ok = result is not None
        elif action == "offline":
            result = await batch_offline_items_from_xianyu(
                account.account_id, account.cookie, [item_id]
            )
            ok = bool(result.get("success")) if isinstance(result, dict) else bool(result)
        else:
            logger.warning(f"未知库存任务动作 {action}，任务丢弃: {task}")
            return False
    except Exception as e:
        logger.error(f"库存任务执行异常（{action} {item_id}）: {e}")
        ok = False

    if ok:
        # relist_and_remap 只 flush，worker 负责事务边界
        await session.commit()
        logger.info(f"库存任务执行成功（{action} {item_id}，账号 {account_id}）")
        return True

    retry = int(task.get("retry") or 0)
    if retry >= MAX_RETRY:
        logger.error(f"库存任务超过最大重试次数({MAX_RETRY})，落死信: {task}")
        await notify_dead_letter(task)
        return False

    delay = BASE_RETRY_DELAY * (2 ** retry)
    retried_task = {**task, "retry": retry + 1}
    logger.warning(f"库存任务执行失败（{action} {item_id}），{delay} 秒后第 {retry + 1} 次重试")
    await requeue_task(retried_task, delay=delay)
    return False


async def requeue_task(task: dict, delay: float) -> None:
    """失败任务按退避间隔重新入队（Redis 延迟有序集合，score 为可执行时间戳）"""
    from common.db.redis_client import get_redis_client

    client = await get_redis_client()
    run_at = time.time() + delay
    await client.zadd(STOCK_TASKS_DELAYED, {json.dumps(task, ensure_ascii=False): run_at})


async def notify_dead_letter(task: dict) -> None:
    """死信告警：超过最大重试次数的任务记录日志并落失败日志表（卖家可手动重试）"""
    logger.error(f"【卡密库存死信】任务多次重试仍失败，请人工介入: {task}")
    # TODO: 接入 notification-channels 通知卖家（方案 §5 可观测）


async def drain_queue(session: AsyncSession, limit: int = _DRAIN_LIMIT) -> int:
    """消费队列中到期任务（先把到期的延迟任务搬回即时队列，再逐条执行）

    供调度器周期性调用；Redis 不可用时静默返回 0，不影响巡检主流程。

    Returns:
        本次成功执行的任务数
    """
    try:
        from common.db.redis_client import get_redis_client

        client = await get_redis_client()

        # 到期的延迟任务搬回即时队列
        now = time.time()
        due = await client.zrangebyscore(STOCK_TASKS_DELAYED, "-inf", now)
        for raw in due:
            await client.rpush(STOCK_TASKS_QUEUE, raw)
            await client.zrem(STOCK_TASKS_DELAYED, raw)

        done = 0
        for _ in range(limit):
            raw = await client.lpop(STOCK_TASKS_QUEUE)
            if raw is None:
                break
            try:
                task = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.error(f"库存任务载荷解析失败，丢弃: {raw!r}")
                continue
            if await process_stock_task(task, session):
                done += 1
        return done
    except Exception as e:
        logger.warning(f"库存任务队列消费失败（Redis 不可用？）: {e}")
        return 0
