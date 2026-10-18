from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AdminAuditLog


async def audit(
    session: AsyncSession,
    admin_id: int | None,
    action: str,
    entity_type: str,
    entity_id: str | int,
    details: dict | None = None,
) -> None:
    session.add(
        AdminAuditLog(
            admin_id=admin_id,
            action=action[:64],
            entity_type=entity_type[:64],
            entity_id=str(entity_id)[:64],
            details=details,
        )
    )
