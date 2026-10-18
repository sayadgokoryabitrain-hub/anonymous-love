"""Create an administrator. Example: python -m scripts.create_admin --username admin --password 'long-password' --role superadmin"""

from __future__ import annotations

import argparse
import asyncio

from core.security import hash_password
from database.session import get_session_factory, init_database
from database.models import AdminUser


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--role", default="superadmin", choices=["superadmin", "administrator", "moderator", "support"])
    args = parser.parse_args()
    if len(args.password) < 10:
        raise SystemExit("Password must be at least 10 characters")
    await init_database()
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            AdminUser(
                username=args.username.strip().lower(),
                password_hash=hash_password(args.password),
                role=args.role,
                is_active=True,
            )
        )
        await session.commit()
    print(f"created {args.role} {args.username}")


if __name__ == "__main__":
    asyncio.run(main())
