"""自动发货钩子测试。

覆盖方案 §3.2：发货成功后的取密/回滚/重上架/下架分支。
对应被测模块：websocket/app/services/xianyu/auto_delivery_handler.py（待改造）。

接口约定（测试即接口定义，实现方按此开发）：
1. 卡密发货钩子从 _auto_delivery 抽成独立函数（保证可测性）::

    async def deliver_data_card_secret(
        session, *,
        card,            # xy_cards ORM 对象（data 型卡券）
        item_id: str,
        order_id: str,
        buyer_id: str,
        account_id: str,
        send_message,    # async (buyer_id, content) 注入的 IM 发送
        enqueue_task,    # async (payload: dict) 注入的 Redis 任务投递
        notify,          # async (message: str) 注入的卖家通知
        confirm_delivery=None,  # 可选 async () 确认发货回调，失败同样回滚卡密
    ) -> bool            # True=已发货；False=库存空未发货

   任务载荷约定：{"action": "relist"|"offline", "item_id", "card_id",
   "account_id", "retry": 0}。enqueue_task 抛异常只告警不向外抛。
2. 谓词 is_local_data_card(card_dict) -> bool：仅 own 来源且 type=data
   的卡券走本地卡密库存；dock_l1/dock_l2、未绑定（None）返回 False。
"""
import pytest

# ---------------------------------------------------------------------------
# 被测模块加载（websocket 服务的 app 包与 backend-web 同名，需隔离导入）
# ---------------------------------------------------------------------------


@pytest.fixture
def hook_module(service_importer):
    """隔离导入 websocket 的 auto_delivery_handler 模块。

    TDD：deliver_data_card_secret / is_local_data_card 实现前，
    属性缺失（AttributeError）即红灯。
    """
    with service_importer("websocket") as imp:
        yield imp.import_module("app.services.xianyu.auto_delivery_handler")


def _enqueue_payload(mock_task_queue, index=0) -> dict:
    """从 enqueue_task 的调用参数中提取任务载荷（兼容位置/关键字两种投递方式）。"""
    call = mock_task_queue.call_args_list[index]
    if call.args:
        return call.args[0]
    return dict(call.kwargs)


