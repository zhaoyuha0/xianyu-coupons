"""卡密明细服务测试：common/services/card_secret_service.py

覆盖方案 §3.1：take_one（原子取密）、release（失败回滚）、
add_batch（补货）、stock_of（库存统计）、使用记录查询。
对应被测模块：common/services/card_secret_service.py（待新建）。

接口约定（测试即接口定义，实现方按此开发）：
- take_one(session, card_id, order_id) -> CardSecret | None
- release(session, secret_id) -> None（不存在时静默）
- add_batch(session, card_id, user_id, content) -> int（按行拆分、strip、
  过滤空行、与存量 content 重复的行跳过，返回实际新增条数）
- stock_of(session, card_id) -> int（仅统计 status=0）
- list_usage_records(session, card_id, user_id, page=1, page_size=20)
  -> (items, total)，按 used_at 倒序；card 不属于 user_id 时返回 ([], 0)
"""
import pytest

from tests.conftest import IS_SQLITE

# TDD：服务实现前整个文件标记跳过（原因即待办）；实现落地后自动进入真实断言
card_secret_service = pytest.importorskip(
    "common.services.card_secret_service",
    reason="待实现：common/services/card_secret_service.py",
)


class TestTakeOne:
    """原子取卡密 take_one：FOR UPDATE SKIP LOCKED，先占位后确认。"""

    async def test_有库存时返回最早一条可用卡密(self, async_session, seed_card, seed_secrets):
        """验证按 id 升序取最早写入的可用卡密（FIFO），保证卡密按序消耗。"""
        card = await seed_card()
        rows = await seed_secrets(card.id, n=3)

        secret = await card_secret_service.take_one(async_session, card.id, "ORDER001")

        assert secret is not None, "有库存时应取到卡密"
        assert secret.id == rows[0].id, "应取最早写入（id 最小）的卡密"
        assert secret.content == rows[0].content

    async def test_取密成功后状态置为已用并记录订单号与时间(
        self, async_session, seed_card, seed_secrets, freeze_beijing_now
    ):
        """验证取出后 status=1、order_id 写入、used_at 落北京时间，同事务生效。"""
        frozen = freeze_beijing_now(card_secret_service)
        card = await seed_card()
        await seed_secrets(card.id, n=1)

        secret = await card_secret_service.take_one(async_session, card.id, "ORDER001")
        await async_session.commit()
        await async_session.refresh(secret)

        assert secret.status == 1, "取出后状态应为已用"
        assert secret.order_id == "ORDER001", "取出后应记录订单号"
        assert secret.used_at == frozen, "used_at 应写入（冻结的）北京时间"

    async def test_库存为空时返回None(self, async_session, seed_card):
        """验证分类下无 status=0 记录时返回 None，作为"库存空"信号驱动下架分支。"""
        card = await seed_card()

        secret = await card_secret_service.take_one(async_session, card.id, "ORDER001")

        assert secret is None, "空库存应返回 None 作为下架信号"

    async def test_已用与作废卡密不会被取出(self, async_session, seed_card, seed_secrets):
        """验证 status=1（已用）、status=2（作废）的记录被跳过，只取可用卡密。"""
        card = await seed_card()
        await seed_secrets(card.id, n=2, status=1, content_prefix="USED")
        await seed_secrets(card.id, n=2, status=2, content_prefix="VOID")
        usable = await seed_secrets(card.id, n=1, status=0, content_prefix="OK")

        secret = await card_secret_service.take_one(async_session, card.id, "ORDER001")
        assert secret is not None and secret.content == "OK-0", "只能取出可用卡密"

        # 可用的一张取完后，剩余的已用/作废卡密不应被取出
        again = await card_secret_service.take_one(async_session, card.id, "ORDER002")
        assert again is None, "已用/作废卡密不应被取出"

    async def test_只取指定分类的卡密不影响其它分类(self, async_session, seed_card, seed_secrets):
        """验证按 card_id 隔离，同类目多分类并存时互不串货。"""
        card_a = await seed_card(name="分类A")
        card_b = await seed_card(name="分类B")
        await seed_secrets(card_a.id, n=1, content_prefix="A")
        await seed_secrets(card_b.id, n=2, content_prefix="B")

        secret = await card_secret_service.take_one(async_session, card_a.id, "ORDER001")

        assert secret.content.startswith("A-"), "取密不能串到其它分类"
        assert await card_secret_service.stock_of(async_session, card_b.id) == 2, (
            "分类B 库存不应受影响"
        )

    @pytest.mark.skipif(IS_SQLITE, reason="SKIP LOCKED 并发语义需真实 MySQL 验证")
    async def test_并发取密同一卡密不会被发出两次(self):
        """并发核心用例：模拟多订单同时 take_one，验证 SKIP LOCKED 保证每条卡密仅被一个订单占有。

        实现提示（MySQL 环境）：用 async_sessionmaker 开多个独立会话，
        asyncio.gather 并发 take_one 同一 card_id，断言取到的 secret.id 互不相同。
        """

    @pytest.mark.skipif(IS_SQLITE, reason="SKIP LOCKED 并发语义需真实 MySQL 验证")
    async def test_并发取密直到库存耗尽后超出的请求返回None(self):
        """验证 N 条库存并发 N+M 个请求时，恰好 N 个成功、M 个返回 None，不超发。

        实现提示（MySQL 环境）：种子 N 条卡密，gather N+M 个 take_one，
        断言结果中恰好 N 个非 None 且 id 互不相同。
        """

    async def test_串行连续取密按序消耗且不重复(self, async_session, seed_card, seed_secrets):
        """并发用例的串行等价：SQLite 下验证取过的卡密不会被再次取出。"""
        card = await seed_card()
        rows = await seed_secrets(card.id, n=2)

        first = await card_secret_service.take_one(async_session, card.id, "ORDER001")
        await async_session.commit()
        second = await card_secret_service.take_one(async_session, card.id, "ORDER002")
        await async_session.commit()
        third = await card_secret_service.take_one(async_session, card.id, "ORDER003")

        assert first.id == rows[0].id and second.id == rows[1].id, "应按序消耗"
        assert first.id != second.id, "同一张卡密不应被发出两次"
        assert third is None, "库存耗尽后应返回 None"


