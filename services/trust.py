from __future__ import annotations

from core.clock import as_utc, utcnow
from database.models import User


def calculate_trust(user: User) -> int:
    created = as_utc(user.created_at) or utcnow()
    days = max(0, (utcnow() - created).days)
    score = 10
    if user.verification_status == "approved":
        score += 30
    score += min(20, days // 7)
    score -= 15 * (user.confirmed_violations or 0)
    score -= 2 * (user.warning_count or 0)
    if (user.confirmed_violations or 0) == 0 and days >= 30:
        score += 5
    if user.status == "restricted":
        score = min(score, 20)
    if user.status == "banned":
        score = 0
    return max(0, min(100, score))


def refresh_trust(user: User) -> int:
    user.trust_score = calculate_trust(user)
    user.updated_at = utcnow()
    return user.trust_score


def trust_level(score: int) -> str:
    if score < 25:
        return "low"
    if score < 50:
        return "new"
    return "ok"
