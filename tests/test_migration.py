"""存量数据迁移测试。

覆盖方案 §2/§4 迁移脚本：xy_cards.data_content 每行一条卡密 → xy_card_secrets 明细表。
对应被测模块：common/db/migrate_card_secrets.py（待新建，
提供 async migrate_data_content_to_secrets(session) 迁移函数）。

约定：迁移幂等键为 (card_id, content)；迁移不清空旧 data_content。
"""
import pytest
from sqlalchemy import func, select

# TDD：迁移模块与模型实现前整个文件标记跳过；实现落地后自动进入真实断言
_migrate_module = pytest.importorskip(
    "common.db.migrate_card_secrets", reason="待实现：common/db/migrate_card_secrets.py"
)
migrate_data_content_to_secrets = _migrate_module.migrate_data_content_to_secrets
_card_secret_module = pytest.importorskip(
    "common.models.card_secret", reason="待实现：common/models/card_secret.py"
)
CardSecret = _card_secret_module.CardSecret


async def _secret_rows(async_session, card_id):
    """查询指定分类下的明细行（按 id 升序）。"""
    result = await async_session.execute(
        select(CardSecret).where(CardSecret.card_id == card_id).order_by(CardSecret.id)
    )
    return result.scalars().all()


class TestDataContentMigration:
    """data_content 文本块拆分为明细行。"""

    async def test_每行卡密拆分为一条明细记录(self, async_session, seed_card):
        """验证 N 行 data_content 迁移后生成 N 条 status=0 的 CardSecret，内容逐行对应。"""
        card = await seed_card(data_content="KEY-001\nKEY-002\nKEY-003")

        await migrate_data_content_to_secrets(async_session)
        await async_session.commit()

        rows = await _secret_rows(async_session, card.id)
        assert len(rows) == 3, "3 行 data_content 应拆分为 3 条明细"
        assert [r.content for r in rows] == ["KEY-001", "KEY-002", "KEY-003"], (
            "明细内容应与原行逐行对应"
        )
        assert all(r.status == 0 for r in rows), "迁移的明细应全部为可用状态"

    async def test_迁移保留原分类与用户归属(self, async_session, seed_card):
        """验证明细的 card_id、user_id 与源 xy_cards 记录一致。"""
        card = await seed_card(user_id=42, data_content="KEY-001\nKEY-002")

        await migrate_data_content_to_secrets(async_session)
        await async_session.commit()

        rows = await _secret_rows(async_session, card.id)
        assert len(rows) == 2
        assert all(r.card_id == card.id for r in rows), "card_id 应与源卡券一致"
        assert all(r.user_id == 42 for r in rows), "user_id 应与源卡券一致"

    async def test_空行与首尾空白被清洗(self, async_session, seed_card):
        """验证空行不生成记录，含空白字符的卡密被 strip 后入库。"""
        card = await seed_card(data_content="  KEY-001  \n\n   \nKEY-002\n")

        await migrate_data_content_to_secrets(async_session)
        await async_session.commit()

        rows = await _secret_rows(async_session, card.id)
        assert [r.content for r in rows] == ["KEY-001", "KEY-002"], (
            "空行应跳过、卡密首尾空白应 strip"
        )

    async def test_迁移幂等可重复执行(self, async_session, seed_card):
        """验证重复执行迁移不产生重复明细（按 card_id+content 或迁移标记去重）。"""
        card = await seed_card(data_content="KEY-001\nKEY-002")

        await migrate_data_content_to_secrets(async_session)
        await async_session.commit()
        await migrate_data_content_to_secrets(async_session)  # 重复执行
        await async_session.commit()

        count = await async_session.scalar(
            select(func.count()).select_from(CardSecret).where(CardSecret.card_id == card.id)
        )
        assert count == 2, "重复迁移不应产生重复明细"

    async def test_非data型卡券不参与迁移(self, async_session, seed_card):
        """验证 text/image/api 型卡券的迁移被跳过。"""
        text_card = await seed_card(type="text", text_content="这是一段说明文字")

        await migrate_data_content_to_secrets(async_session)
        await async_session.commit()

        count = await async_session.scalar(
            select(func.count()).select_from(CardSecret)
            .where(CardSecret.card_id == text_card.id)
        )
        assert count == 0, "非 data 型卡券不应生成明细"

    async def test_空data_content分类迁移后库存为0(self, async_session, seed_card):
        """验证无内容的分类迁移后不产生明细，stock_of 返回 0。"""
        card = await seed_card(data_content=None)

        await migrate_data_content_to_secrets(async_session)
        await async_session.commit()

        count = await async_session.scalar(
            select(func.count()).select_from(CardSecret).where(CardSecret.card_id == card.id)
        )
        assert count == 0, "空 data_content 分类不应产生明细"

    async def test_迁移后旧data_content保留兼容(self, async_session, seed_card):
        """验证迁移不清空 xy_cards.data_content，旧逻辑回退路径仍可用。"""
        card = await seed_card(data_content="KEY-001\nKEY-002")

        await migrate_data_content_to_secrets(async_session)
        await async_session.commit()
        await async_session.refresh(card)

        assert card.data_content == "KEY-001\nKEY-002", "迁移后旧 data_content 应原样保留"
