"""初始化管理员账号（TDD 14.2 ``make init-admin``）。

系统里第一个 admin 无法通过接口创建（创建用户本身要 admin），故用本脚本引导：

    python -m app.scripts.init_admin --username admin
    # 未给 --password 时交互式输入，避免口令落到 shell 历史
    ADMIN_PASSWORD=... python -m app.scripts.init_admin --username admin   # 亦可走环境变量

已存在同名用户时默认跳过；加 ``--reset-password`` 可重置其口令。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from app.clients.mysql import dispose_engine, get_session_factory
from app.core.config import get_settings
from app.core.security import hash_password
from app.repositories.user import UserRepository


async def create_admin(username: str, password: str, display_name: str | None, *, reset: bool) -> int:
    """创建或重置管理员，返回进程退出码。"""
    settings = get_settings()
    print(f"[init-admin] 目标库 {settings.db_meta.safe_url}")

    factory = get_session_factory()
    async with factory() as session:
        repo = UserRepository(session)
        existing = await repo.get_by_username(username)

        if existing is not None:
            if not reset:
                print(f"[init-admin] 用户 {username} 已存在（role={existing.role}），跳过。")
                return 0
            await repo.update_password(existing, hash_password(password))
            await repo.update_role(existing, "admin")
            await repo.update_status(existing, True)
            await session.commit()
            print(f"[init-admin] 已重置 {username} 的口令并确保其为 admin/启用状态。")
            return 0

        user = await repo.create(
            username=username,
            password_hash=hash_password(password),
            role="admin",
            display_name=display_name or username,
        )
        await session.commit()
        print(f"[init-admin] 已创建管理员 {username}（id={user.id}）。")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="创建初始管理员账号")
    parser.add_argument("--username", default=os.getenv("ADMIN_USERNAME", "admin"))
    parser.add_argument("--display-name", default=os.getenv("ADMIN_DISPLAY_NAME"))
    parser.add_argument("--reset-password", action="store_true", help="已存在时重置口令")
    args = parser.parse_args()

    password = os.getenv("ADMIN_PASSWORD")
    if not password:
        password = getpass.getpass(f"请输入 {args.username} 的密码（6-32 字符）：")
        if password != getpass.getpass("请再次输入："):
            print("[init-admin] 两次输入不一致。", file=sys.stderr)
            return 2

    if not 6 <= len(password) <= 32:
        print("[init-admin] 密码长度需为 6-32 字符。", file=sys.stderr)
        return 2

    async def runner() -> int:
        try:
            return await create_admin(
                args.username, password, args.display_name, reset=args.reset_password
            )
        finally:
            await dispose_engine()

    return asyncio.run(runner())


if __name__ == "__main__":
    raise SystemExit(main())
