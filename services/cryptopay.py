from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from core.config import get_settings
from core.errors import AppError

logger = logging.getLogger(__name__)


def signature_for(token: str, body: bytes) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def signature_matches(body: bytes, header: str) -> bool:
    token = get_settings().crypto_pay_token
    if not token or not header:
        return False
    expected = signature_for(token, body)
    return hmac.compare_digest(expected, header.strip())


async def _call(method: str, payload: dict | None = None) -> dict | list:
    settings = get_settings()
    if not settings.crypto_pay_token:
        raise AppError("crypto_disabled", "pay.disabled")
    url = settings.crypto_pay_api.rstrip("/") + "/" + method
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                url,
                headers={"Crypto-Pay-API-Token": settings.crypto_pay_token},
                json=payload or {},
            )
    except httpx.HTTPError as exc:
        logger.warning("cryptopay transport failed: %s", exc.__class__.__name__)
        raise AppError("crypto_transport", "pay.fail") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise AppError("crypto_bad_response", "pay.fail") from exc
    if not data.get("ok"):
        logger.warning("cryptopay %s rejected", method)
        raise AppError("crypto_rejected", "pay.fail")
    return data.get("result")


async def create_invoice(*, amount: str, asset: str, description: str, payload: str) -> dict:
    result = await _call(
        "createInvoice",
        {
            "asset": asset,
            "amount": amount,
            "description": description[:1024],
            "payload": payload[:4096],
            "expires_in": 3600,
        },
    )
    if not isinstance(result, dict) or not result.get("invoice_id") or not result.get("bot_invoice_url"):
        raise AppError("crypto_bad_invoice", "pay.fail")
    return result


async def get_invoices(invoice_ids: list[str]) -> list[dict]:
    if not invoice_ids:
        return []
    result = await _call("getInvoices", {"invoice_ids": ",".join(invoice_ids[:100])})
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        items = result.get("items") or []
        return [item for item in items if isinstance(item, dict)]
    return []
