"""
卡密明细模型

功能：
1. 定义卡密明细表（xy_card_secrets），一分类（xy_cards.id）多条卡密
2. 支持文本卡密（content_type=0）与二维码图片卡密（content_type=1）
3. 库存 = 该分类下 status=0 的记录数；售出置 status=1 并记录订单号

对应方案：docs/card-secret-stock-plan.md §2
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base_class import Base


class CardSecret(Base):
    """卡密明细表 - 一分类多条卡密，单条状态可追溯"""

    __tablename__ = "xy_card_secrets"

    # 复合索引：库存统计与原子取货共用（card_id + status）
    # 复合索引：图片卡密导入按 (card_id, image_hash) 去重
    __table_args__ = (
        Index("idx_card_status", "card_id", "status"),
        Index("idx_card_hash", "card_id", "image_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键ID")
    card_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="关联卡券ID（卡密分类，xy_cards.id）")
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="所属用户ID")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="卡密内容：文本卡密存原文；图片卡密存相对URL")
    content_type: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
        comment="卡密形态：0=文本 1=二维码图片",
    )
    image_hash: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, comment="图片字节MD5（导入去重用；文本卡密为NULL）",
    )
    status: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
        comment="状态：0=可用 1=已用 2=作废",
    )
    order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="消费该卡密的订单号（追溯）")
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="使用时间（北京时间）")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
