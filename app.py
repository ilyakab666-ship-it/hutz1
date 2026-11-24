"""FastAPI-прослойка для Happ + запуск бота и фоновых задач в одном процессе.
Запуск: uvicorn app:app --env-file .env --host 127.0.0.1 --port 8000"""
import asyncio, base64, html, json, logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
import core as c
import rollypay as rp
from bot import bot, dp, dur, finalize_payment, notify_admins, send_notice, setup_commands

logging.basicConfig(level=logging.INFO)

MSG = {
    "not_found": (404, "Подписка не найдена."),
    "banned": (403, "Доступ заблокирован."),
    "expired": (403, "Срок доступа истёк."),
    "device_limit": (403, "Достигнут лимит устройств."),
    "hwid_used": (403, "Пробный период для этого устройства уже использован."),
}

def empty_sub() -> Response:
    """Подписка истекла / обнулена: 200 + пустое тело, чтобы Happ очистил список серверов (а не оставил кэш)."""
    title = "base64:" + base64.b64encode(f"{c.BRAND} (Нет подписки)".encode()).decode()  # кириллица не лезет в HTTP-заголовок
    return Response(b"", status_code=200, headers={
        "profile-title": title, "support-url": c.SUPPORT_URL, "profile-update-interval": "1",
        "content-type": "text/plain; charset=utf-8"})

async def step(name: str, fn):
    """Шаг фонового цикла изолирован: сбой одного не блокирует остальные."""
    try: await fn()
    except Exception:
        logging.exception("background: %s", name)

async def job_expiring():
    for tg_id, left in c.notify_expiring():
        await send_notice(tg_id, f"⏳ Доступ к <b>{c.BRAND}</b> закончится через {dur(left)}. "
                                 "Продлите заранее, чтобы не потерять подключение.")

async def job_expired():
    for tg_id in await c.expire_due():
        await send_notice(tg_id, "⏱ Ваш доступ закончился. Продлите подписку, чтобы вернуться.")

async def job_traffic():
    for tg_id, used, limit in await c.notify_traffic():
        await send_notice(tg_id, f"📊 Использовано {c.NOTIFY_TRAFFIC_PCT:g}% трафика "
                                 f"({used / c.GB:.2f} из {limit / c.GB:.2f} ГБ). "
                                 "Когда лимит закончится, интернет через VPN перестанет работать")

async def background_loop():
    """Раз в минуту: напоминание «скоро конец» (платным — за 24 ч) и истёкшие подписки; раз в 5 минут — порог трафика."""
    tick = 0
    while True:
        await step("expiring", job_expiring)
        await step("expired", job_expired)
        if tick % 5 == 0: await step("traffic", job_traffic)
        tick += 1
        await asyncio.sleep(60)

