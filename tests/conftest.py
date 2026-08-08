"""测试公共夹具（fixtures）。

说明：
- 所有数据库测试使用 SQLite + aiosqlite 内存库隔离，严禁连接真实 MySQL。
- 种子夹具采用"工厂模式"：夹具返回一个 async 工厂函数，测试按需传参造数据。
- 被测模块（card_secret、card_secret_service 等）按 TDD 尚不存在，
  相关夹具/测试在导入处自然报 ImportError，即 TDD 的"红"状态。
"""
from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import BigInteger, event
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

# 项目根目录兜底加入 sys.path（pytest 以包模式加载 tests 时通常已加入）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试统一使用 SQLite 内存库；并发语义（SKIP LOCKED）需真实 MySQL 验证，
# 相关用例用 pytest.mark.skipif(IS_SQLITE, ...) 跳过。
IS_SQLITE = True


# ---------------------------------------------------------------------------
# SQLite 方言兼容：MySQL 专有类型在 SQLite 下的编译回退
# ---------------------------------------------------------------------------

@compiles(LONGTEXT, "sqlite")
def _compile_longtext_sqlite(type_, compiler, **kw):  # noqa: ANN001
    """xy_cards 等表使用 MySQL LONGTEXT，SQLite 建表时回退为 TEXT。"""
    return "TEXT"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(type_, compiler, **kw):  # noqa: ANN001
    """项目主键统一用 BigInteger；SQLite 只有 INTEGER 主键才是 rowid 别名
    （BIGINT PRIMARY KEY 不会自增），测试建表时统一回退为 INTEGER。"""
    return "INTEGER"


