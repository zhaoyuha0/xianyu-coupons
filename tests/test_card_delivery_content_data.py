"""提货场景（无闲鱼订单）data 型卡券取内容测试。

覆盖方案 §4 改动清单：common/services/card_delivery_content.build_delivery_content
的 data 型分支从 consume_batch_data 改为走 card_secret_service（卡密明细表）。

接口约定（测试即接口定义，实现方按此开发）：
- build_delivery_content(session, card, context) 的 data 型分支改为
  card_secret_service.take_one(session, card.id, context["order_id"])
- 取到文本卡密（content_type=0）：返回卡密原文（可拼接备注/图片，沿用现逻辑）
- 取到图片卡密（content_type=1）：把图片URL作为文本返回
  （提货为纯文本场景，与 image 型卡券提货行为一致）
- 库存空（take_one 返回 None）：返回 None
- 取出的卡密同事务置为已用并记录 order_id（提货虚拟订单号）

TDD：当前实现仍走 consume_batch_data（读 data_content），
种子只写 xy_card_secrets 时本文件用例即红灯。
"""
import pytest

from common.services.card_delivery_content import build_delivery_content


async def _seed_image_secret(async_session, card, url="/static/uploads/card_secrets/qr_1.png"):
    """写入一条可用的二维码图片卡密并返回。"""
    # TDD：CardSecret 模型实现前，此处 ImportError 即红灯
    from common.models.card_secret import CardSecret

    row = CardSecret(
        card_id=card.id,
        user_id=card.user_id,
        content=url,
        content_type=1,
        image_hash="h" * 32,
        status=0,
    )
    async_session.add(row)
    await async_session.commit()
    await async_session.refresh(row)
    return row


class TestPickupDataCardViaSecrets:
    """data 型卡券提货改走卡密明细表。"""

    async def test_提货消耗一条文本卡密并记录订单号(
        self, async_session, seed_card, seed_secrets
    ):
        """验证提货返回最早一条卡密内容，且该卡密同事务置为已用、记录虚拟订单号。"""
        card = await seed_card()
        rows = await seed_secrets(card.id, n=2, content_prefix="KAMI")

        content = await build_delivery_content(
            async_session, card, {"order_id": "PICK001"}
        )
        await async_session.commit()

        assert content is not None and "KAMI-0" in content, "提货应返回最早一条卡密内容"
        await async_session.refresh(rows[0])
        assert rows[0].status == 1, "被提货的卡密应置为已用"
        assert rows[0].order_id == "PICK001", "被提货的卡密应记录提货虚拟订单号"
        await async_session.refresh(rows[1])
        assert rows[1].status == 0, "一次提货只应消耗一条卡密"

    async def test_图片卡密提货返回图片URL(self, async_session, seed_card):
        """验证取到二维码图片卡密时把图片URL作为文本返回（与 image 型卡券提货一致）。"""
        card = await seed_card()
        secret = await _seed_image_secret(async_session, card)

        content = await build_delivery_content(
            async_session, card, {"order_id": "PICK001"}
        )
        await async_session.commit()

        assert content is not None and secret.content in content, (
            "图片卡密提货应返回图片URL文本"
        )
        await async_session.refresh(secret)
        assert secret.status == 1, "被提货的图片卡密应置为已用"

    async def test_库存空时提货返回None(self, async_session, seed_card):
        """验证卡密明细表无可用库存时返回 None（不再回退读旧 data_content）。"""
        card = await seed_card()  # 不补货，库存为 0

        content = await build_delivery_content(
            async_session, card, {"order_id": "PICK001"}
        )

        assert content is None, "库存空时提货应失败返回 None"
