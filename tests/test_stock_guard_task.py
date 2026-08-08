"""库存巡检任务测试。

覆盖方案 §3.5：scheduler/app/services/scheduler/stock_guard_task.py（待新建）。
定位：发货钩子的双保险——钩子失败/进程崩溃时兜底下架，补货后联动上架。

接口约定（测试即接口定义，实现方按此开发）：

    async def execute(session, relist_enabled: bool = False) -> dict
        # 返回 {"offline": 成功下架数, "relist": 成功上架数, "failed": 失败数}

    # 模块级可替换依赖（测试用 monkeypatch 替换）：
    async def is_item_online(session, item_id) -> bool   # 商品在售判定
    batch_offline_items_from_xianyu  # 来自 common.services.item_offline_service
    relist_item                      # 来自 common.services.item_relist_service

巡检范围约定：type=data 的自有（source=own）绑定；分销（dock_l1/l2）不参与。
下架使用商品所属账号（xy_catalog_items.account_pk → xy_accounts）的凭据。
"""
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def guard_module(service_importer):
    """隔离导入 scheduler 的巡检任务模块（TDD：实现前跳过，原因即待办）。"""
    with service_importer("scheduler") as imp:
        try:
            yield imp.import_module("app.services.scheduler.stock_guard_task")
        except ModuleNotFoundError:
            pytest.skip("待实现：scheduler/app/services/scheduler/stock_guard_task.py")


@pytest.fixture
def mock_platform(guard_module, monkeypatch):
    """替换巡检任务的平台依赖：下架/上架接口与在售判定。"""
    mocks = type("Mocks", (), {})()
    mocks.offline = AsyncMock(return_value={"success": True, "message": "ok"})
    mocks.relist = AsyncMock(return_value=True)
    mocks.online_item_ids = set()  # 由测试设置"在售"的商品集合
    monkeypatch.setattr(
        guard_module, "batch_offline_items_from_xianyu", mocks.offline, raising=False
    )
    monkeypatch.setattr(guard_module, "relist_item", mocks.relist, raising=False)

    async def _is_online(session, item_id):
        return item_id in mocks.online_item_ids

    monkeypatch.setattr(guard_module, "is_item_online", _is_online, raising=False)
    return mocks


class TestStockGuardOfflineSweep:
    """空库存补下架扫描。"""

    async def test_库存为0且商品仍在售的分类触发补下架(
        self, async_session, seed_account, seed_card, seed_item_with_binding,
        guard_module, mock_platform,
    ):
        """验证扫描命中"status=0 库存=0 且绑定商品在售"的记录并调用下架服务。"""
        account = await seed_account(account_id="ACC001", cookie="k=v")
        card = await seed_card()  # 空库存
        await seed_item_with_binding(
            item_id="ITEM001", card_id=card.id, account_pk=account.id
        )
        mock_platform.online_item_ids.add("ITEM001")

        summary = await guard_module.execute(async_session)

        mock_platform.offline.assert_awaited_once()
        args = mock_platform.offline.call_args.args
        assert list(args[2]) == ["ITEM001"], "应对空库存在售商品调用下架"
        assert summary["offline"] == 1 and summary["failed"] == 0

    async def test_库存大于0的商品不被误下架(
        self, async_session, seed_account, seed_card, seed_secrets,
        seed_item_with_binding, guard_module, mock_platform,
    ):
        """验证有库存的分类不会进入下架名单（防误伤核心用例）。"""
        account = await seed_account(account_id="ACC001")
        card = await seed_card()
        await seed_secrets(card.id, n=2)  # 有库存
        await seed_item_with_binding(
            item_id="ITEM001", card_id=card.id, account_pk=account.id
        )
        mock_platform.online_item_ids.add("ITEM001")

        summary = await guard_module.execute(async_session)

        mock_platform.offline.assert_not_awaited(), "有库存商品不应被下架"
        assert summary["offline"] == 0

    async def test_已下架商品不重复触发下架(
        self, async_session, seed_account, seed_card, seed_item_with_binding,
        guard_module, mock_platform,
    ):
        """验证本地/平台已下架的商品被跳过，任务幂等可反复执行。"""
        account = await seed_account(account_id="ACC001")
        card = await seed_card()  # 空库存
        await seed_item_with_binding(
            item_id="ITEM001", card_id=card.id, account_pk=account.id
        )
        # online_item_ids 为空：商品已下架

        summary = await guard_module.execute(async_session)

        mock_platform.offline.assert_not_awaited(), "已下架商品不应重复触发下架"
        assert summary["offline"] == 0

    async def test_分销卡券分类不参与巡检(
        self, async_session, seed_account, seed_card, seed_item_with_binding,
        guard_module, mock_platform,
    ):
        """验证 dock_l1/dock_l2 来源的绑定被排除在库存巡检之外。"""
        account = await seed_account(account_id="ACC001")
        card = await seed_card()  # 空库存
        await seed_item_with_binding(
            item_id="ITEM001", card_id=card.id, account_pk=account.id,
            source="dock_l1", dock_record_id=100,
        )
        mock_platform.online_item_ids.add("ITEM001")

        summary = await guard_module.execute(async_session)

        mock_platform.offline.assert_not_awaited(), "分销绑定不应参与库存巡检"
        assert summary["offline"] == 0

    async def test_多账号场景按各自账号Cookie执行下架(
        self, async_session, seed_account, seed_card, seed_item_with_binding,
        guard_module, mock_platform,
    ):
        """验证跨账号商品使用对应账号的凭据调用平台接口，不串号。"""
        acc_a = await seed_account(account_id="ACC_A", cookie="cookie_a=1")
        acc_b = await seed_account(account_id="ACC_B", cookie="cookie_b=2")
        card_a = await seed_card(name="分类A")
        card_b = await seed_card(name="分类B")
        await seed_item_with_binding(
            item_id="ITEM_A", card_id=card_a.id, account_pk=acc_a.id
        )
        await seed_item_with_binding(
            item_id="ITEM_B", card_id=card_b.id, account_pk=acc_b.id
        )
        mock_platform.online_item_ids.update({"ITEM_A", "ITEM_B"})

        summary = await guard_module.execute(async_session)

        assert mock_platform.offline.await_count == 2
        creds = {c.args[0]: c.args[1] for c in mock_platform.offline.call_args_list}
        assert creds == {"ACC_A": "cookie_a=1", "ACC_B": "cookie_b=2"}, (
            "各账号商品必须使用各自账号的 Cookie，不串号"
        )
        assert summary["offline"] == 2


