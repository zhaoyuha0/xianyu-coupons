# 商品管理与卡券管理模块文档

> 本文档基于 `backend-web`（FastAPI 后端）、`common`（公共代码）、`websocket`（消息/自动发货服务）的源码分析生成，描述商品管理与卡券管理两个业务域的代码结构、接口、数据模型与调用关系。

## 1. 总体分层

```
前端 (frontend/src)
  │
  ▼
路由层    backend-web/app/api/routes/     items.py / cards.py / card_dock.py / product_publish.py / ...
  │        依赖注入: backend-web/app/api/deps.py
  ▼
服务层    backend-web/app/services/       card_service.py / default_reply_service.py / card_dock_service.py / ...
          common/services/                item_service.py / card_matcher.py / card_delivery_content.py / ...
  ▼
数据层    common/models/                  xy_catalog_items / xy_cards / xy_card_item_relations / xy_dock_records / ...
          (SQLAlchemy AsyncSession → MySQL；Redis 用于分布式锁与限流)
  ▼
外部系统  闲鱼 mtop API（h5api.m.goofish.com）、外部发卡平台（codefree，CARD_DOCK_BASE_URL）
```

横切关注点：

- **鉴权**：所有路由经 `deps.get_current_active_user`；数据归属经 `common/utils/auth_scope.py` 的 `resolve_owner_scope` 计算 owner 范围（管理员可看全部）。
- **商品 ↔ 卡券**：通过关联表 `xy_card_item_relations` 多对多关联，统一入口为 `common/services/card_matcher.py` 的 `CardMatcher`。

---

## 2. 商品管理模块

### 2.1 模块组成

| 文件 | 职责 |
|---|---|
| `backend-web/app/api/routes/items.py` | 核心路由（prefix=`/items`，26 个端点） |
| `backend-web/app/api/routes/product_publish.py` | 商品发布（素材库、单品/批量发布、发布日志） |
| `backend-web/app/api/routes/search.py` | 商品搜索（Playwright） |
| `backend-web/app/api/routes/personal_addresses.py` | 个人发布地址库 |
| `backend-web/app/api/routes/publish_addresses.py` | 发布随机地址池（管理员维护） |
| `common/services/item_service.py` | `ItemService`：商品 CRUD + 闲鱼抓取入库（真正实现） |
| `backend-web/app/services/item_service.py` | 薄封装，re-export common 层实现 |
| `backend-web/app/services/default_reply_service.py` | `DefaultReplyService`：默认回复 CRUD |
| `backend-web/app/services/selectable_item_service.py` | `SelectableItemService`：卡券选品弹窗轻量查询 |
| `common/services/item_offline_service.py` | 闲鱼 mtop 批量下架 |
| `common/services/item_delete_service.py` | 闲鱼 mtop 删除商品 |
| `common/utils/item_info_manager.py` | `ItemInfoManager`：闲鱼商品列表抓取（HTTP mtop） |
| `backend-web/app/services/search/` | Playwright 商品搜索（含滑块验证处理） |