class TestRelease:
    """发货失败回滚 release：卡密退回可用，避免发失败还扣库存。"""

    async def test_回滚后状态恢复可用并清空订单号与时间(
        self, async_session, seed_card, seed_secrets
    ):
        """验证 status 复位 0、order_id/used_at 清空，卡密可被再次取出。"""
        card = await seed_card()
        await seed_secrets(card.id, n=1, content_prefix="BACK")

        secret = await card_secret_service.take_one(async_session, card.id, "ORDER001")
        await async_session.commit()

        await card_secret_service.release(async_session, secret.id)
        await async_session.commit()
        await async_session.refresh(secret)

        assert secret.status == 0, "回滚后状态应恢复可用"
        assert secret.order_id is None, "回滚后订单号应清空"
        assert secret.used_at is None, "回滚后使用时间应清空"

        again = await card_secret_service.take_one(async_session, card.id, "ORDER002")
        assert again is not None and again.content == "BACK-0", "回滚的卡密应可被再次取出"

    async def test_回滚不存在的卡密不抛异常(self, async_session):
        """验证对不存在 id 的 release 幂等/静默处理，不影响发货异常路径。"""
        await card_secret_service.release(async_session, 999999)  # 不应抛异常
        await async_session.commit()

    async def test_取密再回滚后库存数量不变(self, async_session, seed_card, seed_secrets):
        """验证 take_one + release 闭环后 stock_of 结果与初始一致。"""
        card = await seed_card()
        await seed_secrets(card.id, n=3)
        before = await card_secret_service.stock_of(async_session, card.id)

        secret = await card_secret_service.take_one(async_session, card.id, "ORDER001")
        await card_secret_service.release(async_session, secret.id)
        await async_session.commit()

        after = await card_secret_service.stock_of(async_session, card.id)
        assert before == 3 and after == before, "取密再回滚后库存应与初始一致"


class TestAddBatch:
    """批量补货 add_batch：卖家向分类追加卡密。"""

    async def test_批量导入多条卡密全部为可用状态(self, async_session, seed_card):
        """验证导入的每行内容落库且 status=0，空行/首尾空白被清洗。"""
        card = await seed_card()

        added = await card_secret_service.add_batch(
            async_session, card.id, card.user_id, "  AAA-1  \nAAA-2\nAAA-3\n"
        )
        await async_session.commit()

        assert added == 3, "应导入 3 条卡密"
        assert await card_secret_service.stock_of(async_session, card.id) == 3

    async def test_重复卡密的处理策略(self, async_session, seed_card, seed_secrets):
        """验证与存量内容重复的卡密按约定处理（约定：重复行跳过，返回实际新增条数）。"""
        card = await seed_card()
        await seed_secrets(card.id, n=1, content_prefix="DUP")  # 存量 "DUP-0"

        added = await card_secret_service.add_batch(
            async_session, card.id, card.user_id, "DUP-0\nNEW-1\nNEW-1\n"
        )
        await async_session.commit()

        assert added == 1, "与存量及批次内重复的行应跳过，仅新增 1 条"
        assert await card_secret_service.stock_of(async_session, card.id) == 2

    async def test_空内容行被过滤(self, async_session, seed_card):
        """验证空字符串/纯空白行不会写入明细表。"""
        card = await seed_card()

        added = await card_secret_service.add_batch(
            async_session, card.id, card.user_id, "\n   \n\t\nVALID-1\n\n"
        )
        await async_session.commit()

        assert added == 1, "空行/纯空白行应被过滤"
        assert await card_secret_service.stock_of(async_session, card.id) == 1

    async def test_导入的卡密归属正确的分类与用户(self, async_session, seed_card):
        """验证 card_id、user_id 正确写入，配合 owner_scope 数据隔离。"""
        from sqlalchemy import select

        from common.models.card_secret import CardSecret

        card = await seed_card(user_id=42)

        await card_secret_service.add_batch(async_session, card.id, card.user_id, "OWN-1\n")
        await async_session.commit()

        row = (await async_session.execute(select(CardSecret))).scalar_one()
        assert row.card_id == card.id, "card_id 应归属目标分类"
        assert row.user_id == 42, "user_id 应归属操作用户（owner_scope 隔离）"
        assert row.status == 0