class TestStockGuardRestockRelist:
    """补货联动上架（可选开关）。"""

    async def test_开关开启时补货后自动重新上架(
        self, async_session, seed_account, seed_card, seed_secrets,
        seed_item_with_binding, guard_module, mock_platform,
    ):
        """验证开关打开且分类库存从 0 变为 >0 时，绑定商品被重新上架。"""
        account = await seed_account(account_id="ACC001")
        card = await seed_card()
        await seed_secrets(card.id, n=3)  # 已补货
        await seed_item_with_binding(
            item_id="ITEM001", card_id=card.id, account_pk=account.id
        )
        # online_item_ids 为空：商品当前处于下架状态

        summary = await guard_module.execute(async_session, relist_enabled=True)

        mock_platform.relist.assert_awaited_once(), "补货后应自动重新上架"
        assert summary["relist"] == 1
        mock_platform.offline.assert_not_awaited()

    async def test_开关关闭时补货不触发上架(
        self, async_session, seed_account, seed_card, seed_secrets,
        seed_item_with_binding, guard_module, mock_platform,
    ):
        """验证开关关闭时巡检只做下架兜底，不做任何上架动作。"""
        account = await seed_account(account_id="ACC001")
        card = await seed_card()
        await seed_secrets(card.id, n=3)
        await seed_item_with_binding(
            item_id="ITEM001", card_id=card.id, account_pk=account.id
        )

        summary = await guard_module.execute(async_session, relist_enabled=False)

        mock_platform.relist.assert_not_awaited(), "开关关闭时不应做任何上架动作"
        assert summary["relist"] == 0


class TestStockGuardRobustness:
    """任务健壮性。"""

    async def test_单个商品处理失败不影响其它商品(
        self, async_session, seed_account, seed_card, seed_item_with_binding,
        guard_module, mock_platform,
    ):
        """验证循环内异常被捕获记录，剩余商品继续处理。"""
        account = await seed_account(account_id="ACC001")
        card_a = await seed_card(name="分类A")
        card_b = await seed_card(name="分类B")
        await seed_item_with_binding(
            item_id="ITEM_A", card_id=card_a.id, account_pk=account.id
        )
        await seed_item_with_binding(
            item_id="ITEM_B", card_id=card_b.id, account_pk=account.id
        )
        mock_platform.online_item_ids.update({"ITEM_A", "ITEM_B"})
        mock_platform.offline.side_effect = [
            Exception("网络抖动"),
            {"success": True, "message": "ok"},
        ]

        summary = await guard_module.execute(async_session)

        assert mock_platform.offline.await_count == 2, "单个失败不应中断后续商品处理"
        assert summary["failed"] == 1 and summary["offline"] == 1

    async def test_任务执行结果落日志(
        self, async_session, seed_account, seed_card, seed_item_with_binding,
        guard_module, mock_platform,
    ):
        """验证每轮扫描的命中数、下架数、失败数写入日志/日志表。"""
        account = await seed_account(account_id="ACC001")
        card = await seed_card()
        await seed_item_with_binding(
            item_id="ITEM001", card_id=card.id, account_pk=account.id
        )
        mock_platform.online_item_ids.add("ITEM001")

        summary = await guard_module.execute(async_session)

        # 约定：execute 返回本轮统计摘要（由实现方写入日志/日志表）
        assert set(summary) >= {"offline", "relist", "failed"}, (
            "执行摘要应包含下架/上架/失败计数"
        )
        assert summary["offline"] == 1
        assert summary["relist"] == 0
        assert summary["failed"] == 0

    async def test_任务配置可从调度器加载与停用(self, service_importer):
        """验证 stock_guard_task 注册进 scheduler 任务体系，支持配置开关与执行间隔。"""
        with service_importer("scheduler") as imp:
            task_service = imp.import_module("app.services.scheduled_task_service")

        assert hasattr(task_service, "TASK_CODE_STOCK_GUARD"), (
            "应定义 TASK_CODE_STOCK_GUARD 任务代码常量"
        )
        assert task_service.TASK_CODE_STOCK_GUARD in task_service.DEFAULT_CONFIGS, (
            "stock_guard 应注册进 DEFAULT_CONFIGS（含 interval_seconds 与 enabled 开关）"
        )
        config = task_service.DEFAULT_CONFIGS[task_service.TASK_CODE_STOCK_GUARD]
        assert config["interval_seconds"] > 0 and "enabled" in config