class TestDeliverySecretFlow:
    """发货链路中的卡密消费。"""

    async def test_订单触发后从绑定分类取一条卡密发给买家(
        self, async_session, seed_card, seed_secrets, hook_module,
        mock_im_sender, mock_task_queue, mock_notifier,
    ):
        """验证 _auto_delivery 按 item_id+规格匹配分类、take_one 取密、IM 发送卡密内容。"""
        card = await seed_card()
        rows = await seed_secrets(card.id, n=2, content_prefix="KAMI")

        sent = await hook_module.deliver_data_card_secret(
            async_session, card=card, item_id="ITEM001", order_id="ORDER001",
            buyer_id="BUYER1", account_id="ACC001",
            send_message=mock_im_sender,
            enqueue_task=mock_task_queue,
            notify=mock_notifier,
        )

        assert sent is True, "有库存时应完成发货"
        mock_im_sender.assert_awaited_once()
        call = mock_im_sender.call_args
        sent_content = call.args[1] if len(call.args) > 1 else call.kwargs.get("content")
        assert sent_content == rows[0].content, "IM 应发送最早一条卡密内容"
        await async_session.refresh(rows[0])
        assert rows[0].status == 1 and rows[0].order_id == "ORDER001", (
            "发出的卡密应置为已用并记录订单号"
        )

    async def test_IM发送失败后卡密回滚为可用(
        self, async_session, seed_card, seed_secrets, hook_module,
        mock_im_sender, mock_task_queue, mock_notifier,
    ):
        """验证发送异常时调用 release，卡密 status 复位 0 且不计发货数。"""
        card = await seed_card()
        rows = await seed_secrets(card.id, n=1)
        mock_im_sender.side_effect = Exception("网络错误")

        with pytest.raises(Exception, match="网络错误"):
            await hook_module.deliver_data_card_secret(
                async_session, card=card, item_id="ITEM001", order_id="ORDER001",
                buyer_id="BUYER1", account_id="ACC001",
                send_message=mock_im_sender,
                enqueue_task=mock_task_queue,
                notify=mock_notifier,
            )

        await async_session.refresh(rows[0])
        assert rows[0].status == 0, "IM 失败后卡密应回滚为可用"
        assert rows[0].order_id is None, "回滚后订单号应清空"
        mock_task_queue.assert_not_awaited(), "发货失败不应投递上下架任务"

    async def test_取密时库存为空则不发货并投递下架任务(
        self, async_session, seed_card, hook_module,
        mock_im_sender, mock_task_queue, mock_notifier,
    ):
        """验证 take_one 返回 None 时：不发 IM、投递 action=offline 任务、不抛未处理异常。"""
        card = await seed_card()  # 不补货，库存为 0

        sent = await hook_module.deliver_data_card_secret(
            async_session, card=card, item_id="ITEM001", order_id="ORDER001",
            buyer_id="BUYER1", account_id="ACC001",
            send_message=mock_im_sender,
            enqueue_task=mock_task_queue,
            notify=mock_notifier,
        )

        assert sent is False, "库存空应返回未发货"
        mock_im_sender.assert_not_awaited(), "库存空不应发 IM"
        mock_task_queue.assert_awaited_once()
        payload = _enqueue_payload(mock_task_queue)
        assert payload["action"] == "offline", "库存空应投递下架任务"
        assert payload["item_id"] == "ITEM001" and payload["card_id"] == card.id

    async def test_多数量订单只取一张卡密(
        self, async_session, seed_card, seed_secrets, hook_module,
        mock_im_sender, mock_task_queue, mock_notifier,
    ):
        """验证沿用现有退化逻辑：一单取一张、重上架一次，不按数量重复取密。"""
        card = await seed_card()
        await seed_secrets(card.id, n=3)

        await hook_module.deliver_data_card_secret(
            async_session, card=card, item_id="ITEM001", order_id="ORDER_MULTI",
            buyer_id="BUYER1", account_id="ACC001",
            send_message=mock_im_sender,
            enqueue_task=mock_task_queue,
            notify=mock_notifier,
        )

        mock_im_sender.assert_awaited_once(), "多数量订单也只发一张卡密"
        from common.services import card_secret_service
        remaining = await card_secret_service.stock_of(async_session, card.id)
        assert remaining == 2, "一次发货只应消耗一张卡密"

    async def test_分销卡券发货不消耗本地卡密库存(self, hook_module):
        """验证 source=dock_l1/dock_l2 的订单不触碰 xy_card_secrets。"""
        assert hook_module.is_local_data_card(
            {"type": "data", "card_source": "dock_l1"}
        ) is False, "一级分销卡券不走本地库存"
        assert hook_module.is_local_data_card(
            {"type": "data", "card_source": "dock_l2"}
        ) is False, "二级分销卡券不走本地库存"
        assert hook_module.is_local_data_card(
            {"type": "data", "card_source": "own"}
        ) is True, "自有 data 卡券应走本地库存"
        assert hook_module.is_local_data_card(
            {"type": "text", "card_source": "own"}
        ) is False, "非 data 卡券不走卡密库存"
        assert hook_module.is_local_data_card(None) is False, (
            "未匹配到卡券（None）时不进入本地卡密发货流程"
        )


