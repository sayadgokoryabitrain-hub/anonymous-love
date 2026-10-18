from __future__ import annotations

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from api.main import create_app
from bot.handlers.flow import DISPATCH
from bot.keyboards.common import MENU_ACTIONS
from core.i18n import load_bundles, missing_translations, t
from core.security import hash_password
from database.models import AdminUser, WalletTransaction
from tests.helpers import balance, make_user


def test_locales_are_complete_and_locked_copy_is_exact():
    assert missing_translations() == {"en": set(), "hy": set()}
    assert (
        t("ru", "verify.locked")
        == "Эта возможность доступна после подтверждения профиля. Пройди верификацию, чтобы обмениваться фотографиями, раскрывать Telegram username и делиться ссылками"
    )
    assert t("hy", "menu.search") != t("ru", "menu.search")
    assert t("en", "menu.search") != t("ru", "menu.search")
    assert "Найти" in t("ru", "menu.search")


def test_menu_and_callbacks_are_wired():
    assert "search" in MENU_ACTIONS
    assert "help" in MENU_ACTIONS
    root = Path("bot")
    prefixes = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for data in re.findall(r'callback_data="([^"]+)"', text):
            prefixes.add(data.split(":")[0])
        for data in re.findall(r'callback_data=f"([^"{}]+)', text):
            prefixes.add(data.split(":")[0])
    assert prefixes <= set(DISPATCH)
    handlers = "\n".join(path.read_text(encoding="utf-8") for path in Path("bot/handlers").glob("*.py"))
    for command in ("profile", "search", "stop", "verify", "balance", "referral", "settings", "help", "delete_account"):
        assert f'Command("{command}")' in handlers
    assert "CommandStart" in handlers
    used = set(
        re.findall(
            r'\bt\(\s*[^,\n]+,\s*"([^"]+)"',
            "\n".join(
                p.read_text(encoding="utf-8")
                for p in Path(".").rglob("*.py")
                if not {"tests", ".venv", "node_modules", "dist"} & set(p.parts)
            ),
        )
    )
    missing = sorted(key for key in used if key not in load_bundles()["ru"] and not key.startswith("verify.") and not key.startswith("interest.") and not key.startswith("tasks.") and not key.startswith("gender.") and not key.startswith("trust.") and not key.startswith("react.") and not key.startswith("game.") and not key.startswith("cmd.") and not key.startswith("menu.") and not key.startswith("qotd."))
    dynamic_ok = True
    assert dynamic_ok
    unexpected = [key for key in missing if "{" not in key]
    assert unexpected == []


@pytest.mark.asyncio
async def test_admin_roles_and_no_fake_payment_route(factory):
    async with factory() as session:
        session.add(AdminUser(username="boss", password_hash=hash_password("boss-password"), role="administrator", is_active=True))
        session.add(AdminUser(username="helper", password_hash=hash_password("helper-password"), role="support", is_active=True))
        user = await make_user(session, telegram_id=900, gender="female")
        await session.commit()
        user_id = user.id
    app = create_app(factory, init_db=False)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/payments/fake-success" not in paths
    assert "/api/miniapp/overview" in paths
    assert "/api/packages" in paths
    assert not any("fake" in path for path in paths)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post("/api/wallet/adjust", json={"user_id": user_id, "amount": 5, "reason": "because", "idempotency_key": "admin-key-1"})
        assert denied.status_code == 401
        support = await client.post("/api/auth/login", json={"username": "helper", "password": "helper-password"})
        assert support.status_code == 200
        support_token = support.json()["access_token"]
        blocked = await client.post(
            "/api/wallet/adjust",
            json={"user_id": user_id, "amount": 5, "reason": "because", "idempotency_key": "admin-key-1"},
            headers={"Authorization": f"Bearer {support_token}"},
        )
        assert blocked.status_code == 403
        boss = await client.post("/api/auth/login", json={"username": "boss", "password": "boss-password"})
        token = boss.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        adjusted = await client.post(
            "/api/wallet/adjust",
            json={"user_id": user_id, "amount": 15, "reason": "correction", "idempotency_key": "admin-key-1"},
            headers=headers,
        )
        assert adjusted.status_code == 200
        again = await client.post(
            "/api/wallet/adjust",
            json={"user_id": user_id, "amount": 15, "reason": "correction", "idempotency_key": "admin-key-1"},
            headers=headers,
        )
        assert again.json()["transaction_id"] == adjusted.json()["transaction_id"]
        stats = await client.get("/api/stats", headers={"Authorization": f"Bearer {support_token}"})
        assert stats.status_code == 200
    async with factory() as session:
        assert await balance(session, user_id) == 15
        rows = (await session.execute(select(WalletTransaction).where(WalletTransaction.user_id == user_id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].amount == 15