### 2.2 路由表（`/items`，items.py）

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `""` | 商品全量列表 |
| GET | `/paginated` | 分页 + 多条件筛选（关键词/擦亮/多规格/多数量发货） |
| GET | `/selectable/all` | 卡券选品弹窗：全量轻量项 |
| GET | `/by-card/{card_id}` | 卡券已关联商品轻量详情 |
| GET | `/cookie/{cookie_id}` | 按账号取商品列表 |
| GET/PUT/DELETE | `/{cookie_id}/{item_id}/default-reply` | 商品默认回复的查/存/删（PUT 含 API 类型 SSRF 校验） |
| POST | `/{cookie_id}/{item_id}/default-reply/upload-image` | 上传回复图片 |
| POST | `/{cookie_id}/batch-default-reply` / `batch-delete-default-reply` / `batch-default-reply/upload-image` | 默认回复批量操作 |
| GET/PUT | `/{cookie_id}/{item_id}/ai-prompt` | AI 提示词查/存 |
| POST | `/{cookie_id}/batch-ai-prompt` / `batch-delete-ai-prompt` | AI 提示词批量操作 |
| GET | `/{cookie_id}/{item_id}` | 商品详情 |
| PUT | `/{cookie_id}/{item_id}` | 通用更新（标题/价格/metadata 等） |
| PUT | `/{cookie_id}/{item_id}/multi-spec` | 开关多规格（写 `metadata_json.is_multi_spec`） |
| PUT | `/{cookie_id}/{item_id}/multi-quantity-delivery` | 开关多数量发货（写 `metadata_json.multi_quantity_delivery`） |
| DELETE | `/delete`、`/{cookie_id}/{item_id}`、`/batch` | 删除（统一/按账号/批量，smart 版兼容孤儿商品） |
| POST | `/batch-offline` | 闲鱼平台批量下架（不改本地库） |
| POST | `/batch-delete-xianyu` | 闲鱼平台批量删除（校验本地归属，不改本地库） |
| POST | `/get-by-page` | 从闲鱼抓单页商品入库 |
| POST | `/get-all-from-account` | 全量抓取入库（单账号/全账号） |
| POST | `/search` | Playwright 搜索闲鱼商品 |

公共批量逻辑收敛在 `_execute_batch_item_operation`（`items.py:31`）。请求/响应 schema 主要在 `common/schemas/item.py`，其余 Pydantic 模型内联于 items.py。

### 2.3 商品发布相关路由（概述）

- `product_publish.py`（prefix=`/product-publish`）：素材库 CRUD（`/materials*`，`ProductMaterialService`）；单品发布 `/publish/single` 与批量发布 `/publish/batch`（Playwright 自动化，后台任务 `_run_batch_publish_background`，状态经 `PublishBatchStatusService` 查询）；发布日志 `/logs`；图片上传 `/upload/images`。
- `search.py`（prefix=`/search`）：`POST /search/items`，Playwright 搜索（支持滑块验证）。
- `personal_addresses.py`：个人发布地址库，分页/增改/批量删/Excel 导入导出，每用户仅管自己的数据。
- `publish_addresses.py`：发布随机地址池，管理员维护、普通用户只读。

### 2.4 核心服务

**`ItemService`**（`common/services/item_service.py:30`）

- `list_items` / `list_items_paginated`：列表查询；`is_polished` 为直接字段，`is_multi_spec` / `multi_quantity_delivery` 存于 `metadata_json`；附带批量装配默认回复状态（`_get_default_reply_status_batch`）与卡券状态（`_get_card_status_batch`，走 `CardMatcher`）。
- `fetch_items_page_from_account` / `fetch_all_items_from_account`：从闲鱼抓取入库。后者用 Redis 分布式锁 `item_sync:{account_id}` 防与定时任务并发（Redis 不可用时靠唯一约束兜底）；整页已存在且无变更时提前停止翻页。
- `save_fetched_items` / `_save_single_item` / `_apply_single_item`：逐商品独立提交，`IntegrityError` 重试转更新，单条失败不影响其它。
- `update_item`：通用更新；title/price/ai_prompt 直接落列，其余字段（含前端 `item_` 前缀映射）写入 `metadata_json` 并 `flag_modified`。
- `delete_item` / `delete_item_smart`：删除并级联清理卡券关联（`CardMatcher.delete_relations_by_item_id`）；smart 版处理"账号已删除的孤儿商品"，返回 ok / not_found / account_required 三态。

**`DefaultReplyService`**（`backend-web/app/services/default_reply_service.py:12`）：账号级（item_id 为 NULL）与商品级默认回复的 get/save/delete；`DefaultReplyRecord` 记录已回复用户（支撑 `reply_once`）。保存时由路由侧做 `validate_api_url`（防 SSRF）与超时归一化。

**闲鱼平台读写（HTTP mtop，不经过本项目的 websocket 服务）**

- 读：`ItemInfoManager` → mtop `mtop.idle.web.xyh.item.list` 分页拉取卖家商品列表。
- 写：`item_offline_service.batch_offline_items_from_xianyu` / `item_delete_service.batch_delete_items_from_xianyu` → h5api.m.goofish.com（`_m_h5_tk` 签名，Set-Cookie 合并回库，令牌过期自动重试/后台恢复登录）。
- 搜索：`backend-web/app/services/search/searcher.py` `ItemSearchService`（Playwright）。

