# 卡密库存自动上下架方案

> 目标：卡密按"分类"管理（一分类多条卡密）；商品绑定一个卡密分类，售出自动发一条卡密并重新上架；分类库存空时自动下架商品。
>
> 卡密形态：**二维码图片为主**——每张卡密就是一张二维码图片，售出后把图片发给买家；同时兼容存量纯文本卡密。
>
> 前置阅读：`sell-coupons.md`（商品/卡券模块现有结构）。

## 1. 现状与复用

| 能力 | 现状 | 结论 |
|---|---|---|
| 卡密池 | `xy_cards.type='data'`，`data_content` 每行一条卡密 | 只是文本块，无单条状态，需规范化为明细表 |
| 原子取卡密 | `CardService.consume_batch_data`（CAS）、`card_delivery_content.consume_batch_data`（行锁） | 思路复用，迁移到新表 |
| 商品↔卡券绑定 | `xy_card_item_relations` + `CardMatcher`（规格匹配） | 直接复用，加"一商品一分类"校验 |
| 发货触发点 | websocket `auto_delivery_handler._auto_delivery`（:1745） | 在成功路径挂后置钩子 |
| 图片上传 | `common/utils/local_image_upload.save_uploaded_image` + 卡券单图接口 `POST /api/cards/upload-image` | 批量导入时逐张复用 |
| 图片 IM 发送 | `send_image_msg` / `_send_image_msg_with_retry`（image 型卡券已用） | 图片卡密发货直接复用 |
| 下架 | `common/services/item_offline_service.py`（mtop `batch.offline`，下架≠删除） | 直接复用 |
| **重新上架** | **无现成实现**，仅有 Playwright 发布器 `xianyu_publisher.py` | 需新建，方案最大风险点 |

## 2. 数据模型

新建卡密明细表 `common/models/card_secret.py`：

```sql
CREATE TABLE xy_card_secrets (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  card_id    BIGINT NOT NULL,           -- 关联 xy_cards.id（即"卡密分类"）
  user_id    INT NOT NULL,
  content    TEXT NOT NULL,             -- 文本卡密存原文；图片卡密存相对URL（/static/uploads/card_secrets/xx.png）
  content_type TINYINT NOT NULL DEFAULT 0, -- 0=文本 1=二维码图片
  image_hash CHAR(32),                  -- 图片字节 MD5，导入去重用；文本卡密为 NULL
  status     TINYINT NOT NULL DEFAULT 0, -- 0=可用 1=已用 2=作废
  order_id   VARCHAR(64),               -- 消费它的订单号（追溯）
  used_at    DATETIME,
  created_at DATETIME,
  INDEX idx_card_status (card_id, status), -- 库存统计与取货共用
  INDEX idx_card_hash (card_id, image_hash) -- 图片导入去重
);
```

- 库存 = `COUNT(*) WHERE card_id=? AND status=0`（与卡密形态无关）
- `content_type` 决定发货走文本消息还是图片消息；新字段由启动时字段补齐自动加列（`common/db/init_database.py`），存量文本卡密补 `content_type=0`
- `xy_cards.data_content` 保留做历史兼容；迁移脚本把存量行拆入新表（按文本卡密导入）
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

图片卡密补货（接口层负责落盘与算哈希，服务层只负责去重与落库）：

```python
async def add_batch_images(
    session, card_id: int, user_id: int,
    images: list[tuple[str, str]],   # (图片相对URL, 图片字节MD5)
) -> dict:
    """批量导入二维码图片卡密：同分类内按 image_hash 去重（含批次内重复），
    每条建 content_type=1、status=0 的明细，content 存图片相对URL。
    返回 {"added": n, "skipped": m}（skipped=与存量或批次内重复的条数）"""
```

### 3.2 发货链路改造 —— `websocket/app/services/xianyu/auto_delivery_handler.py`

data 型卡券取内容改为走 `card_secret_service`（替换原 `consume_batch_data` 调用），成功路径加钩子。取出的卡密按 `content_type` 分发：文本走文本消息，二维码图片走现有图片消息链路：

