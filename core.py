"""Конфиг, БД, клиент панели H1Cloud и бизнес-логика (триал, устройства по x-hwid, промокоды, уведомления).
В БД: key_id = имя клиента в панели, access_url = sub_url панели (наружу не отдаётся)."""
import asyncio, hashlib, json, logging, math, os, re, secrets, sqlite3, tempfile, time
from datetime import datetime
from urllib.parse import quote, urljoin
import httpx

BOT_TOKEN = os.environ["BOT_TOKEN"]
def _norm_url(u: str) -> str:
    """Без схемы (xxxx.example.com) подставляем https://; хвостовой / убираем."""
    u = u.strip().rstrip("/")
    return u if re.match(r"^https?://", u, re.I) else "https://" + u

PUBLIC_URL = _norm_url(os.environ["PUBLIC_URL"])
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
_bc = os.getenv("BACKUP_CHAT_ID", "").strip()
BACKUP_CHAT_ID = int(_bc) if _bc.lstrip("-").isdigit() else (min(ADMINS) if ADMINS else 0)
BACKUP_HOURS = float(os.getenv("BACKUP_HOURS", "6"))
TRIAL_HOURS = int(os.getenv("TRIAL_HOURS", "24"))
GB = 1024 ** 3
TRIAL_GB = float(os.getenv("TRIAL_GB", "3"))
TRIAL_BYTES = int(TRIAL_GB * GB)
DEFAULT_DEVICES = max(1, int(os.getenv("DEFAULT_DEVICES", "1")))

BRAND = os.getenv("BRAND", "HUTZ Vpn")
TAGLINE = os.getenv("TAGLINE", "Безопасное и быстрое подключение")
SUPPORT_URL = os.getenv("SUPPORT_BOT_URL", "https://t.me/hutzvpnbot").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/hutzvpn").strip()
PRIVACY_URL = os.getenv("PRIVACY_URL", "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99").strip()
TERMS_URL = os.getenv("TERMS_URL", "https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15").strip()
LOCATION = os.getenv("LOCATION", "Европа").strip()

# ---------- витрина: пакеты устройств × срок ----------
PACKS = {1: {1: 129, 3: 329, 6: 649}, 3: {1: 189, 3: 499, 6: 999}}   # устройств -> {месяцев: цена ₽}
PACK_TITLE = {1: "👤 На 1 устройство", 3: "👥 На 3 устройства"}
MONTH_DAYS = 30
EXTRA_DEVICE_RUB, EXTRA_DEVICE_DAYS = 59, 30
PAID_GB = float(os.getenv("PAID_TRAFFIC_GB", "1000"))   # лимит трафика платной подписки (по промокоду)
SHOP_URL = os.getenv("SHOP_URL", "").strip()              # ссылка на площадку, где продаются коды

def traffic_text() -> str:
    """Честное описание лимита трафика платной подписки (берётся из PAID_TRAFFIC_GB)."""
    return f"{PAID_GB:g} ГБ трафика"

def months_text(m: int) -> str:
    return f"{m} " + ("месяц" if m == 1 else "месяца" if m < 5 else "месяцев")

# ключ "d{устройств}m{месяцев}" -> параметры тарифа (используется и в витрине, и в генераторе промокодов)
PLANS = {f"d{d}m{m}": {"key": f"d{d}m{m}", "devices": d, "months": m, "days": m * MONTH_DAYS, "price": p,
                       "label": f"{d} устр. · {months_text(m)}"}
         for d, ms in PACKS.items() for m, p in ms.items()}

NOTIFY_EXPIRY_HOURS = float(os.getenv("NOTIFY_EXPIRY_HOURS", "24"))     # напоминание платным пользователям
NOTIFY_TRIAL_HOURS = float(os.getenv("NOTIFY_TRIAL_HOURS", "2"))         # для 24-часового теста — ближе к концу
NOTIFY_TRAFFIC_PCT = float(os.getenv("NOTIFY_TRAFFIC_PCT", "80"))

H1_BASES = [b.rstrip("/") for b in (os.environ["H1_API_URL"], os.getenv("H1_API_FALLBACK", "")) if b.strip()]
H1_TOKEN = os.environ["H1_API_TOKEN"]
H1_DEVICE_LIMIT = int(os.getenv("H1_DEVICE_LIMIT", "0"))
H1_INBOUNDS = [x.strip() for x in os.getenv("H1_INBOUND_IDS", "").split(",") if x.strip()]
H1_CHANNELS = [x.strip() for x in os.getenv("H1_CHANNELS", "").split(",") if x.strip()]

# ---------- БД ----------
def _resolve_db_path() -> str:
    env_p = os.getenv("DB_PATH", "").strip()
    if env_p:
        return env_p
    for d in ("/data", "/var/data", "/storage"):
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return os.path.join(d, "vpn.db")
    return "vpn.db"

DB_PATH = _resolve_db_path()
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)   # папка под БД (например /data/vpn.db)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA busy_timeout=5000")

