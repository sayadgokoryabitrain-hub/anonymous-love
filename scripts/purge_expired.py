"""Delete expired chat excerpts and verification file references."""

from __future__ import annotations

import asyncio

from database.session import get_session_factory, init_database
from services.maintenance import run_maintenance


async def main() -> None:
    await init_database()
    factory = get_session_factory()
    async with factory() as session:
        result = await run_maintenance(session)
        await session.commit()
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
