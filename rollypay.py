"""Клиент RollyPay: создание платежа, статус, проверка подписи вебхука (docs.rollypay.io/api/*)."""
import hashlib, hmac, os, uuid
import httpx
import core as c

SHOP_ID = os.getenv("ROLLYPAY_SHOP_ID", "").strip()            # ID кассы (terminal_id); при auth по API-key не обязателен
API_KEY = os.getenv("ROLLYPAY_API_KEY", "").strip()
SIGNING_SECRET = os.getenv("ROLLYPAY_SIGNING_SECRET", "").strip()   # для проверки подписи вебхуков (выдаётся вместе с кассой)
API_URL = os.getenv("ROLLYPAY_API_URL", "https://api.rollypay.io").strip().rstrip("/")
TEST_MODE = os.getenv("ROLLYPAY_TEST", "0") == "1"             # true -> тестовые платежи (sandbox)
ENABLED = bool(API_KEY and SIGNING_SECRET)
METHODS = {"sbp": "sbp", "crypto": "crypto"}

class RollyError(Exception): pass

def _headers() -> dict:
    # X-Nonce: уникальный на каждый запрос (защита от повторов, 10 минут)
    return {"X-API-Key": API_KEY, "X-Nonce": str(uuid.uuid4()), "Content-Type": "application/json"}

async def _req(method: str, path: str, **kw) -> dict:
    try:
        r = await c._http.request(method, f"{API_URL}/api/v1{path}", headers=_headers(), timeout=20, **kw)
    except httpx.HTTPError as e:
        raise RollyError(f"сеть: {e!r}") from e
    if r.status_code >= 400:
        raise RollyError(f"{r.status_code}: {r.text[:300]}")
    return r.json()

async def create_payment(order_id: str, amount_rub: int, method: str, description: str, tg_id: int) -> dict:
    """Возвращает ответ API (payment_id, pay_url, ...)."""
    body = {"amount": f"{amount_rub:.2f}", "payment_currency": "RUB", "payment_method": METHODS[method],
            "order_id": order_id, "description": description[:200], "customer_id": str(tg_id),
            "metadata": {"tg_id": tg_id}}
    if SHOP_ID: body["terminal_id"] = SHOP_ID
    if TEST_MODE: body["test"] = True
    d = await _req("POST", "/payments", json=body)
    if not d.get("pay_url") or not d.get("payment_id"): raise RollyError(f"нет pay_url в ответе: {d}")
    return d

async def get_payment(payment_id: str) -> dict:
    return await _req("GET", f"/payments/{payment_id}")

def verify_signature(raw_body: bytes, signature: str, timestamp: str) -> bool:
    """HMAC-SHA256(signing_secret, f"{timestamp}.{raw_body}") в hex — сравнение с X-Signature за постоянное время."""
    if not (SIGNING_SECRET and signature and timestamp): return False
    mac = hmac.new(SIGNING_SECRET.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, signature.strip().lower())
