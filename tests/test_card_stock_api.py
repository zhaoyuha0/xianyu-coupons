"""卡密库存后端接口测试。

覆盖方案 §4：backend-web/app/api/routes/cards.py 与 card_service.py 新增端点。
对应被测模块：backend-web/app/api/routes/cards.py（待扩展）。

端点约定（测试即接口定义，实现方按此开发；统一响应 {"success","code","message","data"}）：
- GET  /cards/{card_id}/stock           → data {"available","used","void"}
- GET  /cards                           → data 型卡券列表项附带 "stock" 字段
- GET  /cards/{card_id}/usage-records   → data {"total","items":[{content,order_id,used_at}]}（分页）
- POST /cards/{card_id}/secrets         → body {"content": 多行文本}，data {"added": n}
- POST /cards/{card_id}/secrets/batch-images → multipart 多文件字段 files，
  data {"added": n, "skipped": m}（方案 §3.3，批量导入二维码图片卡密）
- PUT  /cards/item/{item_id}/cards      → 绑定校验（一商品一分类），已有端点待加校验

测试方式：独立 FastAPI 实例挂载 cards 路由，dependency_overrides 覆写
认证与数据库会话，httpx ASGITransport 进程内发请求，不起真实端口。
TDD：新端点实现前返回 404/405，即红灯。
"""
from types import SimpleNamespace

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def api(async_session, service_importer):
    """挂载 cards 路由的测试环境：.client 发请求、.user 控制当前用户、.app/.deps 调依赖。"""
    with service_importer("backend-web") as imp:
        deps = imp.import_module("app.api.deps")
        cards = imp.import_module("app.api.routes.cards")

        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from common.models.user import UserRole, UserStatus

        app = FastAPI()
        app.include_router(cards.router, prefix="/cards")

        # 普通用户身份（role/status 为 resolve_owner_scope 与认证依赖所必需）
        fake_user = SimpleNamespace(
            id=1, role=UserRole.MEMBER, status=UserStatus.ACTIVE
        )
        app.dependency_overrides[deps.get_current_active_user] = lambda: fake_user

        async def _test_session():
            yield async_session

        app.dependency_overrides[deps.get_db_session] = _test_session

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(client=client, app=app, deps=deps, user=fake_user)


