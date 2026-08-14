"""卡密图片批量导入测试：common/services/card_secret_service.add_batch_images

覆盖方案 §3.1 / §3.3：针对某一卡密分类批量导入二维码图片卡密。
对应被测模块：common/services/card_secret_service.py（待新建 add_batch_images）。

接口约定（测试即接口定义，实现方按此开发）：
- add_batch_images(session, card_id, user_id, images) -> dict
  - images: list[tuple[str, str]]，每项为 (图片相对URL, 图片字节MD5)
  - 每条新建 content_type=1、status=0 的 CardSecret，content 存图片相对URL，
    image_hash 存 MD5，card_id/user_id 归属入参
  - 同分类内按 image_hash 去重（含与存量重复、批次内重复），重复条数计入 skipped
  - 返回 {"added": n, "skipped": m}
- 模型新增字段：content_type（TINYINT，默认 0=文本，1=二维码图片）、
  image_hash（CHAR(32)，可空，文本卡密为 NULL）
"""
import pytest

# TDD：服务实现前整个文件标记跳过（原因即待办）；实现落地后自动进入真实断言
card_secret_service = pytest.importorskip(
    "common.services.card_secret_service",
    reason="待实现：common/services/card_secret_service.py（add_batch_images）",
)

# 若服务模块已存在但 add_batch_images 未实现，同样跳过（原因即待办）
if not hasattr(card_secret_service, "add_batch_images"):
    pytest.skip(
        "待实现：card_secret_service.add_batch_images",
        allow_module_level=True,
    )


def _img(i: int) -> tuple[str, str]:
    """构造第 i 张测试图片的 (URL, MD5) 二元组。"""
    return (f"/static/uploads/card_secrets/qr_{i}.png", f"hash-{i:032d}"[:32])


class TestImageFields:
    """模型新增字段：content_type / image_hash。"""

    async def test_新记录默认文本类型(self, async_session, seed_card):
        """验证 content_type 默认 0（文本卡密），image_hash 默认为空。"""
        from common.models.card_secret import CardSecret

        card = await seed_card()
        row = CardSecret(card_id=card.id, user_id=1, content="CARD-001")
        async_session.add(row)
        await async_session.commit()
        await async_session.refresh(row)

        assert row.content_type == 0, "新卡密默认应为文本类型（content_type=0）"
        assert row.image_hash is None, "文本卡密 image_hash 应为空"


class TestAddBatchImages:
    """批量导入二维码图片卡密 add_batch_images。"""

    async def test_批量导入多张图片全部为可用状态(self, async_session, seed_card):
        """验证每张图片落库为 content_type=1、status=0 的明细，content 存 URL。"""
        from sqlalchemy import select

        from common.models.card_secret import CardSecret

        card = await seed_card()
        images = [_img(1), _img(2), _img(3)]

        result = await card_secret_service.add_batch_images(
            async_session, card.id, card.user_id, images
        )
        await async_session.commit()

        assert result == {"added": 3, "skipped": 0}, "3 张全新图片应全部导入"

        rows = (await async_session.execute(select(CardSecret))).scalars().all()
        assert len(rows) == 3
        for row, (url, img_hash) in zip(rows, images):
            assert row.content == url, "content 应存图片相对URL"
            assert row.image_hash == img_hash, "image_hash 应存图片MD5"
            assert row.content_type == 1, "图片卡密 content_type 应为 1"
            assert row.status == 0, "导入的图片卡密应为可用状态"

    async def test_重复图片按哈希跳过(self, async_session, seed_card):
        """验证与存量 image_hash 重复及批次内重复的图片被跳过，返回实际新增条数。"""
        card = await seed_card()
        first = await card_secret_service.add_batch_images(
            async_session, card.id, card.user_id, [_img(1)]
        )
        await async_session.commit()
        assert first == {"added": 1, "skipped": 0}

        # 第二批：_img(1) 与存量重复，_img(2) 批次内重复两次，仅 _img(3) 全新
        result = await card_secret_service.add_batch_images(
            async_session, card.id, card.user_id,
            [_img(1), _img(2), _img(2), _img(3)],
        )
        await async_session.commit()

        assert result == {"added": 2, "skipped": 2}, (
            "存量重复与批次内重复的图片应跳过"
        )
        assert await card_secret_service.stock_of(async_session, card.id) == 3

    async def test_空列表返回零(self, async_session, seed_card):
        """验证传入空列表时不落库、返回 {"added": 0, "skipped": 0}。"""
        card = await seed_card()

        result = await card_secret_service.add_batch_images(
            async_session, card.id, card.user_id, []
        )

        assert result == {"added": 0, "skipped": 0}

    async def test_归属字段正确(self, async_session, seed_card):
        """验证 card_id/user_id 归属目标分类与操作用户（owner_scope 隔离）。"""
        from sqlalchemy import select

        from common.models.card_secret import CardSecret

        card = await seed_card(user_id=42)

        await card_secret_service.add_batch_images(
            async_session, card.id, card.user_id, [_img(1)]
        )
        await async_session.commit()

        row = (await async_session.execute(select(CardSecret))).scalar_one()
        assert row.card_id == card.id, "card_id 应归属目标分类"
        assert row.user_id == 42, "user_id 应归属操作用户（owner_scope 隔离）"

    async def test_导入的图片卡密可被原子取出(self, async_session, seed_card):
        """验证导入的图片卡密进入可用库存，take_one 取出后保持 content_type=1。"""
        card = await seed_card()
        await card_secret_service.add_batch_images(
            async_session, card.id, card.user_id, [_img(1), _img(2)]
        )
        await async_session.commit()

        secret = await card_secret_service.take_one(async_session, card.id, "ORDER001")

        assert secret is not None, "导入的图片卡密应进入可用库存"
        assert secret.content_type == 1, "取出的卡密应保持图片类型，供发货走图片消息"
        assert secret.content == _img(1)[0], "应按写入顺序取最早的图片卡密"
