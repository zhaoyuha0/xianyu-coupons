"""
闲鱼商品重新上架服务

功能：
1. 调用闲鱼卖家后台"重新上架" mtop 接口，把已下架商品重新上架
   （与 item_offline_service 下架配套：下架 ≠ 删除，补货后可再上架）
2. 重上架后若平台重新分配 item_id，同步换绑本地商品记录与卡券关联
   （xy_catalog_items / xy_card_item_relations），保证后续订单仍能匹配到卡密分类
3. 基建与 item_offline_service 一致：_m_h5_tk 签名、Set-Cookie 合并回库、
   令牌过期刷新重试一次

⚠ RELIST_API 接口名与"重上架后 item_id 是否变化"需先抓包验证（方案 §3.5 技术前置），
   当前为占位值，抓包结论落地后校准。

对应方案：docs/card-secret-stock-plan.md §3.5
"""
from __future__ import annotations

import json
import time
from typing import Optional

import aiohttp
from loguru import logger
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.card_item_relation import CardItemRelation
from common.models.xy_account import XYAccount
from common.models.xy_catalog_item import XYCatalogItem
from common.utils.cookie_refresh import (
    extract_cookies_from_response,
    is_token_expired_error,
    merge_cookies,
    update_account_cookies_in_db,
)
from common.utils.xianyu_utils import generate_sign, trans_cookies

# ⚠ 抓包校准：卖家后台"重新上架" mtop 接口名（当前为占位，方案 §3.5 方案 A 验证后替换）
RELIST_API = "mtop.alibaba.idle.seller.pc.item.xxx"
RELIST_URL = f"https://h5api.m.goofish.com/h5/{RELIST_API}/1.0/"

# 最大令牌过期重试次数
MAX_TOKEN_RETRY = 1

# 请求超时（秒）
REQUEST_TIMEOUT = 20


async def _post_form(url: str, data: dict, headers: dict) -> tuple[dict, dict]:
    """发送 mtop 表单 POST 请求

    Args:
        url: 请求地址
        data: 表单字段（含签名 token/sign 等）
        headers: 请求头（含 Cookie）

    Returns:
        (响应JSON载荷, 响应 Set-Cookie 字典)
    """
    async with aiohttp.ClientSession() as http_session:
        async with http_session.post(
            url,
            data=data,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as response:
            res_json = await response.json(content_type=None)
            set_cookies = extract_cookies_from_response(response)
            return res_json, set_cookies


def _build_relist_request(cookies_str: str, item_id: str) -> tuple[str, dict, dict]:
    """构造重新上架请求的 (url, 表单字段, 请求头)，签名逻辑对齐下架服务"""
    cookies = trans_cookies(cookies_str)
    timestamp = str(int(time.time() * 1000))

    # ⚠ 抓包校准：请求体字段（当前按下架接口同构的 itemIds 占位）
    data_val = json.dumps({"itemIds": item_id}, separators=(",", ":"))

    token = cookies.get("_m_h5_tk", "").split("_")[0] if cookies.get("_m_h5_tk") else ""
    sign = generate_sign(timestamp, token, data_val)

    data = {
        "jsv": "2.7.2",
        "appKey": "34839810",
        "t": timestamp,
        "sign": sign,
        "token": token,
        "v": "1.0",
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "needLoginPC": "true",
        "api": RELIST_API,
        "sessionOption": "AutoLoginOnly",
        "data": data_val,
    }

    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "referer": "https://seller.goofish.com/?site=COMMONPRO",
        "cookie": cookies_str.replace("\n", "").replace("\r", ""),
    }
    return RELIST_URL, data, headers


