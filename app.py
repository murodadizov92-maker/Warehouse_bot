"""Sklad tahlil boti (Render web service).
Har kuni 08:00 da (Toshkent) eng ko'p turib qolgan TOP_N ta tovarni guruhga yuboradi.
Baza kerak emas: har safar sotuv tarixini Sales Doctor'dan qayta o'qiydi."""
import asyncio
import datetime as dt
import html
import logging
import os
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from salesdoc import SalesDocClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

# ------------------------------------------------------------ sozlamalar (Render Environment)
def env(name, default=""):
    return os.getenv(name, default).strip()

TOP_N = int(env("TOP_N", "10"))
STALE_DAYS = int(env("STALE_DAYS", "10"))
LOOKBACK = int(env("LOOKBACK_DAYS", "60"))
STATUSES = [int(x) for x in env("SOLD_STATUSES", "1,2,3,4").split(",") if x]
EXCLUDE = [x.strip() for x in env("EXCLUDE_CATEGORIES").split(",") if x.strip()]
WAREHOUSES = {x.strip() for x in env("WAREHOUSE_IDS").split(",") if x.strip()} or None
TZ = ZoneInfo(env("TIMEZONE", "Asia/Tashkent"))
ANALYZE_TIME = env("ANALYZE_TIME", "06:30")  # Sales Doctor'ga kirish va tahlil vaqti
SEND_TIME = env("SEND_TIME", "08:00")        # guruhga yuborish vaqti
CATCHUP_MIN = int(env("CATCHUP_MINUTES", "90"))
AGENT_NOTE = env(
    "AGENT_NOTE",
    "Agentlar, bugun yuqoridagi tovarlarni mijozlarga birinchi navbatda taklif qiling. "
    "Har bir agent kamida bitta shu tovarni sotishga harakat qilsin.",
)

state = {"last_sent": None, "cache": None}
busy = asyncio.Lock()


def today():
    return dt.datetime.now(TZ).date()


# ------------------------------------------------------------ tahlil
async def get_stock(client):
    warehouses = await client.paginate("getStock", {}, "warehouse")
    per_wh = {}
    for w in warehouses:
        if WAREHOUSES and w.get("SD_id") not in WAREHOUSES:
            continue
        for p in w.get("products") or []:
            if p.get("active", "Y") == "N":
                continue
            per_wh[(w.get("SD_id"), p["SD_id"])] = (p.get("name") or "", float(p.get("quantity") or 0))
    stock = {}
    for (_, pid), (name, qty) in per_wh.items():
        s = stock.setdefault(pid, {"name": name, "qty": 0.0})
        s["qty"] += qty
    return stock


async def get_last_sold(client, t):
    last = {}
    cur = t - dt.timedelta(days=LOOKBACK)
    while cur <= t:
        end = min(cur + dt.timedelta(days=6), t)
        orders = await client.paginate(
            "getOrder",
            {"filter": {"agent": "all", "status": STATUSES,
                        "period": {"date": {"from": str(cur), "to": str(end)}}}},
            "order",
        )
        for o in orders:
            day = (o.get("dateDocument") or o.get("dateCreate") or "")[:10]
            for line in o.get("orderProducts") or []:
                pid = (line.get("product") or {}).get("SD_id")
                if pid and day and (line.get("quantity") or 0) > 0 and day > last.get(pid, ""):
                    last[pid] = day
        cur = end + dt.timedelta(days=1)
    return last


def pick_top(stock, last_sold, t):
    """Qoldig'i bor va STALE_DAYS+ kun sotilmagan tovarlar; muhimlik = qoldiq x kun."""
    cands = []
    for pid, s in stock.items():
        if s["qty"] <= 0:
            continue
        ld = last_sold.get(pid)
        days = (t - dt.date.fromisoformat(ld)).days if ld else None
        if days is None or days >= STALE_DAYS:
            cands.append({"name": s["name"], "qty": s["qty"], "days": days,
                          "score": s["qty"] * (days if days is not None else LOOKBACK)})
    cands.sort(key=lambda c: -c["score"])
    return cands


def fmt_qty(q):
    return str(int(q)) if float(q).is_integer() else f"{q:.1f}"


