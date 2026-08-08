# 卡密库存自动上下架方案

> 目标：卡密按"分类"管理（一分类多条卡密）；商品绑定一个卡密分类，售出自动发一条卡密并重新上架；分类库存空时自动下架商品。
>
> 前置阅读：`sell-coupons.md`（商品/卡券模块现有结构）。

## 1. 现状与复用

| 能力 | 现状 | 结论 |
|---|---|---|
| 卡密池 | `xy_cards.type='data'`，`data_content` 每行一条卡密 | 只是文本块，无单条状态，需规范化为明细表 |
| 原子取卡密 | `CardService.consume_batch_data`（CAS）、`card_delivery_content.consume_batch_data`（行锁） | 思路复用，迁移到新表 |
| 商品↔卡券绑定 | `xy_card_item_relations` + `CardMatcher`（规格匹配） | 直接复用，加"一商品一分类"校验 |
| 发货触发点 | websocket `auto_delivery_handler._auto_delivery`（:1745） | 在成功路径挂后置钩子 |
| 下架 | `common/services/item_offline_service.py`（mtop `batch.offline`，下架≠删除） | 直接复用 |
| **重新上架** | **无现成实现**，仅有 Playwright 发布器 `xianyu_publisher.py` | 需新建，方案最大风险点 |

## 2. 数据模型

新建卡密明细表 `common/models/card_secret.py`：

```sql
CREATE TABLE xy_card_secrets (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  card_id    BIGINT NOT NULL,           -- 关联 xy_cards.id（即"卡密分类"）
  user_id    INT NOT NULL,
  content    TEXT NOT NULL,             -- 实际卡密
  status     TINYINT NOT NULL DEFAULT 0, -- 0=可用 1=已用 2=作废
  order_id   VARCHAR(64),               -- 消费它的订单号（追溯）
  used_at    DATETIME,
  created_at DATETIME,
  INDEX idx_card_status (card_id, status)  -- 库存统计与取货共用
);
```

- 库存 = `COUNT(*) WHERE card_id=? AND status=0`
- `xy_cards.data_content` 保留做历史兼容；迁移脚本把存量行拆入新表
- 绑定约束：`CardService.update_item_card_relations` 增加校验——同一商品同一规格只能绑一张 data 型卡券（多规格商品每规格可各绑一种）；分销卡券（dock_l1/l2）不受本方案约束

## 3. 关键代码逻辑

### 3.1 原子取卡密 —— `common/services/card_secret_service.py`（新建）

```python
async def take_one(session, card_id: int, order_id: str) -> CardSecret | None:
    """并发安全地取一条可用卡密并占位（同事务）"""
    stmt = (
        select(CardSecret)
        .where(CardSecret.card_id == card_id, CardSecret.status == 0)
        .order_by(CardSecret.id)
        .limit(1)
        .with_for_update(skip_locked=True)   # 多订单同时卖出不重发
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None                          # 库存空信号
    row.status = 1
    row.order_id = order_id
    row.used_at = get_beijing_now_naive()
    return row

async def release(session, secret_id: int):
    """发货失败回滚：退回可用状态，避免发失败还扣库存"""
    await session.execute(
        update(CardSecret)
        .where(CardSecret.id == secret_id)
        .values(status=0, order_id=None, used_at=None)
    )
```

原则：**先取密占位，发货成功才算数；IM 发送失败必须 `release` 回滚**。

### 3.2 发货链路改造 —— `websocket/app/services/xianyu/auto_delivery_handler.py`

data 型卡券取内容改为走 `card_secret_service`（替换原 `consume_batch_data` 调用），成功路径加钩子：

```python
# _auto_delivery 内，卡密 IM 发送成功后：
secret = await card_secret_service.take_one(session, card_id, order_id)
if secret is None:
    await enqueue_stock_task(action="offline", item_id=item_id, card_id=card_id)
    return                                   # 无货，不发货
try:
    await send_im_message(buyer, secret.content)
except Exception:
    await card_secret_service.release(session, secret.id)
    raise

remaining = await card_secret_service.stock_of(session, card_id)
if remaining > 0:
    await enqueue_stock_task(action="relist",  item_id=item_id)   # 售出后重上架
else:
    await enqueue_stock_task(action="offline", item_id=item_id)   # 最后一张已发，下架
    await notify_seller(f"卡密分类【{card.name}】库存已空，商品已下架")
```