async def relist_item(
    session: AsyncSession,
    account: XYAccount,
    item_id: str,
    retry_count: int = 0,
) -> Optional[str]:
    """调用闲鱼卖家后台"重新上架"接口

    Args:
        session: 数据库会话
        account: 闲鱼账号（使用其 Cookie 鉴权）
        item_id: 要重新上架的商品ID
        retry_count: 令牌过期内部重试计数

    Returns:
        成功返回上架后的 item_id（平台重新分配时为响应 data 中的 newItemId
        字段——字段名抓包校准；未变化则为原 item_id）；失败返回 None
    """
    account_id = account.account_id
    cookies_str = account.cookie or ""

    try:
        url, data, headers = _build_relist_request(cookies_str, item_id)
        res_json, set_cookies = await _post_form(url, data, headers)

        # 响应 Set-Cookie 合并回写账号（与下架服务一致的会话维护）
        if set_cookies:
            cookies_str = merge_cookies(cookies_str, set_cookies)
            await update_account_cookies_in_db(account_id, cookies_str)
            logger.info(f"【{account_id}】重新上架API已合并 {len(set_cookies)} 个Set-Cookie字段并更新到数据库")

        ret = res_json.get("ret", [])
        ret_str = str(ret)

        # 成功：ret 任一项以 SUCCESS 开头
        if any(str(r).startswith("SUCCESS") for r in ret):
            new_item_id = (res_json.get("data") or {}).get("newItemId") or item_id
            if new_item_id != item_id:
                logger.info(f"【{account_id}】商品 {item_id} 重新上架成功，平台分配新 item_id: {new_item_id}")
            else:
                logger.info(f"【{account_id}】商品 {item_id} 重新上架成功")
            return str(new_item_id)

        # 令牌过期：刷新 Cookie 后重试一次
        if is_token_expired_error(ret) and retry_count < MAX_TOKEN_RETRY:
            logger.info(f"【{account_id}】重新上架令牌过期，准备重试({retry_count + 1})")
            account.cookie = cookies_str
            return await relist_item(session, account, item_id, retry_count + 1)

        # 其他业务失败（频控/风控等）
        logger.warning(f"【{account_id}】商品 {item_id} 重新上架失败: {ret_str}")
        return None

    except aiohttp.ClientError as e:
        logger.warning(f"【{account_id}】商品 {item_id} 重新上架网络失败: {e}")
        return None
    except Exception as e:  # noqa: BLE001
        logger.error(f"【{account_id}】商品 {item_id} 重新上架异常: {e}")
        return None


async def remap_item_id(
    session: AsyncSession,
    old_item_id: str,
    new_item_id: str,
) -> None:
    """item_id 换绑：同步更新本地商品记录与卡券关联（只 flush 不 commit，由调用方控制事务边界）

    重上架后平台重新分配 item_id 时调用，保证后续订单仍能匹配到卡密分类。

    Args:
        session: 数据库会话
        old_item_id: 旧商品ID
        new_item_id: 新商品ID
    """
    await session.execute(
        update(XYCatalogItem)
        .where(XYCatalogItem.item_id == old_item_id)
        .values(item_id=new_item_id)
    )
    await session.execute(
        update(CardItemRelation)
        .where(CardItemRelation.item_id == old_item_id)
        .values(item_id=new_item_id)
    )
    await session.flush()
    logger.info(f"item_id 换绑完成（同事务，待提交）: {old_item_id} -> {new_item_id}")


async def relist_and_remap(
    session: AsyncSession,
    account: XYAccount,
    item_id: str,
) -> Optional[str]:
    """换绑联动入口：重新上架成功且平台分配新 item_id 时，同事务完成换绑

    Args:
        session: 数据库会话（调用方负责事务边界，本函数只 flush 不 commit）
        account: 闲鱼账号
        item_id: 要重新上架的商品ID

    Returns:
        成功返回上架后的 item_id（可能为新分配ID）；失败返回 None（不换绑）
    """
    new_item_id = await relist_item(session, account, item_id)
    if new_item_id is None:
        return None
    if new_item_id != item_id:
        await remap_item_id(session, item_id, new_item_id)
    return new_item_id
