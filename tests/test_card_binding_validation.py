"""商品—卡密分类绑定校验测试。

覆盖方案 §2 绑定约束：同一商品同一规格只能绑一张 data 型卡券。
对应被测模块：backend-web/app/services/card_service.py
（update_item_card_relations 增加校验，待改造）。

说明：校验规则最终由 CardService.update_item_card_relations 委托
CardMatcher.update_item_card_relations 落地，本测试直接打 CardMatcher 层。
校验失败约定抛 ValueError（由上层转为统一响应的业务错误）。
"""
import pytest
from sqlalchemy import func, select

from common.models.card_item_relation import CardItemRelation
from common.services.card_matcher import CardMatcher


async def _relation_count(async_session, item_id) -> int:
    return await async_session.scalar(
        select(func.count()).select_from(CardItemRelation)
        .where(CardItemRelation.item_id == item_id)
    )


class TestOneItemOneCategory:
    """一商品一分类约束（绑定写入侧校验）。"""

    async def test_商品绑定一个卡密分类成功(self, async_session, seed_card):
        """验证商品首次绑定 data 型卡券时正常落 xy_card_item_relations。"""
        card = await seed_card()
        matcher = CardMatcher(async_session)

        result = await matcher.update_item_card_relations(
            "ITEM001", user_id=1,
            card_relations=[{"card_id": card.id, "source": "own", "dock_record_id": 0}],
        )
        await async_session.commit()

        assert result["added"] == 1, "首次绑定应成功"
        assert await _relation_count(async_session, "ITEM001") == 1, "关联记录应落库"

    async def test_商品重复绑定第二个data型卡券被拒绝(self, async_session, seed_card):
        """验证同一商品同一规格已绑 data 卡券时再绑第二张 data 卡券返回明确错误。"""
        card_a = await seed_card(name="分类A")
        card_b = await seed_card(name="分类B")
        matcher = CardMatcher(async_session)

        with pytest.raises(ValueError, match="data|卡密|分类"):
            await matcher.update_item_card_relations(
                "ITEM001", user_id=1,
                card_relations=[
                    {"card_id": card_a.id, "source": "own", "dock_record_id": 0},
                    {"card_id": card_b.id, "source": "own", "dock_record_id": 0},
                ],
            )

    async def test_多规格商品允许每个规格各绑一种卡密(self, async_session, seed_card):
        """验证带 spec_name/spec_value 的绑定按规格隔离，不同规格可各绑一张 data 卡券。"""
        # 注意：CardMatcher 的规格判定以 is_multi_spec=True 为前提，种子必须显式开启
        card_spicy = await seed_card(
            name="辣味", is_multi_spec=True, spec_name="口味", spec_value="辣"
        )
        card_sweet = await seed_card(
            name="甜味", is_multi_spec=True, spec_name="口味", spec_value="甜"
        )
        matcher = CardMatcher(async_session)

        result = await matcher.update_item_card_relations(
            "ITEM001", user_id=1,
            card_relations=[
                {"card_id": card_spicy.id, "source": "own", "dock_record_id": 0},
                {"card_id": card_sweet.id, "source": "own", "dock_record_id": 0},
            ],
        )
        await async_session.commit()

        assert result["added"] == 2, "不同规格的 data 卡券应允许共存"

    async def test_商品同时绑定非data型卡券不受限制(self, async_session, seed_card):
        """验证 text/image/api 型卡券可与 data 卡券共存（说明文字+卡密同发场景）。"""
        data_card = await seed_card(name="卡密分类")
        text_card = await seed_card(name="说明文字", type="text", text_content="使用说明")
        matcher = CardMatcher(async_session)

        result = await matcher.update_item_card_relations(
            "ITEM001", user_id=1,
            card_relations=[
                {"card_id": data_card.id, "source": "own", "dock_record_id": 0},
                {"card_id": text_card.id, "source": "own", "dock_record_id": 0},
            ],
        )
        await async_session.commit()

        assert result["added"] == 2, "data 卡券与非 data 卡券应允许共存"

    async def test_分销卡券不参与本地库存校验(self, async_session, seed_card):
        """验证 source=dock_l1/dock_l2 的绑定不受"一商品一分类"约束（分销不走本地库存）。"""
        own_card = await seed_card(name="自有卡密")
        dock_card = await seed_card(name="分销卡密")
        matcher = CardMatcher(async_session)

        result = await matcher.update_item_card_relations(
            "ITEM001", user_id=1,
            card_relations=[
                {"card_id": own_card.id, "source": "own", "dock_record_id": 0},
                {"card_id": dock_card.id, "source": "dock_l1", "dock_record_id": 100},
            ],
        )
        await async_session.commit()

        assert result["added"] == 2, "分销卡券绑定不受一商品一分类约束"

    async def test_重置式绑定先删后插保持原子(self, async_session, seed_card):
        """验证 PUT 重置绑定时旧关联删除与新关联插入在同一事务，失败整体回滚。"""
        card_a = await seed_card(name="分类A")
        card_b = await seed_card(name="分类B")
        matcher = CardMatcher(async_session)

        # 先成功绑定分类A
        await matcher.update_item_card_relations(
            "ITEM001", user_id=1,
            card_relations=[{"card_id": card_a.id, "source": "own", "dock_record_id": 0}],
        )
        await async_session.commit()

        # 再用非法组合（两张 data 卡券）重置绑定：应报错且旧关联完好
        with pytest.raises(ValueError):
            await matcher.update_item_card_relations(
                "ITEM001", user_id=1,
                card_relations=[
                    {"card_id": card_a.id, "source": "own", "dock_record_id": 0},
                    {"card_id": card_b.id, "source": "own", "dock_record_id": 0},
                ],
            )
        await async_session.rollback()

        assert await _relation_count(async_session, "ITEM001") == 1, (
            "校验失败时旧关联不应被删除（先校验再删插）"
        )

    async def test_换绑卡密分类后旧分类不再参与发货匹配(self, async_session, seed_card):
        """验证换绑后 CardMatcher.get_cards_by_item_id 只命中新分类。"""
        card_a = await seed_card(name="旧分类")
        card_b = await seed_card(name="新分类")
        matcher = CardMatcher(async_session)

        await matcher.update_item_card_relations(
            "ITEM001", user_id=1,
            card_relations=[{"card_id": card_a.id, "source": "own", "dock_record_id": 0}],
        )
        # 换绑为新分类
        await matcher.update_item_card_relations(
            "ITEM001", user_id=1,
            card_relations=[{"card_id": card_b.id, "source": "own", "dock_record_id": 0}],
        )
        await async_session.commit()

        matched = await matcher.get_cards_by_item_id("ITEM001")
        matched_ids = {c["id"] for c in matched}
        assert matched_ids == {card_b.id}, "换绑后只应命中新分类"


class TestBindingReadConsistency:
    """绑定读取侧与 CardMatcher 的一致性。"""

    async def test_旧字段item_id回退兼容仍生效(self, async_session, seed_card):
        """验证关联表无数据时回退 xy_cards.item_id 的兼容逻辑不被新校验破坏。"""
        card = await seed_card(name="旧式绑定", item_id="ITEM_LEGACY")
        matcher = CardMatcher(async_session)

        matched = await matcher.get_cards_by_item_id("ITEM_LEGACY")

        assert {c["id"] for c in matched} == {card.id}, "旧字段回退匹配应保持可用"