# ---------------------------------------------------------------------------
# 数据库相关夹具
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def async_session():
    """提供隔离的异步数据库会话（每个测试独立内存库，用完销毁）。

    用于所有直接操作 xy_card_secrets / xy_cards / xy_card_item_relations 的测试。
    """
    import common.models  # noqa: F401  触发全部模型注册到 Base.metadata
    from common.db.base_class import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    # CardMatcher 等既有代码的原生 SQL 使用 MySQL 的 NOW()，在 SQLite 连接上注册同名函数
    @event.listens_for(engine.sync_engine, "connect")
    def _register_sqlite_now(dbapi_conn, _):  # noqa: ANN001
        dbapi_conn.create_function(
            "now", 0, lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def seed_card(async_session):
    """构造一张 data 型卡券（卡密分类）的工厂夹具。

    参数：user_id、name、spec_name/spec_value（可选，用于多规格场景）。
    返回：xy_cards 记录对象。
    """
    from common.models.card import Card

    counter = {"n": 0}

    async def _factory(user_id: int = 1, name: str | None = None,
                       type: str = "data", **kwargs):  # noqa: A002
        counter["n"] += 1
        card = Card(
            user_id=user_id,
            name=name or f"测试卡券{counter['n']}",
            type=type,
            **kwargs,
        )
        async_session.add(card)
        await async_session.commit()
        await async_session.refresh(card)
        return card

    return _factory


@pytest_asyncio.fixture
async def seed_secrets(async_session):
    """向指定卡密分类批量写入 N 条可用卡密的工厂夹具。

    参数：card_id、数量 n、初始 status（默认 0=可用）。
    返回：写入的 CardSecret 记录列表。
    """
    async def _factory(card_id: int, n: int = 3, status: int = 0,
                       user_id: int = 1, content_prefix: str = "SECRET"):
        # TDD：CardSecret 模型实现前，此处 ImportError 即红灯
        from common.models.card_secret import CardSecret

        rows = [
            CardSecret(
                card_id=card_id,
                user_id=user_id,
                content=f"{content_prefix}-{i}",
                status=status,
            )
            for i in range(n)
        ]
        async_session.add_all(rows)
        await async_session.commit()
        for row in rows:
            await async_session.refresh(row)
        return rows

    return _factory


@pytest_asyncio.fixture
async def seed_item_with_binding(async_session):
    """构造"商品 + 卡券绑定关系"的工厂夹具。

    参数：item_id、card_id、source（own/dock_l1/dock_l2）、dock_record_id。
    返回：(商品记录, 关联表记录) 元组。
    """
    from common.models.card_item_relation import CardItemRelation
    from common.models.xy_catalog_item import XYCatalogItem

    counter = {"n": 0}

    async def _factory(item_id: str | None = None, card_id: int = 1,
                       user_id: int = 1, account_pk: int | None = None,
                       source: str = "own", dock_record_id: int = 0):
        counter["n"] += 1
        item_id = item_id or f"ITEM{counter['n']:04d}"
        item = XYCatalogItem(
            owner_id=user_id,
            account_pk=account_pk,
            item_id=item_id,
            title=f"测试商品{counter['n']}",
            created_at=datetime(2026, 1, 1),
        )
        relation = CardItemRelation(
            user_id=user_id,
            card_id=card_id,
            item_id=item_id,
            source=source,
            dock_record_id=dock_record_id,
        )
        async_session.add_all([item, relation])
        await async_session.commit()
        await async_session.refresh(item)
        await async_session.refresh(relation)
        return item, relation

    return _factory


@pytest_asyncio.fixture
async def seed_account(async_session):
    """构造一个闲鱼账号（含 Cookie）的工厂夹具，供上下架服务测试使用。"""
    from common.models.xy_account import XYAccount

    counter = {"n": 0}

    async def _factory(owner_id: int = 1, account_id: str | None = None,
                       cookie: str | None = None, **kwargs):
        counter["n"] += 1
        account = XYAccount(
            owner_id=owner_id,
            account_id=account_id or f"ACC{counter['n']:04d}",
            cookie=cookie or "_m_h5_tk=faketoken123; other=1",
            login_method="qrcode",
            **kwargs,
        )
        async_session.add(account)
        await async_session.commit()
        await async_session.refresh(account)
        return account

    return _factory


# ---------------------------------------------------------------------------
# 外部依赖 Mock 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_im_sender():
    """Mock 闲鱼 IM 发送，断言发货内容/调用次数/模拟发送失败。"""
    return AsyncMock()


@pytest.fixture
def mock_task_queue():
    """Mock Redis 上下架任务队列（enqueue_stock_task），断言投递的 action 与载荷。"""
    return AsyncMock()


@pytest.fixture
def mock_notifier():
    """Mock 卖家通知渠道（notification-channels），断言告警内容。"""
    return AsyncMock()


# ---------------------------------------------------------------------------
# 时间相关夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def freeze_beijing_now(monkeypatch):
    """冻结北京时间（get_beijing_now_naive），用于断言 used_at 等时间字段。

    用法：frozen = freeze_beijing_now(被测模块对象)
    注意 patch 点是"使用方模块"中导入的名字，而非 time_utils 定义处。
    """
    frozen = datetime(2026, 1, 1, 12, 0, 0)

    def _freeze(module, attr: str = "get_beijing_now_naive") -> datetime:
        monkeypatch.setattr(module, attr, lambda: frozen, raising=False)
        return frozen

    return _freeze


# ---------------------------------------------------------------------------
# 服务包导入工具（处理 backend-web / websocket / scheduler 同名 app 包冲突）
# ---------------------------------------------------------------------------


@pytest.fixture
def service_importer():
    """按服务目录隔离导入其 ``app`` 包。

    三个后端服务的包名都是 ``app``，同一 pytest 进程内会冲突。
    本工具在导入前清理 sys.modules 中的 app* 并临时调整 sys.path，
    退出时恢复原状。用法::

        with service_importer("websocket") as imp:
            module = imp.import_module("app.services.xianyu.auto_delivery_handler")
    """

    @contextmanager
    def _importer(service_dir: str):
        path = str(ROOT / service_dir)
        saved = {k: v for k, v in sys.modules.items()
                 if k == "app" or k.startswith("app.")}
        for k in saved:
            sys.modules.pop(k)
        sys.path.insert(0, path)
        try:
            yield importlib
        finally:
            sys.path.remove(path)
            for k in list(sys.modules):
                if k == "app" or k.startswith("app."):
                    sys.modules.pop(k)
            sys.modules.update(saved)

    return _importer