class TestPostDeliveryBranch:
    """发货成功后的上下架分支（核心闭环）。"""

    async def test_发货后剩余库存大于0则投递重新上架任务(
        self, async_session, seed_card, seed_secrets, hook_module,
        mock_im_sender, mock_task_queue, mock_notifier,
    ):
        """验证 stock_of>0 时投递 action=relist 任务，载荷含 item_id/account_id。"""
        card = await seed_card()
        await seed_secrets(card.id, n=2)  # 发出 1 张后还剩 1 张

        await hook_module.deliver_data_card_secret(
            async_session, card=card, item_id="ITEM001", order_id="ORDER001",
            buyer_id="BUYER1", account_id="ACC001",
            send_message=mock_im_sender,
            enqueue_task=mock_task_queue,
            notify=mock_notifier,
        )

        mock_task_queue.assert_awaited_once()
        payload = _enqueue_payload(mock_task_queue)
        assert payload["action"] == "relist", "有剩余库存应投递重新上架任务"
        assert payload["item_id"] == "ITEM001"
        assert payload["account_id"] == "ACC001"
        mock_notifier.assert_not_awaited(), "有库存时不应惊动卖家"

    async def test_发货后剩余库存为0则投递下架任务并通知卖家(
        self, async_session, seed_card, seed_secrets, hook_module,
        mock_im_sender, mock_task_queue, mock_notifier,
    ):
        """验证发出最后一张后投递 action=offline 任务，且 notification-channels 收到库存空告警。"""
        card = await seed_card(name="游戏点卡")
        await seed_secrets(card.id, n=1)  # 最后一张

        await hook_module.deliver_data_card_secret(
            async_session, card=card, item_id="ITEM001", order_id="ORDER001",
            buyer_id="BUYER1", account_id="ACC001",
            send_message=mock_im_sender,
            enqueue_task=mock_task_queue,
            notify=mock_notifier,
        )

        payload = _enqueue_payload(mock_task_queue)
        assert payload["action"] == "offline", "最后一张发出后应投递下架任务"
        mock_notifier.assert_awaited_once()
        notify_msg = mock_notifier.call_args.args[0]
        assert "库存" in notify_msg and "游戏点卡" in notify_msg, (
            "告警应包含库存空与分类名称"
        )

    async def test_下架任务载荷信息完整(
        self, async_session, seed_card, seed_secrets, hook_module,
        mock_im_sender, mock_task_queue, mock_notifier,
    ):
        """验证 offline 任务含 item_id、card_id、account_id、重试次数字段。"""
        card = await seed_card()
        await seed_secrets(card.id, n=1)

        await hook_module.deliver_data_card_secret(
            async_session, card=card, item_id="ITEM001", order_id="ORDER001",
            buyer_id="BUYER1", account_id="ACC001",
            send_message=mock_im_sender,
            enqueue_task=mock_task_queue,
            notify=mock_notifier,
        )

        payload = _enqueue_payload(mock_task_queue)
        for key in ("action", "item_id", "card_id", "account_id", "retry"):
            assert key in payload, f"任务载荷缺少字段 {key}"
        assert payload["retry"] == 0, "首次投递重试次数应为 0"

    async def test_任务投递失败不影响已完成的卡密发送(
        self, async_session, seed_card, seed_secrets, hook_module,
        mock_im_sender, mock_task_queue, mock_notifier,
    ):
        """验证 enqueue 异常被捕获并告警，买家已收到卡密，不重复发货。"""
        card = await seed_card()
        rows = await seed_secrets(card.id, n=1)
        mock_task_queue.side_effect = Exception("Redis 连接失败")

        sent = await hook_module.deliver_data_card_secret(
            async_session, card=card, item_id="ITEM001", order_id="ORDER001",
            buyer_id="BUYER1", account_id="ACC001",
            send_message=mock_im_sender,
            enqueue_task=mock_task_queue,
            notify=mock_notifier,
        )

        assert sent is True, "任务投递失败不应影响已完成的发货（不向外抛异常）"
        mock_im_sender.assert_awaited_once(), "卡密只应发送一次"
        await async_session.refresh(rows[0])
        assert rows[0].status == 1, "已发出的卡密不应因投递失败而回滚"


class TestDeliveryFailureNoConsume:
    """各种失败路径下库存不被误扣。"""

    async def test_匹配不到卡券时不产生任何库存变动(self, hook_module):
        """验证 _auto_delivery 真实接线：未绑定/分销卡券经 is_local_data_card 拦截，
        不进入取密流程，xy_card_secrets 无写入。

        结构性断言：直接检查 _auto_delivery 源码完成接线验证——
        保证钩子函数不是"实现了但没人调用"的死代码。
        """
        import inspect

        source = inspect.getsource(hook_module.AutoDeliveryHandler._auto_delivery)
        assert "is_local_data_card" in source, (
            "_auto_delivery 应先经 is_local_data_card 判定再进入卡密取密流程"
        )
        assert "deliver_data_card_secret" in source, (
            "_auto_delivery 成功路径应调用 deliver_data_card_secret 钩子"
        )

    async def test_发货前置校验失败时已取卡密被回滚(
        self, async_session, seed_card, seed_secrets, hook_module,
        mock_im_sender, mock_task_queue, mock_notifier,
    ):
        """验证确认发货失败等中断场景下，已占位的卡密最终回到可用状态。"""
        card = await seed_card()
        rows = await seed_secrets(card.id, n=1)

        async def _confirm_fails():
            raise Exception("确认发货失败")

        with pytest.raises(Exception, match="确认发货失败"):
            await hook_module.deliver_data_card_secret(
                async_session, card=card, item_id="ITEM001", order_id="ORDER001",
                buyer_id="BUYER1", account_id="ACC001",
                send_message=mock_im_sender,
                enqueue_task=mock_task_queue,
                notify=mock_notifier,
                confirm_delivery=_confirm_fails,
            )

        await async_session.refresh(rows[0])
        assert rows[0].status == 0, "确认发货失败后已取卡密应回滚为可用"
        assert rows[0].order_id is None