### 3.3 下架 —— 复用现有服务（零改动）

```python
from common.services.item_offline_service import batch_offline_items_from_xianyu
await batch_offline_items_from_xianyu(session, account, [item_id])
# 下架 ≠ 删除：商品仍在卖家后台，补货后可再上架；不改本地商品库
```

### 3.4 重新上架 —— `common/services/item_relist_service.py`（新建）

仿照 `item_offline_service.py` 的基建（mtop 签名 `_m_h5_tk`、Set-Cookie 合并回库、令牌过期重试）：

```python
RELIST_API = "mtop.alibaba.idle.seller.pc.item.xxx"   # ⚠ 需先抓包验证（方案第一步）

async def relist_item(session, account, item_id: str) -> bool:
    """调闲鱼卖家后台"重新上架"接口；若平台重新发布后 item_id 变化，
    需同步更新 xy_catalog_items 与 xy_card_item_relations 的 item_id（换绑）"""
```

- **方案 A（优先）**：逆向卖家后台"重新上架" mtop 接口 —— 开工第一件事，抓包确认接口名、参数、重上架后 item_id 是否变化
- **方案 B（兜底）**：复用 Playwright 发布基建 `xianyu_publisher.py` 走"重新发布"自动化，慢但确定可行

### 3.5 异步任务与巡检（双保险）

websocket 只投递任务，worker 执行上下架，失败指数退避重试、超阈值告警：

```
Redis 队列 stock_tasks: {action: relist|offline, item_id, card_id, account_id, retry}
```

scheduler 新增 `stock_guard_task.py`（每 5 分钟），防钩子漏单：

```python
async def execute():
    # 1) 库存=0 但仍绑定在售商品的分类 → 补下架
    # 2) 开关开启时：库存>0 且商品已下架的分类 → 补货后自动重新上架
    empty_cards = await query_empty_stock_cards_with_online_items()
    for card, item_id, account in empty_cards:
        await batch_offline_items_from_xianyu(session, account, [item_id])
```

## 4. 改动清单

| 位置 | 改动 |
|---|---|
| `common/models/card_secret.py` | 新表模型 + 注册 `common/models/__init__.py` |
| `common/services/card_secret_service.py` | 新建：`take_one` / `release` / `add_batch`（补货）/ `stock_of` / 使用记录 |
| `common/services/card_delivery_content.py` | data 型取内容改调新服务 |
| `websocket/.../auto_delivery_handler.py` | 取密/回滚/成功后投递上下架任务（见 3.2） |
| `common/services/item_relist_service.py` | 新建（方案 A 验证后） |
| `scheduler/.../stock_guard_task.py` | 新建巡检任务并注册 |
| `backend-web/.../cards.py` + `card_service.py` | 端点：库存/使用记录查询、批量导入补货；绑定时"一商品一分类"校验 |
| `frontend` | 卡券页显示库存与已用记录；绑定弹窗显示库存 |
| 迁移脚本 | 建表 + `data_content` 存量行导入 |

## 5. 边界与注意点

- **并发**：同一分类被多商品绑定、多账号同时出单时，`SKIP LOCKED` 保证一张卡密只发一次
- **多数量发货**：沿用现逻辑——一单只取一张、重上架一次；对接卡券（dock_l1/l2）不走本地库存
- **item_id 换绑**：若重新发布后闲鱼分配新 item_id，任务回调必须更新 `xy_catalog_items` 和 `xy_card_item_relations`，否则后续订单匹配不到卡券
- **可观测**：上下架任务全量落日志，失败超阈值走 notification-channels 告警，卖家可手动重试

## 6. 实施顺序

1. **抓包验证重上架 mtop 接口**（决定方案 A/B，确认 item_id 是否变化）—— 技术前置
2. 建表 + `card_secret_service` + 存量迁移
3. 发货链路改造（取密/回滚/钩子）
4. 下架闭环 + 巡检任务
5. 后台 API 与前端界面
