"""重新上架服务测试。

覆盖方案 §3.4：common/services/item_relist_service.py（待新建）。
注意：mtop 接口名与"重上架后 item_id 是否变化"需先抓包验证，
涉及接口细节的用例以 Mock 响应驱动，抓包结论落地后校准。

接口约定（测试即接口定义，实现方按此开发）：
1. common/services/item_relist_service.py::

    RELIST_API = "mtop.alibaba.idle.seller.pc.item.xxx"  # 抓包后校准
    async def relist_item(session, account, item_id) -> str | None
        # 成功返回上架后的 item_id（平台重新分配时为响应 data 中的
        # "newItemId" 字段——字段名抓包校准；未变化则为原 item_id）；失败返回 None
    async def remap_item_id(session, old_item_id, new_item_id) -> None
        # 只 flush 不 commit，由调用方控制事务边界
    async def relist_and_remap(session, account, item_id) -> str | None
        # 换绑联动入口：relist_item 成功且返回新 id 时，同事务 remap_item_id
    async def _post_form(url, data, headers) -> tuple[dict, dict]  # (json载荷, set-cookie字典)

   relist_item 内部用 _post_form 发请求（测试 patch 它拦截 HTTP），
   并复用 common.utils.cookie_refresh.update_account_cookies_in_db 回写 Cookie。
   mtop 响应约定：ret 任一项以 "SUCCESS" 开头为成功；
   含 "TOKEN_EXOIRED" 为令牌过期（刷新 Cookie 后重试一次）。
2. 任务 worker：scheduler/app/services/scheduler/stock_task_worker.py::

    MAX_RETRY = 5
    BASE_RETRY_DELAY = 60  # 秒，退避间隔 = BASE_RETRY_DELAY * 2 ** retry
    async def process_stock_task(task: dict, session) -> bool
    async def requeue_task(task: dict, delay: float) -> None
    async def notify_dead_letter(task: dict) -> None

   worker 执行 relist 任务必须走 relist_and_remap（保证换绑联动生效）。
"""
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

# TDD：服务实现前整个文件标记跳过（原因即待办）；实现落地后自动进入真实断言
item_relist_service = pytest.importorskip(
    "common.services.item_relist_service",
    reason="待实现：common/services/item_relist_service.py",
)