def _init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      tg_id INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL,
      hwid TEXT,                           -- устарело: мигрирует в devices
      key_id TEXT, access_url TEXT, plan TEXT DEFAULT 'trial',
      limit_bytes INTEGER DEFAULT 0, expires_at INTEGER DEFAULT 0,
      status TEXT DEFAULT 'new',           -- new | active | expired | banned
      created_at INTEGER);
    CREATE TABLE IF NOT EXISTS hwids(hwid TEXT PRIMARY KEY, tg_id INTEGER, used_at INTEGER);
    CREATE TABLE IF NOT EXISTS device_info(      -- модель/ОС устройства по x-hwid (для админки)
      hwid TEXT PRIMARY KEY, tg_id INTEGER, model TEXT, os TEXT, first_seen INTEGER, last_seen INTEGER);
    CREATE TABLE IF NOT EXISTS promo_codes(
      code TEXT PRIMARY KEY, days INTEGER NOT NULL, devices INTEGER NOT NULL,
      is_used INTEGER DEFAULT 0, used_by INTEGER, created_at INTEGER, used_at INTEGER,
      kind TEXT DEFAULT 'days', discount_pct INTEGER DEFAULT 0,
      max_uses INTEGER DEFAULT 1, uses_count INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS promo_uses(
      code TEXT NOT NULL, tg_id INTEGER NOT NULL, used_at INTEGER,
      PRIMARY KEY (code, tg_id));
    CREATE TABLE IF NOT EXISTS payments(
      order_id TEXT PRIMARY KEY, tg_id INTEGER NOT NULL, plan_key TEXT NOT NULL,
      days INTEGER NOT NULL, devices INTEGER NOT NULL, amount INTEGER NOT NULL, method TEXT,
      status TEXT NOT NULL DEFAULT 'pending',      -- pending | paid | failed
      rp_payment_id TEXT, pay_url TEXT, created_at INTEGER, paid_at INTEGER);
    """)
    for _ddl in ("username TEXT", "devices TEXT DEFAULT '[]'", "device_limit INTEGER DEFAULT 1",
                 "notified_exp INTEGER DEFAULT 0", "notified_traffic INTEGER DEFAULT 0",
                 "discount_pct INTEGER DEFAULT 0", "discount_code TEXT"):
        try: db.execute(f"ALTER TABLE users ADD COLUMN {_ddl}")   # автомиграция старых БД
        except sqlite3.OperationalError: pass
    for _ddl in ("kind TEXT DEFAULT 'days'", "discount_pct INTEGER DEFAULT 0",
                 "max_uses INTEGER DEFAULT 1", "uses_count INTEGER DEFAULT 0"):
        try: db.execute(f"ALTER TABLE promo_codes ADD COLUMN {_ddl}")
        except sqlite3.OperationalError: pass
    db.execute("UPDATE promo_codes SET uses_count=1 WHERE is_used=1 AND (uses_count IS NULL OR uses_count=0)")
    db.execute("UPDATE promo_codes SET max_uses=1 WHERE max_uses IS NULL OR max_uses<1")
    db.execute("UPDATE promo_codes SET kind='days' WHERE kind IS NULL OR kind=''")
    for _r in db.execute("SELECT tg_id,hwid,devices FROM users WHERE hwid IS NOT NULL AND hwid!=''").fetchall():
        try: _devs = json.loads(_r["devices"] or "[]")
        except ValueError: _devs = []
        if _r["hwid"] not in _devs: _devs.append(_r["hwid"])
        db.execute("UPDATE users SET devices=?, hwid=NULL WHERE tg_id=?", (json.dumps(_devs), _r["tg_id"]))
    db.commit()

_init_db()

def backup_db_bytes() -> bytes:
    """Создаёт консистентный снимок SQLite (безопасно для WAL) и возвращает его байтами."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp)
        try:
            db.backup(dst)
        finally:
            dst.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

async def restore_db_bytes(data: bytes) -> dict:
    """Проверяет загруженный файл SQLite и атомарно заливает его поверх текущей БД."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        src_conn = sqlite3.connect(tmp)
        try:
            tables = {r[0] for r in src_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "users" not in tables:
                raise ValueError("В файле нет таблицы users — это не бэкап бота.")
            async with _lock:
                src_conn.backup(db)
                _init_db()
                _h1_miss_cache.clear()
        finally:
            src_conn.close()
        return {
            "users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "active": db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0],
            "payments": db.execute("SELECT COUNT(*) FROM payments WHERE status='paid'").fetchone()[0],
            "promos": db.execute("SELECT COUNT(*) FROM promo_codes WHERE is_used=0").fetchone()[0],
        }
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

def now() -> int: return int(time.time())
def get_user(tg_id): return db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
def sub_url(u) -> str: return f"{PUBLIC_URL}/sub/{u['token']}"

def devices_of(u) -> list[str]:
    try: return list(json.loads(u["devices"] or "[]"))
    except ValueError: return []

def ensure_user(tg_id, username=None):
    u = get_user(tg_id)
    if not u:
        db.execute("INSERT INTO users(tg_id,token,created_at,username,device_limit) VALUES(?,?,?,?,?)",
                   (tg_id, secrets.token_urlsafe(16), now(), username, DEFAULT_DEVICES))
        db.commit()
    elif username and u["username"] != username:
        db.execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id)); db.commit()
    return get_user(tg_id)

# ---------- панель H1 ----------
class H1Error(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"H1 {status}: {text}")
        self.status = status

_http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=True)
RETRIES, RETRY_PAUSE = 3, 1.5                 # попыток на запрос и пауза между ними, сек
RETRY_STATUS = {429, 500, 502, 503, 504}      # временные ответы панели — повторяем
T_READ, T_WRITE = 15, 30                      # таймауты: чтение 15 с; создание/изменение — до 30 с (по доке H1)

# --- уведомление админов о сбоях (хук ставит bot.py; не чаще раза в 5 минут на ключ) ---
alert_hook = None
_alert_at: dict[str, float] = {}

def alert(key: str, text: str):
    t = time.time()
    if not alert_hook or t - _alert_at.get(key, 0) < 300: return
    _alert_at[key] = t
    try: asyncio.get_running_loop().create_task(alert_hook(text))
    except RuntimeError: pass

def _path(name: str) -> str: return f"/clients/{quote(name, safe='')}"
def _cl(d: dict) -> dict: return d.get("client", d)

async def _h1(method: str, path: str, **kw) -> dict:
    """Запрос к панели: до RETRIES попыток при сетевых сбоях/таймаутах/5xx; на запасной адрес — при сетевой ошибке.
    Повтор POST /create безопасен: «уже существует» (409) обрабатывается в h1_create."""
    timeout = T_READ if method == "GET" else T_WRITE
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        for base in H1_BASES:
            try:
                r = await _http.request(method, base + path, headers={"Authorization": f"Bearer {H1_TOKEN}"},
                                        timeout=timeout, **kw)
            except httpx.TransportError as e:
                last = e
                continue
            if r.status_code in RETRY_STATUS:
                last = H1Error(r.status_code, "временная ошибка панели")
                continue
            if r.status_code >= 400:
                try: msg = r.json().get("error") or r.text
                except Exception: msg = r.text
                if r.status_code in (401, 403):
                    alert("h1auth", f"🔑 Панель H1 отклонила токен ({r.status_code}). Проверьте H1_API_TOKEN в .env.")
                raise H1Error(r.status_code, str(msg)[:300])
            return r.json() if r.content else {}
        if attempt < RETRIES:
            logging.warning("H1 %s %s: попытка %d/%d не удалась (%r), повтор", method, path, attempt, RETRIES, last)
            await asyncio.sleep(RETRY_PAUSE)
    alert("h1down", f"🔴 Панель H1 недоступна: {method} {path} — {last!r}. Выдача и продление доступа не работают.")
    raise last or RuntimeError("H1_API_URL не задан")

async def h1_get(name: str) -> dict: return _cl(await _h1("GET", _path(name)))
async def h1_patch(name: str, body: dict) -> dict: return _cl(await _h1("PATCH", _path(name), json=body))

async def h1_delete(name: str):
    try: await _h1("DELETE", _path(name))
    except H1Error as e:
        if e.status != 404: raise

async def h1_create(name: str, expires_at: int, gb: float) -> dict:
    body = {"name": name, "expires_at": expires_at,
            "days": max(1, math.ceil((expires_at - now()) / 86400)),  # на случай bad_days на старых панелях
            "traffic_limit_gb": gb, "device_limit": H1_DEVICE_LIMIT}
    if H1_INBOUNDS or H1_CHANNELS:
        body["manual"] = True
        if H1_INBOUNDS: body["inbound_ids"] = H1_INBOUNDS
        if H1_CHANNELS: body["channels"] = H1_CHANNELS
    try:
        return _cl(await _h1("POST", "/create", json=body))
    except H1Error as e:
        if e.status != 409: raise
        # клиент уже есть (повтор после таймаута / остаток от тестов) — приводим к нужным параметрам
        return await h1_patch(name, {"expires_at": expires_at, "traffic_limit_gb": gb,
                                     "device_limit": H1_DEVICE_LIMIT})

async def _sub_of(name: str, cl: dict) -> str:
    url = cl.get("sub_url") or cl.get("subscription_url")
    if not url:
        full = await h1_get(name)
        url = full.get("sub_url") or full.get("subscription_url")
    if not url: raise H1Error(502, "панель не вернула sub_url")
    return urljoin(H1_BASES[0] + "/", url)

async def usage(key_id) -> int:
    cl = await h1_get(key_id)
    return int(float(cl.get("traffic_used_gb") or 0) * GB)

_h1_miss_cache: dict[int, float] = {}   # tg_id -> ts последнего 404 в панели (чтобы не дёргать H1 на каждый клик)
_H1_MISS_TTL = 300

def _parse_ts(val) -> int:
    if val is None or val == "": return 0
    if isinstance(val, (int, float)):
        v = int(val)
        return v // 1000 if v > 10**12 else v
    s = str(val).strip()
    if s.isdigit():
        v = int(s)
        return v // 1000 if v > 10**12 else v
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0

def _extract_clients(resp) -> list[dict]:
    if isinstance(resp, list): return [x for x in resp if isinstance(x, dict)]
    if isinstance(resp, dict):
        for k in ("clients", "items", "data", "results"):
            if isinstance(resp.get(k), list):
                return [x for x in resp[k] if isinstance(x, dict)]
    return []

def _upsert_from_h1_client(tg_id: int, cl: dict) -> str:
    """Создаёт или восстанавливает пользователя в локальной БД по данным клиента из H1Cloud.
    Не затирает уже активных/забаненных пользователей и сохраняет привязанные устройства + лимит устройств."""
    name = cl.get("name") or f"tg{tg_id}"
    exp = _parse_ts(cl.get("expires_at") or cl.get("expire_at") or cl.get("expiry_time"))
    gb = float(cl.get("traffic_limit_gb") or 0)
    limit_bytes = int(gb * GB) if gb > 0 else TRIAL_BYTES
    raw_url = cl.get("sub_url") or cl.get("subscription_url") or ""
    url = urljoin(H1_BASES[0] + "/", raw_url) if raw_url else ""
    t = now()
    disabled = cl.get("enabled") is False or cl.get("active") is False or cl.get("status") in ("disabled", "expired", "banned")
    status = "active" if (exp > t and not disabled) else "expired"
    plan = "paid" if (gb > TRIAL_GB + 0.1 or (exp - t) > TRIAL_HOURS * 3600 + 3600) else "trial"

    u = get_user(tg_id)
    if not u:
        restored_devs = [
            r[0] for r in db.execute(
                "SELECT hwid FROM device_info WHERE tg_id=? ORDER BY last_seen DESC", (tg_id,)
            ).fetchall()
        ]
        if not restored_devs:
            restored_devs = [
                r[0] for r in db.execute(
                    "SELECT hwid FROM hwids WHERE tg_id=? ORDER BY used_at DESC", (tg_id,)
                ).fetchall()
            ]
        paid_rows = db.execute(
            "SELECT devices FROM payments WHERE tg_id=? AND status='paid' AND plan_key!='test1'", (tg_id,)
        ).fetchall()
        extra_slots = db.execute(
            "SELECT COUNT(*) FROM payments WHERE tg_id=? AND status='paid' AND plan_key='x'", (tg_id,)
        ).fetchone()[0]
        max_paid_devs = max([r[0] for r in paid_rows if r[0] and r[0] > 0] or [0])
        restored_limit = max(DEFAULT_DEVICES, len(restored_devs), max_paid_devs + extra_slots)
        db.execute(
            "INSERT INTO users(tg_id,token,key_id,access_url,plan,limit_bytes,expires_at,status,created_at,device_limit,devices) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tg_id, secrets.token_urlsafe(16), name, url, plan, limit_bytes, exp, status, t,
             restored_limit, json.dumps(restored_devs[:restored_limit])))
        return "created"
    if u["status"] == "banned":
        return "skipped"
    if u["status"] == "new" or not u["key_id"] or (status == "active" and exp > (u["expires_at"] or 0)):
        cur_devs = devices_of(u)
        if not cur_devs:
            cur_devs = [
                r[0] for r in db.execute(
                    "SELECT hwid FROM device_info WHERE tg_id=? ORDER BY last_seen DESC", (tg_id,)
                ).fetchall()
            ]
        cur_limit = max(u["device_limit"] or DEFAULT_DEVICES, len(cur_devs))
        db.execute(
            "UPDATE users SET key_id=?, access_url=COALESCE(NULLIF(?,''),access_url), plan=?, "
            "limit_bytes=?, expires_at=?, status=?, device_limit=?, devices=? WHERE tg_id=?",
            (name, url, plan, limit_bytes, exp, status, cur_limit, json.dumps(cur_devs[:cur_limit]), tg_id))
        return "updated"
    return "skipped"

async def sync_from_h1() -> dict:
    """Подтягивает всех клиентов вида tg<id> из панели H1Cloud в локальную БД (чтобы админка не была пустой после пересборки)."""
    res = {"created": 0, "updated": 0, "skipped": 0, "total": 0}
    try:
        raw = await _h1("GET", "/clients")
    except H1Error as e:
        logging.warning("sync_from_h1: панель ответила %s", e)
        return res
    clients = _extract_clients(raw)
    res["total"] = len(clients)
    async with _lock:
        for cl in clients:
            name = str(cl.get("name") or "")
            m = re.fullmatch(r"tg(\d+)", name)
            if not m: continue
            tg_id = int(m.group(1))
            if not (cl.get("sub_url") or cl.get("subscription_url")):
                try: cl = await h1_get(name)
                except Exception: pass
            act = _upsert_from_h1_client(tg_id, cl)
            res[act] += 1
            _h1_miss_cache.pop(tg_id, None)
        db.commit()
    return res

async def _sync_user_from_h1_nolock(tg_id: int, force: bool = False):
    """Проверяет, есть ли клиент tg<tg_id> уже в H1Cloud. Вызывать под _lock.
    Если есть — восстанавливает реальную подписку в БД и блокирует повторный триал."""
    u = get_user(tg_id)
    if not u or u["status"] != "new":
        return u
    t = time.time()
    if not force and t - _h1_miss_cache.get(tg_id, 0) < _H1_MISS_TTL:
        return u
    name = f"tg{tg_id}"
    try:
        cl = await h1_get(name)
    except H1Error as e:
        if e.status == 404:
            _h1_miss_cache[tg_id] = t
            return u
        raise
    _upsert_from_h1_client(tg_id, cl)
    db.commit()
    _h1_miss_cache.pop(tg_id, None)
    return get_user(tg_id)

async def sync_user_from_h1(tg_id: int, force: bool = False):
    async with _lock:
        return await _sync_user_from_h1_nolock(tg_id, force=force)

_FWD_REQ = ("x-hwid", "user-agent", "x-device-os", "x-ver-os", "x-device-model", "accept-language")
_FWD_RESP = {"subscription-userinfo", "profile-title", "profile-update-interval", "profile-web-page-url",
             "support-url", "announce", "announce-url", "routing", "content-disposition", "content-type"}

async def fetch_sub(u, req_headers) -> tuple[bytes, dict]:
    """Забирает подписку с панели (sub_url не светим пользователю) и возвращает тело + заголовки."""
    fwd = {k: req_headers[k] for k in _FWD_REQ if k in req_headers}
    r = await _http.get(u["access_url"], headers=fwd, timeout=T_READ)
    if r.status_code != 200:
        raise H1Error(r.status_code, "подписка панели недоступна")
    return r.content, {k.lower(): v for k, v in r.headers.items() if k.lower() in _FWD_RESP}

# ---------- логика ----------
_lock = asyncio.Lock()

async def resolve_sub(token: str, hwid: str, model: str = "", os_name: str = ""):
    """Вызывается при каждом запросе подписки из Happ. Возвращает (статус, user).
    model / os_name — заголовки x-device-model / x-device-os: сохраняются для админки."""
    async with _lock:
        u = db.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
        if not u: return "not_found", None
        if u["status"] in ("banned", "expired"): return u["status"], u
        devs = devices_of(u)

        if u["status"] == "new":  # первое подключение = сначала сверяем с H1Cloud (анти-абуз после пересборки)
            try:
                u = await _sync_user_from_h1_nolock(u["tg_id"], force=True)
            except Exception:
                logging.exception("sync_user_from_h1 failed for %s", u["tg_id"])
            if u["status"] in ("banned", "expired"):
                return u["status"], u
        if u["status"] == "new":  # точно новый клиент — запускаем триал
            owner = db.execute("SELECT tg_id FROM hwids WHERE hwid=?", (hwid,)).fetchone()
            if owner and owner["tg_id"] != u["tg_id"]: return "hwid_used", u   # анти-абуз триала
            name, exp = f"tg{u['tg_id']}", now() + TRIAL_HOURS * 3600
            cl = await h1_create(name, exp, TRIAL_GB)
            url = await _sub_of(name, cl)
            db.execute("UPDATE users SET key_id=?,access_url=?,limit_bytes=?,expires_at=?,status='active',"
                       "notified_exp=0,notified_traffic=0 WHERE tg_id=?",
                       (name, url, TRIAL_BYTES, exp, u["tg_id"]))
            _h1_miss_cache.pop(u["tg_id"], None)

        if hwid not in devs:
            if len(devs) >= (u["device_limit"] or 1):
                db.commit()
                return "device_limit", u
            devs.append(hwid)
            db.execute("UPDATE users SET devices=? WHERE tg_id=?", (json.dumps(devs), u["tg_id"]))
            db.execute("INSERT OR IGNORE INTO hwids VALUES(?,?,?)", (hwid, u["tg_id"], now()))
        _touch_device(hwid, u["tg_id"], model, os_name)
        db.commit()
        return "ok", get_user(u["tg_id"])

def _touch_device(hwid: str, tg_id: int, model: str, os_name: str):
    """Запоминает модель/ОС устройства (пустые значения не затирают уже известные)."""
    t, model, os_name = now(), (model or "").strip()[:60], (os_name or "").strip()[:30]
    db.execute("INSERT INTO device_info(hwid,tg_id,model,os,first_seen,last_seen) VALUES(?,?,?,?,?,?) "
               "ON CONFLICT(hwid) DO UPDATE SET tg_id=excluded.tg_id, last_seen=excluded.last_seen, "
               "model=COALESCE(NULLIF(excluded.model,''),model), os=COALESCE(NULLIF(excluded.os,''),os)",
               (hwid, tg_id, model, os_name, t, t))

def _hh(hwid: str) -> str:
    """Короткий отпечаток hwid для callback_data (лимит 64 байта)."""
    return hashlib.sha1(hwid.encode()).hexdigest()[:8]

def device_list(u) -> list[dict]:
    """Привязанные устройства пользователя: [{hwid, h, model, os}], в порядке привязки."""
    out = []
    for hw in devices_of(u):
        r = db.execute("SELECT model,os FROM device_info WHERE hwid=?", (hw,)).fetchone()
        out.append({"hwid": hw, "h": _hh(hw), "model": (r["model"] if r else "") or "", "os": (r["os"] if r else "") or ""})
    return out

def unbind_device(tg_id: int, h: str) -> dict | None:
    """Отвязывает одно устройство по отпечатку h. Лимит устройств (оплаченные слоты) не меняется.
    Возвращает {hwid, model, os} отвязанного устройства или None, если его уже нет."""
    u = get_user(tg_id)
    if not u: return None
    devs = device_list(u)
    d = next((x for x in devs if x["h"] == h), None)
    if not d: return None
    left = [x["hwid"] for x in devs if x["h"] != h]
    db.execute("UPDATE users SET devices=? WHERE tg_id=?", (json.dumps(left), tg_id))
    db.commit()
    return d

async def _recreate(u, exp: int, gb: float | None = None):
    """Клиента нет в панели (удалили руками / user_not_found) — создаём заново до срока exp и обновляем sub_url в БД.
    Лимит трафика — прежний (или gb, если задан). Вызывать под _lock. Счётчик трафика у нового клиента с нуля."""
    name = u["key_id"] or f"tg{u['tg_id']}"
    cl = await h1_create(name, exp, u["limit_bytes"] / GB if gb is None else gb)
    url = await _sub_of(name, cl)
    db.execute("UPDATE users SET key_id=?,access_url=?,notified_traffic=0 WHERE tg_id=?", (name, url, u["tg_id"]))
    db.commit()
    logging.warning("клиент %s отсутствовал в панели — пересоздан (до %s)", name, exp)

async def restore_client(tg_id: int, stale_url: str | None = None):
    """Автовосстановление активного пользователя на оставшийся срок. Возвращает свежую строку user
    или None, если пользователь не активен / срок уже вышел. stale_url защищает от двойного пересоздания
    при параллельных запросах: если sub_url в БД уже обновили — просто возвращаем его."""
    async with _lock:
        u = get_user(tg_id)
        if not u or u["status"] != "active" or u["expires_at"] <= now(): return None
        if stale_url is not None and u["access_url"] != stale_url: return u
        await _recreate(u, u["expires_at"])
        return get_user(tg_id)

async def fetch_sub_restoring(u, req_headers):
    """Забирает подписку; если панель отвечает 404 (активный в БД клиент пропал из панели) — пересоздаёт клиента
    и пробует ещё раз. Возвращает (body, headers, user) или None, если восстановить нельзя (срок вышел)."""
    try:
        body, hdrs = await fetch_sub(u, req_headers)
        return body, hdrs, u
    except H1Error as e:
        if e.status != 404: raise
    u = await restore_client(u["tg_id"], u["access_url"])
    if not u: return None
    body, hdrs = await fetch_sub(u, req_headers)
    return body, hdrs, u

async def expire_due() -> list[int]:
    done = []
    async with _lock:
        for u in db.execute("SELECT * FROM users WHERE status='active' AND expires_at<=?", (now(),)).fetchall():
            try: await h1_delete(u["key_id"])
            except Exception: logging.exception("не удалось удалить клиента %s в панели при истечении (срок в панели истечёт сам)", u["key_id"])
            db.execute("UPDATE users SET status='expired' WHERE tg_id=?", (u["tg_id"],))
            done.append(u["tg_id"])
        db.commit()
    return done

# --- внутренние операции (вызывать под _lock) ---
async def _grant_nolock(u, days: float, gb: float, device_limit: int | None = None):
    """Выдаёт доступ «с нуля»: старый клиент удаляется, создаётся новый. device_limit=None — лимит устройств не трогаем;
    иначе выставляем его и обрезаем список привязанных устройств с конца."""
    tg_id = u["tg_id"]
    name, exp = f"tg{tg_id}", now() + int(days * 86400)
    if u["key_id"]: await h1_delete(u["key_id"])
    cl = await h1_create(name, exp, gb)
    url = await _sub_of(name, cl)
    db.execute("UPDATE users SET key_id=?,access_url=?,plan='paid',limit_bytes=?,expires_at=?,"
               "status='active',notified_exp=0,notified_traffic=0 WHERE tg_id=?",
               (name, url, int(gb * GB), exp, tg_id))
    if device_limit is not None:
        db.execute("UPDATE users SET device_limit=?, devices=? WHERE tg_id=?",
                   (device_limit, json.dumps(devices_of(u)[:device_limit]), tg_id))
    db.commit()

async def _extend_nolock(u, hours: float, gb: float | None = None):
    """Продлевает активного пользователя. gb — если задан, заодно меняет лимит трафика (триал → платный)."""
    exp = u["expires_at"] + int(hours * 3600)
    body = {"expires_at": exp}                    # абсолютная дата — повтор безопасен
    if gb is not None: body["traffic_limit_gb"] = gb
    try:
        await h1_patch(u["key_id"], body)
    except H1Error as e:
        if e.status != 404: raise
        await _recreate(u, exp, gb)               # клиента удалили в панели — создаём заново сразу на новый срок
    db.execute("UPDATE users SET expires_at=?,notified_exp=0,notified_traffic=0 WHERE tg_id=?", (exp, u["tg_id"]))
    if gb is not None:
        db.execute("UPDATE users SET limit_bytes=? WHERE tg_id=?", (int(gb * GB), u["tg_id"]))
    db.commit()

async def grant(tg_id: int, days: float, gb: float, devices: int | None = None):
    async with _lock:
        await _grant_nolock(ensure_user(tg_id), days, gb, devices)

async def extend(tg_id: int, hours: float) -> bool:
    async with _lock:
        u = get_user(tg_id)
        if not u or u["status"] != "active": return False
        await _extend_nolock(u, hours)
        return True

async def ban(tg_id: int):
    async with _lock:
        u = get_user(tg_id)
        if u and u["key_id"]: await h1_delete(u["key_id"])
        db.execute("UPDATE users SET status='banned' WHERE tg_id=?", (tg_id,)); db.commit()

def add_device_slot(tg_id: int):
    db.execute("UPDATE users SET device_limit=device_limit+1 WHERE tg_id=?", (tg_id,)); db.commit()

def remove_device_slot(tg_id: int) -> tuple[int, int] | None:
    """−1 слот (не ниже 1). Если привязанных устройств больше нового лимита — лишние отрезаются с конца.
    Возвращает (новый_лимит, сколько_устройств_отвязано) или None, если пользователя нет."""
    u = get_user(tg_id)
    if not u: return None
    limit, devs = max(1, (u["device_limit"] or 1) - 1), devices_of(u)
    db.execute("UPDATE users SET device_limit=?, devices=? WHERE tg_id=?", (limit, json.dumps(devs[:limit]), tg_id))
    db.commit()
    return limit, max(0, len(devs) - limit)

def reset_devices(tg_id: int):
    """Очищает список привязанных устройств (глобальная защита триала по hwids остаётся)."""
    db.execute("UPDATE users SET devices='[]' WHERE tg_id=?", (tg_id,)); db.commit()

# ---------- админ: поиск, время, отзыв доступа ----------
def find_user(q: str):
    """Поиск по числовому TG ID или @username (без учёта регистра)."""
    q = (q or "").strip().lstrip("@")
    if not q: return None
    if q.isdigit(): return get_user(int(q))
    return db.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (q,)).fetchone()

async def shift_time(tg_id: int, hours: float) -> str:
    """+/− часы к сроку. Возвращает ok | expired (срок ушёл в прошлое: клиент удалён, статус expired) | noop.
    Плюс для неактивного пользователя выдаёт доступ заново; минус для неактивного ничего не делает."""
    async with _lock:
        u = get_user(tg_id)
        if not u or u["status"] == "banned": return "noop"
        active = u["status"] == "active"
        if hours > 0:
            if active: await _extend_nolock(u, hours)
            else: await _grant_nolock(u, hours / 24, (u["limit_bytes"] / GB) or TRIAL_GB)
            return "ok"
        if not active: return "noop"
        exp = u["expires_at"] + int(hours * 3600)
        if exp <= now():
            await h1_delete(u["key_id"])
            db.execute("UPDATE users SET status='expired',expires_at=? WHERE tg_id=?", (exp, tg_id)); db.commit()
            return "expired"
        await _extend_nolock(u, hours)
        return "ok"

async def revoke(tg_id: int) -> bool:
    """Полный отзыв доступа без бана: клиент удаляется из панели, expired, срок 0, устройства очищены."""
    async with _lock:
        u = get_user(tg_id)
        if not u or u["status"] == "banned": return False
        if u["key_id"]: await h1_delete(u["key_id"])
        db.execute("UPDATE users SET status='expired',expires_at=0,devices='[]' WHERE tg_id=?", (tg_id,)); db.commit()
        return True

# ---------- промокоды (одноразовые) ----------
_PROMO_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # без похожих 0/O, 1/I/L
_CYR2LAT = str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX")  # кириллица, случайно набранная вместо латиницы
_promo_fails: dict[int, list[int]] = {}
PROMO_MAX_FAILS, PROMO_WINDOW = 5, 600                  # 5 неверных кодов за 10 минут -> пауза
_promo_gc_at = 0.0

def _promo_gc(t: int):
    """Раз в минуту выкидывает из _promo_fails тех, у кого все неудачи старше окна (иначе словарь растёт вечно)."""
    global _promo_gc_at
    if t - _promo_gc_at < 60: return
    _promo_gc_at = t
    for k in [k for k, v in _promo_fails.items() if not any(x > t - PROMO_WINDOW for x in v)]:
        del _promo_fails[k]

def norm_code(raw: str) -> str:
    """'hutz-ab12-cd34', 'HUTZ AB12 CD34', 'hutzab12cd34' -> 'HUTZ-AB12-CD34'."""
    s = re.sub(r"[^A-Z0-9]", "", (raw or "").upper().translate(_CYR2LAT))
    return f"HUTZ-{s[4:8]}-{s[8:]}" if s.startswith("HUTZ") and len(s) == 12 else s

def _new_code() -> str:
    g = lambda: "".join(secrets.choice(_PROMO_ALPHABET) for _ in range(4))
    return f"HUTZ-{g()}-{g()}"

def apply_discount(price: int, discount_pct: int) -> int:
    """Возвращает цену со скидкой (не ниже 1 ₽)."""
    pct = max(0, min(99, int(discount_pct or 0)))
    return max(1, round(price * (100 - pct) / 100)) if pct else price

def create_custom_promos(kind: str = "days", days: int = 0, devices: int = 1, discount_pct: int = 0,
                         max_uses: int = 1, count: int = 1, custom_code: str | None = None) -> list[str]:
    """Создаёт 1 кастомный промокод (если задан custom_code) или count случайных кодов.
    kind: 'days' (подписка на days дней и devices устройств) | 'discount' (скидка discount_pct% на оплату)."""
    kind = "discount" if kind == "discount" else "days"
    days = max(0, int(days))
    devices = max(1, int(devices))
    discount_pct = max(0, min(95, int(discount_pct)))
    max_uses = max(1, int(max_uses))
    count = max(1, min(100, int(count)))
    t = now()

    if custom_code:
        code = norm_code(custom_code)
        if len(code) < 3 or len(code) > 32:
            raise ValueError("Код должен содержать от 3 до 32 латинских букв/цифр.")
        try:
            db.execute(
                "INSERT INTO promo_codes(code,days,devices,is_used,created_at,kind,discount_pct,max_uses,uses_count) "
                "VALUES(?,?,?,0,?,?,?,?,0)",
                (code, days, devices, t, kind, discount_pct, max_uses))
        except sqlite3.IntegrityError:
            raise ValueError(f"Промокод {code} уже существует.")
        db.commit()
        return [code]

    codes = []
    while len(codes) < count:
        code = _new_code()
        try:
            db.execute(
                "INSERT INTO promo_codes(code,days,devices,is_used,created_at,kind,discount_pct,max_uses,uses_count) "
                "VALUES(?,?,?,0,?,?,?,?,0)",
                (code, days, devices, t, kind, discount_pct, max_uses))
        except sqlite3.IntegrityError:
            continue
        codes.append(code)
    db.commit()
    return codes

def gen_promos(days: int, devices: int, n: int) -> list[str]:
    """Создаёт n уникальных одноразовых кодов на days дней и devices устройств."""
    return create_custom_promos(kind="days", days=days, devices=devices, max_uses=1, count=n)

def promos_left() -> int:
    return db.execute("SELECT COUNT(*) FROM promo_codes WHERE is_used=0 AND uses_count < max_uses").fetchone()[0]

def list_active_promos(limit: int = 20):
    return db.execute(
        "SELECT * FROM promo_codes WHERE is_used=0 AND uses_count < max_uses "
        "ORDER BY (max_uses > 1) DESC, created_at DESC LIMIT ?", (limit,)).fetchall()

def delete_promo(code: str) -> bool:
    cur = db.execute("DELETE FROM promo_codes WHERE code=?", (code,))
    db.execute("DELETE FROM promo_uses WHERE code=?", (code,))
    db.commit()
    return bool(cur.rowcount)

async def redeem_promo(tg_id: int, raw: str):
    """Активация кода. Возвращает (статус, user, promo):
    ok | ok_discount | discount_better | invalid | used | used_self | banned | throttled."""
    code, t = norm_code(raw), now()
    async with _lock:
        _promo_gc(t)
        fails = [x for x in _promo_fails.get(tg_id, []) if x > t - PROMO_WINDOW]
        if len(fails) >= PROMO_MAX_FAILS: return "throttled", None, None
        p = db.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
        if not p:
            _promo_fails[tg_id] = fails + [t]
            return "invalid", None, None
        already = db.execute("SELECT 1 FROM promo_uses WHERE code=? AND tg_id=?", (code, tg_id)).fetchone()
        if already or (p["max_uses"] <= 1 and p["used_by"] == tg_id):
            return "used_self", None, p
        if p["is_used"] or (p["uses_count"] or 0) >= (p["max_uses"] or 1):
            _promo_fails[tg_id] = fails + [t]
            return "used", None, None
        u = ensure_user(tg_id)
        if u["status"] == "banned": return "banned", u, p
        kind = p["kind"] or "days"
        if kind == "discount" and (u["discount_pct"] or 0) >= (p["discount_pct"] or 0):
            return "discount_better", u, p
        # «занимаем» активацию до обращения к панели
        cur = db.execute(
            "UPDATE promo_codes SET uses_count=uses_count+1, "
            "is_used=CASE WHEN uses_count+1 >= max_uses THEN 1 ELSE 0 END, "
            "used_by=?, used_at=? WHERE code=? AND is_used=0 AND uses_count < max_uses",
            (tg_id, t, code))
        if not cur.rowcount:
            return "used", None, None
        db.execute("INSERT OR IGNORE INTO promo_uses(code,tg_id,used_at) VALUES(?,?,?)", (code, tg_id, t))
        db.commit()
        if kind == "discount":
            db.execute("UPDATE users SET discount_pct=?, discount_code=? WHERE tg_id=?",
                       (p["discount_pct"], code, tg_id))
            db.commit()
            _promo_fails.pop(tg_id, None)
            return "ok_discount", get_user(tg_id), p
        try:
            if u["status"] == "active" and u["expires_at"] > t:
                await _extend_nolock(u, p["days"] * 24, PAID_GB if u["plan"] != "paid" else None)
                db.execute("UPDATE users SET plan='paid', device_limit=? WHERE tg_id=?",
                           (max(u["device_limit"] or 1, p["devices"]), tg_id))
                db.commit()
            else:
                await _grant_nolock(u, p["days"], PAID_GB, p["devices"])
        except Exception:
            db.execute(
                "UPDATE promo_codes SET uses_count=MAX(0,uses_count-1), is_used=0 WHERE code=?", (code,))
            db.execute("DELETE FROM promo_uses WHERE code=? AND tg_id=?", (code, tg_id))
            db.commit()
            raise
        _promo_fails.pop(tg_id, None)
        return "ok", get_user(tg_id), p

# ---------- платежи (RollyPay) ----------
def create_payment_row(order_id: str, tg_id: int, key: str, days: int, devices: int, amount: int, method: str):
    db.execute("INSERT INTO payments(order_id,tg_id,plan_key,days,devices,amount,method,status,created_at) "
               "VALUES(?,?,?,?,?,?,?,'pending',?)", (order_id, tg_id, key, days, devices, amount, method, now()))
    db.commit()

def get_payment_row(order_id: str): return db.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()

def set_payment_link(order_id: str, rp_id: str, url: str):
    db.execute("UPDATE payments SET rp_payment_id=?, pay_url=? WHERE order_id=?", (rp_id, url, order_id)); db.commit()

def mark_payment_failed(order_id: str):
    db.execute("UPDATE payments SET status='failed' WHERE order_id=? AND status='pending'", (order_id,)); db.commit()

async def fulfill_payment(order_id: str):
    """Идемпотентное зачисление. Возвращает ('ok', row) — зачислено сейчас; ('already', row) — уже было;
    ('unknown', None) — заказа нет. Заказ «занимается» атомарным UPDATE до обращения к панели
    (повторный вебхук/кнопка не начислят второй раз); при сбое панели статус откатывается и исключение
    уходит наверх (вебхук ответит 500 -> RollyPay повторит доставку)."""
    async with _lock:
        p = get_payment_row(order_id)
        if not p: return "unknown", None
        if p["status"] == "paid": return "already", p
        if not db.execute("UPDATE payments SET status='paid', paid_at=? WHERE order_id=? AND status!='paid'",
                          (now(), order_id)).rowcount:
            return "already", p
        db.commit()
        try:
            u, t = ensure_user(p["tg_id"]), now()
            if p["plan_key"] == "test1":                   # тестовый платёж на 1 ₽ из админки — подписку не трогаем
                pass
            elif p["plan_key"] == "x":                     # доп. устройство: +1 слот
                add_device_slot(p["tg_id"])
            elif u["status"] == "active" and u["expires_at"] > t:
                await _extend_nolock(u, p["days"] * 24, PAID_GB if u["plan"] != "paid" else None)
                db.execute("UPDATE users SET plan='paid', device_limit=? WHERE tg_id=?",
                           (max(u["device_limit"] or 1, p["devices"]), p["tg_id"]))
                db.commit()
            else:
                await _grant_nolock(u, p["days"], PAID_GB, p["devices"])
            if p["plan_key"] != "test1" and (u["discount_pct"] or 0) > 0:
                db.execute("UPDATE users SET discount_pct=0, discount_code=NULL WHERE tg_id=?", (p["tg_id"],))
                db.commit()
        except Exception:
            db.execute("UPDATE payments SET status='pending', paid_at=NULL WHERE order_id=?", (order_id,))
            db.commit()
            raise
        return "ok", get_payment_row(order_id)

# ---------- уведомления (флаги в БД → нет спама после перезапуска) ----------
def notify_expiring() -> list[tuple[int, int]]:
    """[(tg_id, осталось_секунд)]: платным — за NOTIFY_EXPIRY_HOURS (24 ч), тесту — за NOTIFY_TRIAL_HOURS."""
    t = now()
    rows = db.execute(
        "SELECT tg_id,expires_at FROM users WHERE status='active' AND notified_exp=0 AND expires_at>? AND "
        "expires_at <= ? + CASE WHEN plan='trial' THEN ? ELSE ? END",
        (t, t, int(NOTIFY_TRIAL_HOURS * 3600), int(NOTIFY_EXPIRY_HOURS * 3600))).fetchall()
    if rows:
        db.executemany("UPDATE users SET notified_exp=1 WHERE tg_id=?", [(r["tg_id"],) for r in rows]); db.commit()
    return [(r["tg_id"], r["expires_at"] - t) for r in rows]

async def notify_traffic() -> list[tuple[int, int, int]]:
    """[(tg_id, used_bytes, limit_bytes)] для тех, кто впервые достиг порога трафика."""
    out = []
    rows = db.execute("SELECT tg_id,key_id,limit_bytes FROM users "
                      "WHERE status='active' AND notified_traffic=0 AND limit_bytes>0").fetchall()
    for r in rows:
        try: used = await usage(r["key_id"])
        except Exception: continue
        if used >= r["limit_bytes"] * NOTIFY_TRAFFIC_PCT / 100:
            cur = db.execute("UPDATE users SET notified_traffic=1 WHERE tg_id=? AND notified_traffic=0", (r["tg_id"],))
            db.commit()
            if cur.rowcount: out.append((r["tg_id"], used, r["limit_bytes"]))
    return out