```python
# _auto_delivery 内，卡密 IM 发送成功后：
secret = await card_secret_service.take_one(session, card_id, order_id)
if secret is None:
    await enqueue_stock_task(action="offline", item_id=item_id, card_id=card_id)
    return                                   # 无货，不发货
try:
    if secret.content_type == 1:
        # 二维码图片：复用 image 型卡券同款发送链路（先传闲鱼 CDN 再发图片消息）
        await _send_image_msg_with_retry(ws, chat_id, buyer_id, secret.content)
    else:
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

### 3.3 批量导入卡密图片接口 —— `backend-web/app/api/routes/cards.py`

针对某一卡密分类，一次上传多张二维码图片完成补货：

```
POST /api/cards/{card_id}/secrets/batch-images
Content-Type: multipart/form-data
字段：files: 多个图片文件（jpg/png/webp 等）
```

- 鉴权：`get_current_active_user` + owner_scope 校验（分类必须属于当前用户），仅 `type='data'` 的卡券可导入
- 限制：单批最多 50 张；单张 ≤ 5MB；`content_type` 必须 `image/*`（复用 `save_uploaded_image` 的类型/大小/扩展名安全校验）
- 落盘：逐张调用 `save_uploaded_image` 存到 `backend-web/static/uploads/card_secrets/`，文件名前缀 `card_{card_id}`，`content` 存相对 URL `/static/uploads/card_secrets/{filename}`
- 去重：对每张图片字节算 MD5 作为 `image_hash`，连同 URL 交给 `card_secret_service.add_batch_images` 统一去重落库（重复图片跳过、不重复落盘）
- 响应：统一格式 `{"success": true, "data": {"added": n, "skipped": m}}`；某张校验失败时整体回滚（已落盘文件删除）并返回中文错误原因
- 补货后商品已下架的，由巡检任务（§3.6）发现"库存>0 且已下架"自动重新上架，无需接口内处理

### 3.4 下架 —— 复用现有服务（零改动）

```python
from common.services.item_offline_service import batch_offline_items_from_xianyu
await batch_offline_items_from_xianyu(session, account, [item_id])
# 下架 ≠ 删除：商品仍在卖家后台，补货后可再上架；不改本地商品库
```

### 3.5 重新上架 —— `common/services/item_relist_service.py`（新建）

仿照 `item_offline_service.py` 的基建（mtop 签名 `_m_h5_tk`、Set-Cookie 合并回库、令牌过期重试）：

```python
RELIST_API = "mtop.alibaba.idle.seller.pc.item.xxx"   # ⚠ 需先抓包验证（方案第一步）

async def relist_item(session, account, item_id: str) -> bool:
    """调闲鱼卖家后台"重新上架"接口；若平台重新发布后 item_id 变化，
    需同步更新 xy_catalog_items 与 xy_card_item_relations 的 item_id（换绑）"""
```

- **方案 A（优先）**：逆向卖家后台"重新上架" mtop 接口 —— 开工第一件事，抓包确认接口名、参数、重上架后 item_id 是否变化
- **方案 B（兜底）**：复用 Playwright 发布基建 `xianyu_publisher.py` 走"重新发布"自动化，慢但确定可行

### 3.6 异步任务与巡检（双保险）

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
| `common/services/card_secret_service.py` | 新建：`take_one` / `release` / `add_batch`（文本补货）/ `add_batch_images`（图片补货）/ `stock_of` / 使用记录 |
| `common/services/card_delivery_content.py` | data 型取内容改调新服务：`take_one` 替换 `consume_batch_data`；取到图片卡密（content_type=1）时把图片 URL 作为文本返回（提货为纯文本场景，与 image 型卡券提货行为一致）；库存空返回 None |
| `websocket/.../auto_delivery_handler.py` | 取密/回滚/成功后投递上下架任务；图片卡密走图片消息链路（见 3.2） |
| `common/services/item_relist_service.py` | 新建（方案 A 验证后） |
| `scheduler/.../stock_guard_task.py` | 新建巡检任务并注册 |
| `backend-web/.../cards.py` + `card_service.py` | 端点：库存/使用记录查询、批量导入补货、`POST /{card_id}/secrets/batch-images`（批量导入卡密图片，见 3.3）；绑定时"一商品一分类"校验 |
| `frontend` | 卡券页显示库存与已用记录（图片卡密显示缩略图）、多图批量上传补货入口；绑定弹窗显示库存 |
| 迁移脚本 | 建表 + `data_content` 存量行导入（按文本卡密） |

## 5. 边界与注意点

- **并发**：同一分类被多商品绑定、多账号同时出单时，`SKIP LOCKED` 保证一张卡密只发一次
- **多数量发货**：沿用现逻辑——一单只取一张、重上架一次；对接卡券（dock_l1/l2）不走本地库存
- **图片去重**：同一张二维码图片重复上传（含同批次内重复）按 `image_hash` 跳过，避免同一二维码被发两次
- **图片文件生命周期**：作废/已用卡密的图片文件保留在磁盘供追溯与后台查看，不做定时清理（量大了再议）
- **item_id 换绑**：若重新发布后闲鱼分配新 item_id，任务回调必须更新 `xy_catalog_items` 和 `xy_card_item_relations`，否则后续订单匹配不到卡券
- **可观测**：上下架任务全量落日志，失败超阈值走 notification-channels 告警，卖家可手动重试

## 6. 实施顺序

1. **抓包验证重上架 mtop 接口**（决定方案 A/B，确认 item_id 是否变化）—— 技术前置
2. 建表（含 `content_type`/`image_hash`）+ `card_secret_service` + 存量迁移
3. 发货链路改造（取密/回滚/钩子，图片卡密走图片消息）
4. 批量导入卡密图片接口（§3.3）+ `add_batch_images`
5. 下架闭环 + 巡检任务
6. 后台其余 API 与前端界面（库存/记录展示、多图上传入口）