**关键边界**：本地库的写入只发生在抓取入库路径；`batch-offline` / `batch-delete-xianyu` 只操作闲鱼平台、不改本地库。

### 2.5 商品数据模型

| 表 | 模型 | 关键字段 |
|---|---|---|
| `xy_catalog_items` | `common/models/xy_catalog_item.py:20` `XYCatalogItem` | `owner_id`、`account_pk`（列名 account_id → xy_accounts.id）、`item_id`、`title`、`price`、`ai_prompt`、`is_polished`、`metadata_json`（列名 metadata，存 description/detail/category/is_multi_spec/multi_quantity_delivery）；索引 `idx_cat_account_item`、`idx_cat_owner_created` |
| `xy_default_replies` | `common/models/default_reply.py:13` `DefaultReply` | `account_id`、`item_id`（NULL=账号级）、`enabled`、`reply_type`(text/api)、`reply_content`、`reply_image`、`api_url`、`api_timeout`、`reply_once` |
| `xy_default_reply_records` | 同文件 :35 `DefaultReplyRecord` | account_id + item_id + user_id 已回复记录 |

---

## 3. 卡券管理模块

### 3.1 模块组成

| 层 | 文件 | 职责 |
|---|---|---|
| 路由 | `backend-web/app/api/routes/cards.py` | 自有卡券 CRUD 与商品关联（prefix=`/cards`） |
| 路由 | `backend-web/app/api/routes/card_dock.py` | 分销卡券：对接外部发卡平台（prefix=`/card-dock`） |
| 路由 | `backend-web/app/api/routes/user_settings.py` | 对接卡密秘钥的创建入口（card_dock 前置配置） |
| 服务 | `backend-web/app/services/card_service.py` | `CardService`：卡券主服务 |
| 服务 | `backend-web/app/services/selectable_card_service.py` | 关联弹窗双源（自有+对接）合并分页 |
| 服务 | `backend-web/app/services/card_dock_service.py` | `CardDockService`：上游平台 HTTP 代理 |
| 服务 | `backend-web/app/services/card_secret_key_service.py` | 对接秘钥创建（调外部密钥管理服务） |
| 服务 | `backend-web/app/services/pickup_service.py` | 免登录提货（提货秘钥 + dock_record_id） |
| 服务 | `backend-web/app/services/dock_record_service.py` / `sub_dock_record_service.py` | 分销对接记录 CRUD、一/二级分销 |
| 公共服务 | `common/services/card_matcher.py` | `CardMatcher`：统一卡券匹配器，被 backend-web / websocket / scheduler 三端共用 |
| 公共服务 | `common/services/card_delivery_content.py` | 提货场景取内容（text/data/api/image） |
| 公共服务 | `common/services/delivery_utils.py` | 发货内容变量替换渲染 |

### 3.2 路由表（`/cards`，cards.py）

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `""` | 卡券分页列表（搜索/类型过滤/lite 轻量模式） |
| GET | `/item/{item_id}` | 查指定商品的卡券列表 |
| GET | `/selectable` | 关联弹窗：自有 + 对接卡券合并分页 |
| GET | `/selectable/all` | 可选卡券全量轻量键列表（供全选） |
| POST | `""` | 创建卡券（多规格/图片数/手续费校验，`_validate_card_payload`） |
| GET/PUT/DELETE | `/{card_id}` | 卡券详情/更新/删除 |
| POST | `/upload-image` | 上传卡券图片到 `static/uploads/cards` |
| POST | `/batch-delete` | 批量删除 |
| GET/PUT | `/{card_id}/items` | 查/重置卡券 → 商品关联（先删后插） |
| POST | `/batch-bind` | 批量绑定卡券到商品（关联表，不复制卡券） |
| POST | `/batch-save` | 创建一张卡券并绑定到多个商品 |
| DELETE | `/relation/{card_id}/{item_id}` | 删除单条关联 |
| PUT | `/item/{item_id}/cards` | 重置商品 → 卡券关联（含 source/dock_record_id） |
| POST | `/batch-clear-item-relations` | 批量清空商品关联（不删卡券） |