class TestStockQueryApi:
    """库存与使用记录查询端点。"""

    async def test_查询卡密分类库存数(self, api, seed_card, seed_secrets):
        """验证 GET 库存接口返回指定分类的可用/已用/作废数量。"""
        card = await seed_card()
        await seed_secrets(card.id, n=2, status=0, content_prefix="OK")
        await seed_secrets(card.id, n=3, status=1, content_prefix="USED")
        await seed_secrets(card.id, n=1, status=2, content_prefix="VOID")

        resp = await api.client.get(f"/cards/{card.id}/stock")

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        data = body["data"]
        assert data["available"] == 2, "可用数应为 status=0 的条数"
        assert data["used"] == 3, "已用数应为 status=1 的条数"
        assert data["void"] == 1, "作废数应为 status=2 的条数"

    async def test_卡券列表携带库存字段(self, api, seed_card, seed_secrets):
        """验证 /cards 列表响应为 data 型卡券附带 stock 字段，前端可直接展示。

        现有响应结构（见 card_service.get_cards_paginated）：
        {"list": [...], "total", "page", "page_size", "total_pages"}（未包 ApiResponse）。
        """
        card = await seed_card()
        await seed_secrets(card.id, n=2)

        resp = await api.client.get("/cards")

        assert resp.status_code == 200
        body = resp.json()
        target = next(c for c in body["list"] if c["id"] == card.id)
        assert target.get("stock") == 2, "data 型卡券列表项应附带可用库存数"

    async def test_分页查询使用记录(self, api, seed_card, seed_secrets, async_session):
        """验证使用记录接口返回 content/order_id/used_at 并支持分页。"""
        from common.services import card_secret_service

        card = await seed_card()
        await seed_secrets(card.id, n=3)
        for i in range(3):
            await card_secret_service.take_one(async_session, card.id, f"ORDER{i:03d}")
        await async_session.commit()

        resp = await api.client.get(
            f"/cards/{card.id}/usage-records", params={"page": 1, "page_size": 2}
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        data = body["data"]
        assert data["total"] == 3, "总数应为全部使用记录数"
        assert len(data["items"]) == 2, "每页条数应受 page_size 限制"
        first = data["items"][0]
        assert first["content"] and first["order_id"] and first["used_at"], (
            "记录应含卡密内容/订单号/使用时间"
        )

    async def test_未登录访问被拒绝(self, api):
        """验证所有库存接口走 get_current_active_user，匿名请求返回 401。"""
        # 移除认证覆写，模拟匿名请求（无 Authorization 头）
        api.app.dependency_overrides.pop(api.deps.get_current_active_user, None)
        api.app.dependency_overrides.pop(api.deps.get_current_user, None)

        for path in ("/cards/1/stock", "/cards/1/usage-records"):
            resp = await api.client.get(path)
            assert resp.status_code == 401, f"匿名访问 {path} 应返回 401"
        resp = await api.client.post("/cards/1/secrets", json={"content": "X-1"})
        assert resp.status_code == 401, "匿名补货应返回 401"

    async def test_普通用户只能看自己分类的库存(self, api, seed_card, seed_secrets):
        """验证 owner_scope 生效：跨用户查询返回 404 业务错误，不泄露他人卡密。

        契约定稿：HTTP 200 + {"success": false, "code": 404}（遵循统一响应约定，
        对不存在的分类与他人的分类一视同仁，不泄露存在性）。
        """
        card = await seed_card(user_id=1)  # 属于用户 1
        await seed_secrets(card.id, n=5)
        api.user.id = 2  # 当前登录用户换成 2

        resp = await api.client.get(f"/cards/{card.id}/stock")

        assert resp.status_code == 200, "业务错误按统一响应返回 HTTP 200"
        body = resp.json()
        assert body["success"] is False, "跨用户查询应返回业务错误"
        assert body["code"] == 404, "错误码应为 404（不泄露分类存在性）"


class TestRestockApi:
    """批量导入补货端点。"""

    async def test_批量导入卡密成功(self, api, seed_card, async_session):
        """验证提交多行文本后按行拆分入库，返回成功条数。"""
        card = await seed_card()

        resp = await api.client.post(
            f"/cards/{card.id}/secrets", json={"content": "KEY-1\nKEY-2\n\nKEY-3\n"}
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["added"] == 3, "应按行拆分入库并返回成功条数"

        from common.services import card_secret_service
        assert await card_secret_service.stock_of(async_session, card.id) == 3

    async def test_导入内容为空时返回参数错误(self, api, seed_card):
        """验证空内容/全空行提交返回 400 及明确错误信息。"""
        card = await seed_card()

        resp = await api.client.post(f"/cards/{card.id}/secrets", json={"content": "\n  \n"})

        body = resp.json()
        assert resp.status_code in (200, 400), "业务错误按统一响应处理"
        assert body["success"] is False, "空内容应返回业务错误"
        assert body["code"] == 400, "错误码应为 400"
        assert body["message"], "应返回明确错误信息"

    async def test_导入目标分类不存在或无权限被拒绝(self, api, seed_card):
        """验证他人 card_id / 不存在 card_id 返回 403/404。"""
        other_card = await seed_card(user_id=99)  # 他人分类
        api.user.id = 1

        resp_missing = await api.client.post(
            "/cards/999999/secrets", json={"content": "KEY-1"}
        )
        resp_foreign = await api.client.post(
            f"/cards/{other_card.id}/secrets", json={"content": "KEY-1"}
        )

        for resp in (resp_missing, resp_foreign):
            body = resp.json()
            assert body["success"] is False, "不存在/无权限的分类应拒绝导入"
            assert body["code"] in (403, 404), "错误码应为 403 或 404"


class TestBindingValidationApi:
    """绑定接口的"一商品一分类"校验（接口层）。"""

    async def test_重复绑定data型卡券返回业务错误(self, api, seed_card):
        """验证 PUT /cards/item/{item_id}/cards 绑第二个 data 卡券时返回明确错误码与提示。"""
        card_a = await seed_card(name="分类A")
        card_b = await seed_card(name="分类B")

        resp = await api.client.put(
            "/cards/item/ITEM001/cards",
            json={"card_items": [
                {"card_id": card_a.id, "source": "own"},
                {"card_id": card_b.id, "source": "own"},
            ]},
        )

        body = resp.json()
        assert body["success"] is False, "重复绑定 data 卡券应返回业务错误"
        assert body["message"], "应返回对卖家可读的明确提示"

    async def test_合法绑定返回成功且关联落库(self, api, seed_card, async_session):
        """验证正常绑定返回 200，xy_card_item_relations 记录正确（含 source/dock_record_id）。"""
        from sqlalchemy import select

        from common.models.card_item_relation import CardItemRelation

        card = await seed_card()

        resp = await api.client.put(
            "/cards/item/ITEM001/cards",
            json={"card_items": [
                {"card_id": card.id, "source": "own", "dock_record_id": 0},
            ]},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True, "合法绑定应成功"

        relation = (await async_session.execute(
            select(CardItemRelation).where(CardItemRelation.item_id == "ITEM001")
        )).scalar_one()
        assert relation.card_id == card.id
        assert relation.source == "own"
        assert relation.dock_record_id == 0


def _png(tag: bytes) -> bytes:
    """构造带唯一标识的最小 PNG 字节串（不同 tag 产生不同 MD5）。"""
    return b"\x89PNG\r\n\x1a\n" + tag


def _files(*tags: bytes) -> list:
    """把若干 tag 转成 httpx multipart 的 files 参数（字段名 files）。"""
    return [
        ("files", (f"qr_{tag.decode()}.png", _png(tag), "image/png"))
        for tag in tags
    ]


class TestBatchImagesApi:
    """批量导入卡密图片端点（方案 §3.3）。

    契约：POST /cards/{card_id}/secrets/batch-images，multipart 字段 files。
    """

    async def test_批量上传二维码图片成功(self, api, seed_card, async_session):
        """验证多张图片一次上传后逐张落库为可用图片卡密，返回 added/skipped。"""
        card = await seed_card()

        resp = await api.client.post(
            f"/cards/{card.id}/secrets/batch-images",
            files=_files(b"a1", b"a2", b"a3"),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"] == {"added": 3, "skipped": 0}, "3 张全新图片应全部导入"

        from sqlalchemy import select

        from common.models.card_secret import CardSecret

        rows = (await async_session.execute(
            select(CardSecret).where(CardSecret.card_id == card.id)
        )).scalars().all()
        assert len(rows) == 3
        for row in rows:
            assert row.content_type == 1, "导入的卡密应为图片类型"
            assert row.status == 0, "导入的卡密应为可用状态"
            assert row.content.startswith("/static/uploads/card_secrets/"), (
                "content 应存落盘后的相对URL"
            )
            assert row.image_hash, "image_hash 应存图片MD5"

    async def test_重复图片按字节哈希跳过(self, api, seed_card, async_session):
        """验证同字节图片（含批次内重复与跨批次重复）被跳过，计入 skipped。"""
        card = await seed_card()

        first = await api.client.post(
            f"/cards/{card.id}/secrets/batch-images", files=_files(b"a1")
        )
        assert first.json()["data"] == {"added": 1, "skipped": 0}

        # 第二批：a1 与存量重复，a2 批次内重复，仅 a3 全新
        resp = await api.client.post(
            f"/cards/{card.id}/secrets/batch-images",
            files=_files(b"a1", b"a2", b"a2", b"a3"),
        )

        body = resp.json()
        assert body["success"] is True
        assert body["data"] == {"added": 2, "skipped": 2}, (
            "存量重复与批次内重复的图片应跳过"
        )

        from common.services import card_secret_service
        assert await card_secret_service.stock_of(async_session, card.id) == 3

    async def test_未登录上传被拒绝(self, api, seed_card):
        """验证匿名请求上传图片返回 401。"""
        card = await seed_card()
        api.app.dependency_overrides.pop(api.deps.get_current_active_user, None)
        api.app.dependency_overrides.pop(api.deps.get_current_user, None)

        resp = await api.client.post(
            f"/cards/{card.id}/secrets/batch-images", files=_files(b"a1")
        )
        assert resp.status_code == 401, "匿名上传应返回 401"

    async def test_他人或不存在分类被拒绝(self, api, seed_card):
        """验证他人 card_id / 不存在 card_id 返回 403/404，不泄露存在性。"""
        other_card = await seed_card(user_id=99)
        api.user.id = 1

        resp_missing = await api.client.post(
            "/cards/999999/secrets/batch-images", files=_files(b"a1")
        )
        resp_foreign = await api.client.post(
            f"/cards/{other_card.id}/secrets/batch-images", files=_files(b"a1")
        )

        for resp in (resp_missing, resp_foreign):
            body = resp.json()
            assert body["success"] is False, "不存在/无权限的分类应拒绝导入"
            assert body["code"] in (403, 404), "错误码应为 403 或 404"

    async def test_非data型卡券拒绝导入(self, api, seed_card):
        """验证 text/image/api 型卡券不接受卡密图片导入（仅 data 型分类可导入）。"""
        text_card = await seed_card(type="text", text_content="说明")

        resp = await api.client.post(
            f"/cards/{text_card.id}/secrets/batch-images", files=_files(b"a1")
        )

        body = resp.json()
        assert body["success"] is False, "非 data 型卡券应拒绝导入"
        assert body["code"] == 400, "错误码应为 400"

    async def test_非图片文件被拒绝(self, api, seed_card):
        """验证 content_type 非 image/* 的文件返回 400 及明确错误信息。"""
        card = await seed_card()

        resp = await api.client.post(
            f"/cards/{card.id}/secrets/batch-images",
            files=[("files", ("evil.txt", b"not an image", "text/plain"))],
        )

        body = resp.json()
        assert body["success"] is False, "非图片文件应拒绝"
        assert body["code"] == 400, "错误码应为 400"
        assert body["message"], "应返回明确错误信息"

    async def test_单批超过50张被拒绝(self, api, seed_card):
        """验证单批上传数量上限（50 张），超出返回 400。"""
        card = await seed_card()
        files = [
            ("files", (f"qr_{i}.png", _png(f"b{i}".encode()), "image/png"))
            for i in range(51)
        ]

        resp = await api.client.post(
            f"/cards/{card.id}/secrets/batch-images", files=files
        )

        body = resp.json()
        assert body["success"] is False, "单批超过 50 张应拒绝"
        assert body["code"] == 400, "错误码应为 400"