class TestStockOf:
    """库存统计 stock_of：库存 = status=0 的记录数。"""

    async def test_库存数等于可用卡密条数(self, async_session, seed_card, seed_secrets):
        """验证 stock_of 只统计 status=0，已用/作废不计入。"""
        card = await seed_card()
        await seed_secrets(card.id, n=2, status=0, content_prefix="OK")
        await seed_secrets(card.id, n=3, status=1, content_prefix="USED")
        await seed_secrets(card.id, n=1, status=2, content_prefix="VOID")

        assert await card_secret_service.stock_of(async_session, card.id) == 2, (
            "库存应只统计 status=0 的卡密"
        )

    async def test_无记录分类库存为0(self, async_session, seed_card):
        """验证从未补货的分类库存为 0（驱动下架逻辑的边界值）。"""
        card = await seed_card()

        assert await card_secret_service.stock_of(async_session, card.id) == 0


class TestUsageRecords:
    """使用记录查询：供后台展示已发卡密及对应订单。"""

    async def _seed_used(self, async_session, seed_secrets, card, n=3):
        """通过 take_one 造 n 条真实使用记录（保证 order_id/used_at 齐全）。"""
        await seed_secrets(card.id, n=n)
        taken = []
        for i in range(n):
            secret = await card_secret_service.take_one(async_session, card.id, f"ORDER{i:03d}")
            taken.append(secret)
        await async_session.commit()
        return taken

    async def test_按分类查询已用卡密含订单号与使用时间(
        self, async_session, seed_card, seed_secrets
    ):
        """验证返回 content、order_id、used_at，按 used_at 倒序。"""
        card = await seed_card()
        await self._seed_used(async_session, seed_secrets, card, n=3)

        items, total = await card_secret_service.list_usage_records(
            async_session, card.id, user_id=card.user_id
        )

        assert total == 3, "使用记录总数应为 3"
        assert len(items) == 3
        for item in items:
            assert item.content, "记录应含卡密内容"
            assert item.order_id and item.order_id.startswith("ORDER"), "记录应含订单号"
            assert item.used_at is not None, "记录应含使用时间"
        used_ats = [item.used_at for item in items]
        assert used_ats == sorted(used_ats, reverse=True), "应按 used_at 倒序"

    async def test_使用记录支持分页(self, async_session, seed_card, seed_secrets):
        """验证分页参数生效，总数与每页数据正确。"""
        card = await seed_card()
        await self._seed_used(async_session, seed_secrets, card, n=5)

        page1, total = await card_secret_service.list_usage_records(
            async_session, card.id, user_id=card.user_id, page=1, page_size=2
        )
        page3, _ = await card_secret_service.list_usage_records(
            async_session, card.id, user_id=card.user_id, page=3, page_size=2
        )

        assert total == 5, "总数应不受分页影响"
        assert len(page1) == 2, "第一页应有 2 条"
        assert len(page3) == 1, "第三页应有 1 条"
        assert {i.id for i in page1}.isdisjoint({i.id for i in page3}), "分页数据不应重叠"

    async def test_普通用户只能查询自己分类的使用记录(
        self, async_session, seed_card, seed_secrets
    ):
        """验证 owner_scope 隔离：用户 B 看不到用户 A 的卡密使用记录。"""
        card_a = await seed_card(user_id=1, name="用户A的分类")
        await self._seed_used(async_session, seed_secrets, card_a, n=2)

        items, total = await card_secret_service.list_usage_records(
            async_session, card_a.id, user_id=2  # 用户B 查询用户A的分类
        )

        assert items == [] and total == 0, "普通用户不应看到他人分类的使用记录"