### 3.3 分销卡券（`/card-dock`，card_dock.py）

**业务定位**：非卡密导入，而是对接外部发卡平台（上游卡券系统 codefree）的实时代理。后端从当前用户个人设置读取「对接卡密秘钥」（`distribution.card_secret_key`），以 `api_key` 透传调用上游 `{CARD_DOCK_BASE_URL}/api/card-product/agent/*`。所有数据实时获取、不缓存、不落库；统一返回 `{success, code, message, data}`，业务错误也以 HTTP 200 返回。

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/sources` | 卡券商下拉列表 |
| GET | `/goods` / `/goods/{goods_id}` / `/goods/{goods_id}/stock` | 货源商品列表/详情/各规格库存 |
| POST | `/purchase` | 提货（扣上游余额返卡密），内存限流 5 次/60 秒 |
| GET | `/purchase-url` | 生成含 api_key 的直提 URL（"复制提货 api"） |

`CardDockService`（`card_dock_service.py:51`）：秘钥读取带 5 分钟 TTL 缓存；专用 HTTPClient 超时 60s 且 `max_retries=1`——提货是非幂等扣款操作，禁用重试防重复扣款。

**前置配置**：`POST /user-settings/card-secret-key/create`（user_settings.py:143）一键创建对接秘钥——向外部密钥管理服务申请 key，写入当前用户 `distribution.card_secret_key` 设置并失效缓存。未配置秘钥时分销卡券所有接口返回"尚未配置对接卡密秘钥"。

### 3.4 核心服务

**`CardService`**（`card_service.py:18`）

- 查询：`get_cards_paginated`、`get_dockable_cards_paginated`（货源广场）、`get_card_by_id`、`get_cards_by_item_id(_and_spec)`。
- CRUD：`create_card` / `update_card` / `delete_card` / `batch_delete_cards` / `check_card_duplicate`。
- 关联操作（均委托 `CardMatcher`）：`batch_save_and_bind`、`batch_bind_cards_to_items`、`update_card_item_relations`、`update_item_card_relations`、`get_card_item_ids`。
- 发货辅助：`consume_batch_data`（CAS 乐观锁原子消费 data 卡密首行）、`increment_delivery_count`。
- 注意：`get_available_card` / `mark_card_used` / `get_card_content` / `_call_card_api`（约 :851-1018）**无调用方，属遗留代码**。

**`CardMatcher`**（`common/services/card_matcher.py:29`，三端统一匹配器）

- `get_cards_by_item_id`：先查关联表（带 source/dock_record_id），无数据回退旧字段 `xy_cards.item_id`（向后兼容），再做规格匹配（完全匹配 > 通用卡券）与按 id 去重（own 优先）。
- 关联表增删：`update_card_item_relations`、`update_item_card_relations`、`batch_bind_cards_to_items`、级联删除。

**`card_delivery_content.py`**（提货/发货取内容）

- `consume_batch_data`：行锁 `FOR UPDATE` 版本（与 CardService 的 CAS 版并存，供提货场景在分布式锁 + 同事务下使用）。
- `get_api_card_content`：API 型卡券拉取，最多重试 4 次。
- `build_delivery_content`：按 text / data / api / image 类型生成纯文本发货内容。

**`delivery_utils.py`**：`{DELIVERY_CONTENT}` 及订单上下文变量替换渲染。

**`PickupService`**（`pickup_service.py:53`）：免登录提货；Redis 限流 5s/次 + 分布式锁；按对接价格经 `SettlementService` 分润结算，虚拟订单写入 `xy_agent_orders`。

**`DockRecordService` / `SubDockRecordService`**：分销对接记录 CRUD、加价、一/二级分销、上下级状态级联。

### 3.5 卡券数据模型

| 表 | 模型文件 | 关键字段 |
|---|---|---|
| `xy_cards` | `common/models/card.py:14` | `user_id`、`item_id`（旧单商品关联字段，现以关联表为准）、`name`、`type`(api/text/data/image)、`enabled`、`delay_seconds`、`delivery_count`、`price`、`is_dockable`、`fee_payer`(distributor/dealer)、`min_price`、`dock_visibility`(public/dealer_only)、`is_multi_spec`/`spec_name`/`spec_value`、`api_config`(JSON)、`text_content`(LONGTEXT)、`data_content`(LONGTEXT 卡密池，每行一条)、`image_url`/`image_urls` |
| `xy_card_item_relations` | `common/models/card_item_relation.py:20` | `user_id`、`card_id`、`item_id`、`source`(own/dock_l1/dock_l2)、`dock_record_id`（0=自有）——商品 ↔ 卡券多对多关联 |
| `xy_dock_records` | `common/models/dock_record.py:18` | `user_id`、`card_id`、`dock_name`、`markup_amount`、`delivery_count`、`status`、`owner_disabled`、`level`(1/2 级分销)、`parent_dock_id`、`source_user_id`、`allow_sub_dock`、`sub_dock_price`、`sub_dock_visibility` |
| `xy_dock_code_bindings` | `common/models/dock_code_binding.py:18` | user_id ↔ target_user_id 对接码绑定，控制 dealer_only 卡券可见性 |
| `xy_user_settings` | `common/models/user_setting.py` | key=`distribution.card_secret_key` 存对接秘钥；`balance` 存余额 |
| `xy_agent_orders` | `common/models/agent_order.py` | 提货/对接发货的分润代理订单 |

### 3.6 自动发货链路（卡券的消费端）

```
订单触发 (websocket 服务 auto_delivery_handler.py:_auto_delivery, :1745)
  → db_manager.get_cards_by_item_id（经 common/db/compat.py 委托 CardMatcher，带规格匹配）
  → 按 card_source 区分 own / dock_l1 / dock_l2
  → 取内容（text/data/image/api，经 card_delivery_content）
  → IM 发送 → delivery_count 累加
  → 对接卡券：多数量退化为 1 张、过发货校验、走 SettlementService 分润结算
