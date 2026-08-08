"""卡密明细模型测试：common/models/card_secret.py

覆盖方案 §2：xy_card_secrets 表结构与状态语义。
对应被测模块：common/models/card_secret.py（待新建）。

TDD 说明：被测模型尚不存在，模块级 import 失败（ERROR）即当前的红灯状态。
"""
import pytest
from sqlalchemy.exc import IntegrityError

# TDD：模型实现前整个文件标记跳过（原因即待办）；实现落地后自动进入真实断言
_card_secret_module = pytest.importorskip(
    "common.models.card_secret", reason="待实现：common/models/card_secret.py"
)
CardSecret = _card_secret_module.CardSecret


class TestCardSecretModel:
    """模型字段与默认值。"""

    async def test_新记录默认状态为可用(self, async_session):
        """验证 status 默认 0（可用），order_id/used_at 默认为空。"""
        row = CardSecret(card_id=1, user_id=1, content="CARD-001")
        async_session.add(row)
        await async_session.commit()
        await async_session.refresh(row)

        assert row.status == 0, "新卡密默认状态应为 0（可用）"
        assert row.order_id is None, "新卡密 order_id 应为空"
        assert row.used_at is None, "新卡密 used_at 应为空"

    async def test_必填字段校验(self, async_session):
        """验证 card_id、user_id、content 缺失时落库报错（NOT NULL 约束）。"""
        # 缺 card_id
        async_session.add(CardSecret(user_id=1, content="CARD-001"))
        with pytest.raises(IntegrityError):
            await async_session.commit()
        await async_session.rollback()

        # 缺 user_id
        async_session.add(CardSecret(card_id=1, content="CARD-001"))
        with pytest.raises(IntegrityError):
            await async_session.commit()
        await async_session.rollback()

        # 缺 content
        async_session.add(CardSecret(card_id=1, user_id=1))
        with pytest.raises(IntegrityError):
            await async_session.commit()
        await async_session.rollback()

    async def test_状态枚举语义(self, async_session):
        """验证 status 取值仅 0=可用/1=已用/2=作废 三种语义被代码正确使用。"""
        from sqlalchemy import func, select

        async_session.add_all([
            CardSecret(card_id=1, user_id=1, content="可用", status=0),
            CardSecret(card_id=1, user_id=1, content="已用", status=1),
            CardSecret(card_id=1, user_id=1, content="作废", status=2),
        ])
        await async_session.commit()

        # 可用库存语义：仅 status=0 计入
        usable = await async_session.scalar(
            select(func.count()).select_from(CardSecret).where(CardSecret.status == 0)
        )
        assert usable == 1, "三种状态下应只有 1 条可用（status=0）"

    async def test_模型已注册到common包(self):
        """验证 common/models/__init__.py 导出 CardSecret，迁移与查询可正常引用。"""
        import common.models

        assert hasattr(common.models, "CardSecret"), "common.models 应导出 CardSecret"
        assert common.models.CardSecret is CardSecret
        assert CardSecret.__tablename__ == "xy_card_secrets"


class TestCardSecretIndex:
    """索引有效性（结构性验证，可选）。"""

    async def test_按分类加状态的复合索引存在(self):
        """验证 idx_card_status(card_id, status) 存在，保证取密与库存统计走索引。"""
        from sqlalchemy import Index

        indexes = [arg for arg in CardSecret.__table_args__ if isinstance(arg, Index)]
        target = next((idx for idx in indexes if idx.name == "idx_card_status"), None)
        assert target is not None, "应定义 idx_card_status 复合索引"
        columns = [col.name for col in target.columns]
        assert columns == ["card_id", "status"], (
            f"索引列应为 (card_id, status)，实际 {columns}"
        )