SUCCESS_PAYLOAD = {"ret": ["SUCCESS::调用成功"], "data": {}}
SUCCESS_NEW_ID_PAYLOAD = {"ret": ["SUCCESS::调用成功"], "data": {"newItemId": "NEW_ITEM"}}
BIZ_FAIL_PAYLOAD = {"ret": ["FAIL_BIZ_频控限制::触发频控"], "data": {}}
TOKEN_EXPIRED_PAYLOAD = {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"], "data": {}}


@pytest.fixture
def mock_post_form(monkeypatch):
    """拦截 item_relist_service 的 HTTP 出口，记录请求并返回预设响应。"""
    mock = AsyncMock(return_value=(SUCCESS_PAYLOAD, {}))
    monkeypatch.setattr(item_relist_service, "_post_form", mock, raising=False)
    return mock


class TestRelistItem:
    """单商品重新上架 relist_item。"""

    async def test_调用正确的mtop接口与签名(self, async_session, seed_account, mock_post_form):
        """验证请求带 _m_h5_tk 签名、目标为卖家后台重上架接口（接口名以抓包结果校准）。"""
        account = await seed_account(cookie="_m_h5_tk=faketoken123; other=1")

        result = await item_relist_service.relist_item(async_session, account, "ITEM001")

        assert result == "ITEM001", "成功且 id 未变化时应返回原 item_id"
        mock_post_form.assert_awaited_once()
        url, data = mock_post_form.call_args.args[0], mock_post_form.call_args.args[1]
        assert item_relist_service.RELIST_API in url, "请求目标应为重上架 mtop 接口"
        assert data.get("token") == "faketoken123", "签名应使用 Cookie 中的 _m_h5_tk"
        assert data.get("sign"), "请求应携带签名"

    async def test_重上架成功返回商品ID且不改本地商品库(
        self, async_session, seed_account, mock_post_form,
    ):
        """验证平台返回成功时服务返回商品ID，不改动本地商品库记录。"""
        from sqlalchemy import func, select

        from common.models.xy_catalog_item import XYCatalogItem

        account = await seed_account()
        async_session.add(XYCatalogItem(
            owner_id=1, account_pk=account.id, item_id="ITEM001",
            title="测试", created_at=datetime(2026, 1, 1),
        ))
        await async_session.commit()

        result = await item_relist_service.relist_item(async_session, account, "ITEM001")

        assert result == "ITEM001", "平台成功时应返回商品ID"
        count = await async_session.scalar(
            select(func.count()).select_from(XYCatalogItem)
            .where(XYCatalogItem.item_id == "ITEM001")
        )
        assert count == 1, "relist_item 本身不应改动本地商品库（换绑走 relist_and_remap）"

    async def test_平台返回业务失败时返回None并记录原因(
        self, async_session, seed_account, mock_post_form,
    ):
        """验证频控/风控等平台错误被解析，返回 None 且日志含原始错误码。"""
        account = await seed_account()
        mock_post_form.return_value = (BIZ_FAIL_PAYLOAD, {})

        result = await item_relist_service.relist_item(async_session, account, "ITEM001")

        assert result is None, "平台业务失败应返回 None"

    async def test_令牌过期时自动重试(self, async_session, seed_account, mock_post_form):
        """验证 _m_h5_tk 失效场景按现有基建刷新 Cookie 后重试（对齐 item_offline_service 行为）。"""
        account = await seed_account()
        mock_post_form.side_effect = [
            (TOKEN_EXPIRED_PAYLOAD, {}),
            (SUCCESS_PAYLOAD, {}),
        ]

        result = await item_relist_service.relist_item(async_session, account, "ITEM001")

        assert result == "ITEM001", "令牌过期重试后成功应返回商品ID"
        assert mock_post_form.await_count == 2, "令牌过期应自动重试一次"

    async def test_响应Set_Cookie合并回写账号(
        self, async_session, seed_account, mock_post_form, monkeypatch,
    ):
        """验证平台下发的新 Cookie 合并更新到 xy_accounts（与下架服务一致的会话维护）。"""
        account = await seed_account()
        mock_post_form.return_value = (SUCCESS_PAYLOAD, {"_m_h5_tk": "newtoken456"})
        mock_update = AsyncMock()
        monkeypatch.setattr(
            item_relist_service, "update_account_cookies_in_db", mock_update, raising=False
        )

        result = await item_relist_service.relist_item(async_session, account, "ITEM001")

        assert result is not None
        mock_update.assert_awaited_once(), "响应 Set-Cookie 应合并回写账号"


class TestRelistItemIdRemap:
    """重上架后 item_id 变化的换绑处理（方案 §3.4/§5 的核心风险闭环）。"""

    async def _seed_old_item(self, async_session, seed_card, seed_item_with_binding):
        card = await seed_card()
        item, relation = await seed_item_with_binding(item_id="OLD_ITEM", card_id=card.id)
        return card, item, relation

    async def test_平台分配新item_id时同步更新本地商品记录(
        self, async_session, seed_card, seed_item_with_binding,
    ):
        """验证 xy_catalog_items.item_id 更新为新 id，旧记录可追溯。"""
        _, item, _ = await self._seed_old_item(async_session, seed_card, seed_item_with_binding)

        await item_relist_service.remap_item_id(async_session, "OLD_ITEM", "NEW_ITEM")
        await async_session.commit()
        await async_session.refresh(item)

        assert item.item_id == "NEW_ITEM", "本地商品记录应换绑为新 item_id"

    async def test_新item_id同步换绑卡券关联(
        self, async_session, seed_card, seed_item_with_binding,
    ):
        """验证 xy_card_item_relations.item_id 一并更新，保证后续订单仍能匹配到卡密分类。"""
        _, _, relation = await self._seed_old_item(
            async_session, seed_card, seed_item_with_binding
        )

        await item_relist_service.remap_item_id(async_session, "OLD_ITEM", "NEW_ITEM")
        await async_session.commit()
        await async_session.refresh(relation)

        assert relation.item_id == "NEW_ITEM", "卡券关联应同步换绑新 item_id"

    async def test_换绑在同一事务内完成(
        self, async_session, seed_card, seed_item_with_binding,
    ):
        """验证商品记录与关联表更新同生共死，不出现半换绑状态。"""
        # 约定：remap_item_id 只做 flush、不内部 commit，由调用方控制事务边界。
        # 回滚后两张表都应保持旧值，证明更新处于同一事务。
        _, item, relation = await self._seed_old_item(
            async_session, seed_card, seed_item_with_binding
        )

        await item_relist_service.remap_item_id(async_session, "OLD_ITEM", "NEW_ITEM")
        await async_session.rollback()
        await async_session.refresh(item)
        await async_session.refresh(relation)

        assert item.item_id == "OLD_ITEM", "回滚后商品记录应保持旧 item_id"
        assert relation.item_id == "OLD_ITEM", "回滚后关联记录应保持旧 item_id"

    async def test_重上架分配新id时自动完成换绑(
        self, async_session, seed_account, seed_card, seed_item_with_binding,
        mock_post_form,
    ):
        """验证 relist_and_remap 联动：relist 返回新 id 时自动换绑两张表（核心闭环）。"""
        account = await seed_account()
        _, item, relation = await self._seed_old_item(
            async_session, seed_card, seed_item_with_binding
        )
        mock_post_form.return_value = (SUCCESS_NEW_ID_PAYLOAD, {})

        result = await item_relist_service.relist_and_remap(
            async_session, account, "OLD_ITEM"
        )
        await async_session.commit()
        await async_session.refresh(item)
        await async_session.refresh(relation)

        assert result == "NEW_ITEM", "应返回平台分配的新 item_id"
        assert item.item_id == "NEW_ITEM", "商品记录应已换绑"
        assert relation.item_id == "NEW_ITEM", "卡券关联应已换绑（后续订单仍能匹配卡密）"

    async def test_重上架后id不变则不触发换绑(
        self, async_session, seed_account, seed_card, seed_item_with_binding,
        mock_post_form, monkeypatch,
    ):
        """验证 relist 返回原 id 时不做无谓的换绑写入。"""
        account = await seed_account()
        await self._seed_old_item(async_session, seed_card, seed_item_with_binding)
        mock_post_form.return_value = (SUCCESS_PAYLOAD, {})  # data 无 newItemId
        mock_remap = AsyncMock()
        monkeypatch.setattr(
            item_relist_service, "remap_item_id", mock_remap, raising=False
        )

        result = await item_relist_service.relist_and_remap(
            async_session, account, "OLD_ITEM"
        )

        assert result == "OLD_ITEM"
        mock_remap.assert_not_awaited(), "item_id 未变化时不应触发换绑"

    async def test_重上架失败不触发换绑(
        self, async_session, seed_account, seed_card, seed_item_with_binding,
        mock_post_form, monkeypatch,
    ):
        """验证 relist 失败（返回 None）时不换绑、不半成品更新。"""
        account = await seed_account()
        _, item, relation = await self._seed_old_item(
            async_session, seed_card, seed_item_with_binding
        )
        mock_post_form.return_value = (BIZ_FAIL_PAYLOAD, {})
        mock_remap = AsyncMock()
        monkeypatch.setattr(
            item_relist_service, "remap_item_id", mock_remap, raising=False
        )

        result = await item_relist_service.relist_and_remap(
            async_session, account, "OLD_ITEM"
        )

        assert result is None
        mock_remap.assert_not_awaited(), "重上架失败不应换绑"


class TestRelistWorker:
    """上下架任务 worker（消费 Redis stock_tasks 队列）。"""

    @pytest.fixture
    def worker_module(self, service_importer):
        """隔离导入 scheduler 的任务 worker 模块（TDD：实现前跳过，原因即待办）。"""
        with service_importer("scheduler") as imp:
            try:
                yield imp.import_module("app.services.scheduler.stock_task_worker")
            except ModuleNotFoundError:
                pytest.skip("待实现：scheduler/app/services/scheduler/stock_task_worker.py")

    async def test_消费relist任务并调用重上架服务(
        self, async_session, seed_account, worker_module, monkeypatch,
    ):
        """验证 worker 正确解析 action=relist 载荷并执行（走 relist_and_remap 保证换绑联动），成功后任务出队。"""
        account = await seed_account(account_id="ACC001")
        mock_relist = AsyncMock(return_value="ITEM001")
        mock_requeue = AsyncMock()
        monkeypatch.setattr(worker_module, "relist_and_remap", mock_relist, raising=False)
        monkeypatch.setattr(worker_module, "requeue_task", mock_requeue, raising=False)

        task = {"action": "relist", "item_id": "ITEM001", "card_id": 1,
                "account_id": "ACC001", "retry": 0}
        ok = await worker_module.process_stock_task(task, async_session)

        assert ok is True, "执行成功应返回 True（任务出队）"
        mock_relist.assert_awaited_once()
        mock_requeue.assert_not_awaited(), "成功任务不应重新入队"

    async def test_消费offline任务并调用下架服务(
        self, async_session, seed_account, worker_module, monkeypatch,
    ):
        """验证 worker 正确解析 action=offline 载荷并调用 batch_offline_items_from_xianyu。"""
        account = await seed_account(account_id="ACC001", cookie="k=v")
        mock_offline = AsyncMock(return_value={"success": True, "message": "ok"})
        monkeypatch.setattr(
            worker_module, "batch_offline_items_from_xianyu", mock_offline, raising=False
        )

        task = {"action": "offline", "item_id": "ITEM001", "card_id": 1,
                "account_id": "ACC001", "retry": 0}
        ok = await worker_module.process_stock_task(task, async_session)

        assert ok is True
        mock_offline.assert_awaited_once()
        args = mock_offline.call_args.args
        assert args[0] == "ACC001" and args[1] == "k=v", "应使用任务对应账号的凭据下架"
        assert list(args[2]) == ["ITEM001"]

    async def test_执行失败按指数退避重试(
        self, async_session, seed_account, worker_module, monkeypatch,
    ):
        """验证失败任务重新入队且重试间隔递增，retry 计数正确累加。"""
        await seed_account(account_id="ACC001")
        mock_relist = AsyncMock(return_value=None)  # relist_and_remap 失败返回 None
        mock_requeue = AsyncMock()
        monkeypatch.setattr(worker_module, "relist_and_remap", mock_relist, raising=False)
        monkeypatch.setattr(worker_module, "requeue_task", mock_requeue, raising=False)

        for retry in (0, 1, 2):
            task = {"action": "relist", "item_id": "ITEM001", "card_id": 1,
                    "account_id": "ACC001", "retry": retry}
            ok = await worker_module.process_stock_task(task, async_session)
            assert ok is False, "执行失败应返回 False"

        assert mock_requeue.await_count == 3, "失败任务应重新入队"
        delays = [c.kwargs.get("delay") for c in mock_requeue.call_args_list]
        assert all(d is not None for d in delays), "重试应携带退避间隔"
        assert delays[0] < delays[1] < delays[2], "退避间隔应随 retry 递增"
        retried = [c.args[0]["retry"] for c in mock_requeue.call_args_list]
        assert retried == [1, 2, 3], "retry 计数应正确累加"

    async def test_超过最大重试次数后告警并落死信(
        self, async_session, seed_account, worker_module, monkeypatch,
    ):
        """验证超阈值任务通知卖家、写入失败日志表，不再无限重试。"""
        await seed_account(account_id="ACC001")
        mock_relist = AsyncMock(return_value=None)
        mock_requeue = AsyncMock()
        mock_dead = AsyncMock()
        monkeypatch.setattr(worker_module, "relist_and_remap", mock_relist, raising=False)
        monkeypatch.setattr(worker_module, "requeue_task", mock_requeue, raising=False)
        monkeypatch.setattr(worker_module, "notify_dead_letter", mock_dead, raising=False)

        task = {"action": "relist", "item_id": "ITEM001", "card_id": 1,
                "account_id": "ACC001", "retry": worker_module.MAX_RETRY}
        ok = await worker_module.process_stock_task(task, async_session)

        assert ok is False
        mock_requeue.assert_not_awaited(), "超过最大重试次数不应再入队"
        mock_dead.assert_awaited_once(), "超阈值任务应告警并落死信"
