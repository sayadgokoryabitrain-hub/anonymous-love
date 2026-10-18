from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AuthError
from core.security import decode_token
from database.models import AdminUser

bearer = HTTPBearer(auto_error=False)
ROLE_RANK = {"support": 1, "moderator": 2, "administrator": 3, "superadmin": 4}


async def db_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def current_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(db_session),
) -> AdminUser:
    if credentials is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        payload = decode_token(credentials.credentials, "admin")
    except AuthError as exc:
        raise HTTPException(status_code=401, detail="unauthorized") from exc
    admin = await session.get(AdminUser, int(payload["sub"]))
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=401, detail="unauthorized")
    request.state.admin = admin
    return admin


def require_role(minimum: str):
    async def checker(admin: AdminUser = Depends(current_admin)) -> AdminUser:
        if ROLE_RANK.get(admin.role, 0) < ROLE_RANK[minimum]:
            raise HTTPException(status_code=403, detail="forbidden")
        return admin

    return checker