def _task_done(t: asyncio.Task):
    if not t.cancelled() and t.exception():
        logging.error("фоновая задача упала", exc_info=t.exception())
        asyncio.get_event_loop().create_task(notify_admins(f"🔴 Фоновая задача остановилась: {t.exception()!r}"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    try: await setup_commands()
    except Exception: logging.exception("set_my_commands")
    tasks = [asyncio.create_task(dp.start_polling(bot, handle_signals=False)),
             asyncio.create_task(background_loop())]
    for t in tasks: t.add_done_callback(_task_done)
    yield
    for t in tasks: t.cancel()
    await bot.session.close()
    await c._http.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/sub/{token}")
async def sub(token: str, request: Request):
    hwid = (request.headers.get("x-hwid") or "").strip()
    if not hwid:
        logging.info("sub %s…: нет x-hwid", token[:6])
        return PlainTextResponse("Откройте ссылку в приложении Happ.", status_code=400)
    try:
        status, u = await c.resolve_sub(token, hwid, request.headers.get("x-device-model", ""),
                                        request.headers.get("x-device-os", ""))
        logging.info("sub %s… hwid=%s… -> %s", token[:6], hwid[:8], status)
        if status == "expired": return empty_sub()
        if status != "ok":
            code, text = MSG[status]
            return PlainTextResponse(text, status_code=code)
        # если панель отвечает 404 (клиента удалили, а в БД он active) — пересоздаём его на оставшийся срок
        # и обновляем sub_url в БД, а не падаем с 502
        res = await c.fetch_sub_restoring(u, request.headers)
        if not res: return empty_sub()
        body, hdrs, u = res
    except Exception as e:
        logging.exception("sub failed")
        c.alert("sub", f"🔴 /sub не смог получить подписку с панели: {e!r}")
        return PlainTextResponse("Панель недоступна, повторите позже.", status_code=502)
    if "subscription-userinfo" not in hdrs:
        try: used = await c.usage(u["key_id"])
        except Exception: used = 0
        hdrs["subscription-userinfo"] = (f"upload=0; download={used}; total={u['limit_bytes']}; "
                                         f"expire={u['expires_at']}")
    hdrs["profile-title"] = c.BRAND[:25]          # название подписки в Happ (до 25 символов)
    hdrs["support-url"] = c.SUPPORT_URL           # иконка поддержки внутри Happ
    hdrs["profile-update-interval"] = "1"         # автообновление раз в час
    hdrs.setdefault("content-type", "text/plain; charset=utf-8")
    return Response(body, headers=hdrs)

@app.post("/webhook/rollypay")
async def webhook_rollypay(request: Request):
    raw = await request.body()                       # подпись считается по сырому телу
    if not rp.verify_signature(raw, request.headers.get("x-signature", ""), request.headers.get("x-timestamp", "")):
        logging.warning("rollypay webhook: неверная подпись")
        return PlainTextResponse("bad signature", status_code=403)
    try: d = json.loads(raw)
    except ValueError: return PlainTextResponse("bad json", status_code=400)
    if d.get("event_type") != "payment.paid" or d.get("status") != "paid":
        return PlainTextResponse("ok")               # прочие события (created/expired/underpaid…) — просто 2xx
    if d.get("test") and not rp.TEST_MODE:
        return PlainTextResponse("ok")               # тестовый платёж в боевом режиме не зачисляем
    p = c.get_payment_row(str(d.get("order_id", "")))
    if not p:
        logging.warning("rollypay webhook: неизвестный order_id %s", d.get("order_id"))
        return PlainTextResponse("ok")
    try: paid = float(d.get("amount", 0))
    except (TypeError, ValueError): paid = 0
    if paid + 0.001 < p["amount"]:                   # сумма в вебхуке меньше счёта — не зачисляем
        await notify_admins(f"⚠️ RollyPay: сумма {d.get('amount')} ≠ {p['amount']} по заказу {p['order_id']}")
        return PlainTextResponse("ok")
    try: await finalize_payment(p["order_id"])
    except Exception:
        logging.exception("webhook finalize")
        return PlainTextResponse("retry", status_code=500)   # RollyPay повторит доставку (панель недоступна)
    return PlainTextResponse("ok")

OPEN_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__BRAND__</title>
<style>:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{display:grid;place-items:center;min-height:100vh;margin:0;padding:24px;box-sizing:border-box;text-align:center}
.btn{display:inline-block;padding:14px 28px;border-radius:12px;background:#2f6bff;color:#fff;text-decoration:none;font-weight:600}
code{word-break:break-all;font-size:13px}.hint{opacity:.7;font-size:14px}</style></head>
<body><main><h2>__BRAND__</h2><p>Открываем Happ…</p>
<p><a class="btn" href="__DEEPLINK__">🚀 Открыть в Happ</a></p>
<p class="hint">Не открылось? Установите Happ и добавьте подписку вручную:</p><p><code>__SUB__</code></p></main>
<script>setTimeout(function(){location.href=__DEEPLINK_JS__},50)</script></body></html>"""

@app.get("/open/{token}", response_class=HTMLResponse)
async def open_in_happ(token: str):
    if not c.db.execute("SELECT 1 FROM users WHERE token=?", (token,)).fetchone():
        return HTMLResponse("Не найдено", status_code=404)
    sub_link = f"{c.PUBLIC_URL}/sub/{token}"
    deeplink = f"happ://add/{sub_link}"
    page = (OPEN_PAGE.replace("__BRAND__", html.escape(c.BRAND)).replace("__DEEPLINK_JS__", json.dumps(deeplink))
            .replace("__DEEPLINK__", html.escape(deeplink)).replace("__SUB__", html.escape(sub_link)))
    return HTMLResponse(page)


if __name__ == "__main__":   # python app.py — то же, что uvicorn app:app (на Bothost запускается именно так)
    import os, uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))