```

其它入口：

- 手动/定时发货：`websocket/app/api/routes/internal.py`，同样查 `CardItemRelation` 得 card_source/dock_record_id，支持 card_only、send_before_confirm、multi_quantity 等模式。
- 定时补发：`scheduler` 的 `redelivery_task.py` 也用 `CardMatcher` 做卡券匹配。
- 商品删除时：`ItemService` 委托 `CardMatcher` 级联清理关联。

---

## 4. 商品 ↔ 卡券关联关系

```
xy_catalog_items (商品)                xy_cards (卡券)
        │                                   │
        └────► xy_card_item_relations ◄────┘
                (user_id, card_id, item_id,
                 source=own/dock_l1/dock_l2,
                 dock_record_id)
                          │
                          ▼
                   xy_dock_records (分销对接记录，一/二级分销)
```

- 关联统一由 `CardMatcher` 维护；`xy_cards.item_id` 为历史字段，仅作回退兼容。
- `own`：自有卡券；`dock_l1` / `dock_l2`：一级/二级分销对接卡券（对应 `xy_dock_records`）。
- 前端选品/选券弹窗分别由 `SelectableItemService`（`/items/selectable/all`、`/items/by-card/{card_id}`）和 `SelectableCardService`（`/cards/selectable*`）提供轻量数据。

## 5. 已知问题与备注

- `items.py` 中 `DefaultReplyService` 注解无显式 import（靠 `from __future__ import annotations` 延迟求值，运行时正常，静态检查会报未定义）。
- `items.py` 的 `batch_delete_items` 内部 `from loguru import logger` 覆盖模块顶部 stdlib logger（items.py:805），局部不一致。
- 并发消费卡密有两套实现：backend-web `CardService.consume_batch_data`（CAS 乐观锁）与 common `card_delivery_content.consume_batch_data`（行锁 FOR UPDATE，供提货场景）。
- `CardService` 尾部 `get_available_card` 等 4 个方法为无调用方的遗留代码。