def advice(c):
    if c["days"] is None:
        return f"{LOOKBACK} kunda umuman sotilmagan — yangi tovar sifatida taklif qiling yoki aksiya qo'ying"
    if c["days"] >= 30:
        return "juda uzoq turib qolgan — chegirma yoki boshqa tovar bilan to'plam qilib soting"
    return "mijozlarga birinchi navbatda taklif qiling"


def build_message(cands, t):
    never = sum(1 for c in cands if c["days"] is None)
    head = (f"📦 <b>Sklad tahlili — {t:%d.%m.%Y}</b>\n"
            f"{STALE_DAYS}+ kun sotilmagan tovar: <b>{len(cands)} ta</b> "
            f"({LOOKBACK} kunda umuman sotilmagan: {never} ta).\n")
    if not cands:
        return head + "\n✅ Hamma tovar aylanmoqda, bugun alohida ro'yxat yo'q."
    lines = [head, f"<b>Bugungi eng muhim {min(TOP_N, len(cands))} ta tovar:</b>\n"]
    for n, c in enumerate(cands[:TOP_N], 1):
        sold = "umuman sotilmagan" if c["days"] is None else f"{c['days']} kun sotilmagan"
        lines.append(f"<b>{n}. {html.escape(c['name'])}</b>\n"
                     f"   Ostatka: <b>{fmt_qty(c['qty'])}</b> · {sold}\n"
                     f"   💡 {advice(c)}\n")
    lines.append(f"📣 <b>Buyruq:</b> {html.escape(AGENT_NOTE)}")
    return "\n".join(lines)


async def get_categories(client):
    cats = await client.paginate("getProductCategory", {}, "productCategory")
    return [{"id": c["SD_id"], "name": c.get("name") or c["SD_id"]} for c in cats]


async def get_excluded(client):
    """EXCLUDE_CATEGORIES (nom yoki SD_id) bo'yicha chiqarib tashlanadigan tovar ID'lari.
    Qaytaradi: (tovar_idlar_to'plami, topilmagan_nomlar_ro'yxati)"""
    if not EXCLUDE:
        return set(), []
    cats = await get_categories(client)
    by_key = {}
    for c in cats:
        by_key[c["id"].lower()] = c
        by_key[c["name"].strip().lower()] = c
    ids, missing = set(), []
    for token in EXCLUDE:
        c = by_key.get(token.lower())
        if not c:
            missing.append(token)
            continue
        warehouses = await client.paginate("getStock", {"category": {"SD_id": c["id"]}}, "warehouse")
        for w in warehouses:
            for p in w.get("products") or []:
                ids.add(p["SD_id"])
    return ids, missing


async def make_report(client):
    t = today()
    last = await get_last_sold(client, t)
    stock = await get_stock(client)
    excluded, missing = await get_excluded(client)
    stock = {pid: s for pid, s in stock.items() if pid not in excluded}
    text = build_message(pick_top(stock, last, t), t)
    if missing:
        text += "\n\n⚠️ Kategoriya topilmadi: " + html.escape(", ".join(missing)) + " (/kategoriya bilan tekshiring)"
    return text


# ------------------------------------------------------------ yuborish
async def fresh_report(client):
    async with busy:
        return await make_report(client)


async def send_report(bot, client, chat_id, thread_id=None):
    """/sklad buyrug'i uchun: hozirning o'zida yangidan hisoblab yuboradi."""
    await bot.send_message(chat_id, await fresh_report(client), message_thread_id=thread_id, parse_mode=ParseMode.HTML)


async def do_analyze(client):
    """Ertalabki tahlil: Sales Doctor'ga kiradi, natijani xotirada saqlaydi (yubormaydi)."""
    try:
        state["cache"] = (today(), await fresh_report(client))
        log.info("Tahlil tayyor, %s da yuboriladi", SEND_TIME)
    except Exception:
        log.exception("Tahlilda xato")


