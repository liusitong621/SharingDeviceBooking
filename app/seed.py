"""
首次启动时的种子数据。

一条原则：**种子只在表为空时写入**，绝不覆盖已有数据。
否则每次重启都会把演示账号的密码重置回默认值——这在测试环境是方便，
在生产环境是漏洞（谁能保证没人把演示账号留着？）。
"""

from __future__ import annotations

import logging
import uuid

from app import db
from app.auth import hash_password

logger = logging.getLogger("app.seed")

# 三个演示账号。真实上线前必须删掉或改密——代码里只保留这一处，便于清理。
DEFAULT_USERS = [
    # (工号, 姓名, 部门, 角色, 初始密码)
    ("admin", "系统管理员", "设备科", "admin", "Admin@12345"),
    ("1001", "张伟", "研发一部", "employee", "Staff@12345"),
    ("1002", "李娜", "研发二部", "employee", "Staff@12345"),
]

DEMO_DEVICES = [
    # (资产编号, 名称, 型号, 位置, 状态)
    ("EQ-0001", "手持频谱分析仪", "Keysight N9340B", "A 座 3 层仪器间", "available"),
    ("EQ-0002", "热成像仪", "FLIR E8-XT", "A 座 3 层仪器间", "available"),
    ("EQ-0003", "便携式示波器", "Tektronix MDO3024", "B 座 1 层工具库", "available"),
]


async def seed_if_empty() -> None:
    users = await db.list_users(limit=1)
    if not users:
        for emp_no, name, dept, role, pwd in DEFAULT_USERS:
            await db.upsert_user(
                emp_no,
                name,
                hash_password(pwd),
                role=role,
                dept=dept,
                must_change_pwd=True,
            )
        logger.warning(
            "已写入 %d 个演示账号（admin / 1001 / 1002），初始密码见 app/seed.py，"
            "首次登录会强制改密",
            len(DEFAULT_USERS),
        )

    devices = await db.list_devices()
    if not devices:
        for asset_no, name, model, location, status in DEMO_DEVICES:
            await db.upsert_device(str(uuid.uuid4()), asset_no, name, model, location, status)
        logger.info("已写入 %d 台演示设备", len(DEMO_DEVICES))