async def do_send(bot, client, chat_id, thread_id):
    if chat_id is None:
        log.warning("GROUP_CHAT_ID berilmagan")
        return
    if state["last_sent"] == today():
        return
    try:
        cache = state["cache"]
        text = cache[1] if cache and cache[0] == today() else await fresh_report(client)
        await bot.send_message(chat_id, text, message_thread_id=thread_id, parse_mode=ParseMode.HTML)
        state["last_sent"] = today()
    except Exception:
        log.exception("Hisobot yuborilmadi")
        try:
            await bot.send_message(chat_id, "❌ Bugungi sklad tahlilida xato chiqdi.", message_thread_id=thread_id, parse_mode=ParseMode.HTML)
        except Exception:
            pass


def _hm(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h, m


async def daily(at, job):
    h, m = _hm(at)
    while True:
        now = dt.datetime.now(TZ)
        nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        await job()


async def scheduler(bot, client):
    chat_id = int(env("GROUP_CHAT_ID")) if env("GROUP_CHAT_ID") else None
    thread_id = int(env("TOPIC_ID")) if env("TOPIC_ID") else None

    async def send():
        await do_send(bot, client, chat_id, thread_id)

    async def analyze():
        await do_analyze(client)

    # Render servisni qayta ishga tushirgan bo'lsa, o'tkazib yuborilganini bajaradi
    await asyncio.sleep(10)
    now = dt.datetime.now(TZ)
    sh, sm = _hm(SEND_TIME)
    send_at = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if send_at <= now <= send_at + dt.timedelta(minutes=CATCHUP_MIN):
        await send()
    elif ANALYZE_TIME:
        ah, am = _hm(ANALYZE_TIME)
        if now.replace(hour=ah, minute=am, second=0, microsecond=0) <= now < send_at:
            await analyze()

    jobs = [asyncio.create_task(daily(SEND_TIME, send))]
    if ANALYZE_TIME:
        jobs.append(asyncio.create_task(daily(ANALYZE_TIME, analyze)))
    await asyncio.gather(*jobs)


# ------------------------------------------------------------ Telegram buyruqlari
dp = Dispatcher()
RT = {}  # bot va client main() da to'ldiriladi


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Chat ID: <code>{m.chat.id}</code>\nTopik ID: <code>{m.message_thread_id}</code>", parse_mode=ParseMode.HTML)


@dp.message(Command("kategoriya"))
async def cmd_kategoriya(m: Message):
    try:
        async with busy:
            cats = await get_categories(RT["sd"])
        ex = {e.lower() for e in EXCLUDE}
        lines = ["<b>Kategoriyalar</b> (🚫 = hisobotga kirmaydi):"]
        for c in sorted(cats, key=lambda c: c["name"].lower()):
            mark = "🚫 " if (c["id"].lower() in ex or c["name"].strip().lower() in ex) else ""
            lines.append(f"{mark}{html.escape(c['name'])}  <code>{html.escape(c['id'])}</code>")
        lines.append("\nChiqarib tashlash: Render → Environment → EXCLUDE_CATEGORIES = nom1, nom2")
        await m.answer("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        log.exception("kategoriya")
        await m.answer(f"❌ Xato: {html.escape(str(e))}", parse_mode=ParseMode.HTML)


@dp.message(Command("sklad"))
async def cmd_sklad(m: Message):
    await m.answer("⏳ Hisoblayapman...")
    try:
        await send_report(RT["bot"], RT["sd"], m.chat.id, m.message_thread_id)
    except Exception as e:
        log.exception("sklad")
        await m.answer(f"❌ Xato: {html.escape(str(e))}", parse_mode=ParseMode.HTML)


# ------------------------------------------------------------ veb-server (UptimeRobot uchun)
async def health(_):
    return web.Response(text=f"ok, oxirgi yuborilgan: {state['last_sent']}")


async def main():
    bot = Bot(env("BOT_TOKEN"), default_properties=DefaultBotProperties(parse_mode=ParseMode.HTML))
    sd = SalesDocClient(env("SALESDOC_DOMAIN"), env("SALESDOC_LOGIN"),
                        env("SALESDOC_PASSWORD"), env("SALESDOC_FILIAL_ID"))
    RT.update(bot=bot, sd=sd)

    app = web.Application()
    app.add_routes([web.get("/", health), web.get("/health", health)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(env("PORT", "10000"))).start()
    log.info("Veb-server ishga tushdi")

    task = asyncio.create_task(scheduler(bot, sd))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await sd.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
