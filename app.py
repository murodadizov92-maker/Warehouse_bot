"""Sklad tahlil boti (Render web service).
Har kuni 08:00 da (Toshkent) eng ko'p turib qolgan TOP_N ta tovarni guruhga yuboradi.
Baza kerak emas: har safar sotuv tarixini Sales Doctor'dan qayta o'qiydi."""
import asyncio
import datetime as dt
import html
import json
import logging
import os
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from salesdoc import SalesDocClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

# ------------------------------------------------------------ sozlamalar (Render Environment)
def env(name, default=""):
    return os.getenv(name, default).strip()

TOP_N = int(env("TOP_N", "10"))
STALE_DAYS = int(env("STALE_DAYS", "10"))
OVERSTOCK_DAYS = int(env("OVERSTOCK_DAYS", "15"))  # zaxira shuncha kundan ko'p yetsa = ko'payib ketgan
OVERSTOCK_N = int(env("OVERSTOCK_N", "5"))
COVER_CAP = int(env("COVER_CAP", "100"))  # zaxira shundan ko'p bo'lsa "100+ kun" deb yoziladi
TREND_PCT = float(env("TREND_PCT", "20"))  # hafta/oy kunlik sotuvi shu foizdan ko'p farq qilsa 📈/📉
TOTAL_N = int(env("TOTAL_N", "15"))  # xabardagi jami tovar soni (sotilmagan + ko'payib ketgan)
LOOKBACK = int(env("LOOKBACK_DAYS", "60"))
STATUSES = [int(x) for x in env("SOLD_STATUSES", "1,2,3,4").split(",") if x]
EXCLUDE = [x.strip() for x in env("EXCLUDE_CATEGORIES").split(",") if x.strip()]
INCLUDE = [x.strip() for x in env("INCLUDE_CATEGORIES").split(",") if x.strip()]
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


async def get_sales(client, t):
    """Tovar bo'yicha kunlik sotuv: {pid: {'YYYY-MM-DD': miqdor}} (oxirgi LOOKBACK kun)."""
    daily = {}
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
                qty = float(line.get("quantity") or 0) - float(line.get("returned") or 0)  # vozvrat ayriladi
                if pid and day and qty > 0:
                    d = daily.setdefault(pid, {})
                    d[day] = d.get(day, 0.0) + qty
        cur = end + dt.timedelta(days=1)
    return daily


def sales_stats(daily, t):
    """Qaytaradi: oxirgi sotilgan sana, oxirgi 7 kun va oxirgi 30 kunlik sotuv (bugun hisobga olinmaydi)."""
    d7 = t - dt.timedelta(days=7)
    d30 = t - dt.timedelta(days=30)
    last, week, month = {}, {}, {}
    for pid, days in daily.items():
        for day, q in days.items():
            if day > last.get(pid, ""):
                last[pid] = day
            d = dt.date.fromisoformat(day)
            if d30 <= d < t:
                month[pid] = month.get(pid, 0.0) + q
                if d >= d7:
                    week[pid] = week.get(pid, 0.0) + q
    return last, week, month


def pick_top(stock, last_sold, t):
    """Qoldig'i bor va STALE_DAYS+ kun sotilmagan tovarlar; muhimlik = qoldiq x kun."""
    cands = []
    for pid, s in stock.items():
        if s["qty"] <= 0:
            continue
        ld = last_sold.get(pid)
        days = (t - dt.date.fromisoformat(ld)).days if ld else None
        if days is None or days >= STALE_DAYS:
            cands.append({"id": pid, "name": s["name"], "qty": s["qty"], "days": days,
                          "score": s["qty"] * (days if days is not None else LOOKBACK)})
    cands.sort(key=lambda c: -c["score"])
    return cands


def trend_mark(rate_w, rate_m):
    if rate_m <= 0:
        return "📈" if rate_w > 0 else "➖"
    ratio = rate_w / rate_m
    if ratio > 1 + TREND_PCT / 100:
        return "📈"
    if ratio < 1 - TREND_PCT / 100:
        return "📉"
    return "➖"


def pick_overstock(stock, week, month, skip_ids):
    """Zaxira kuni = ostatka / max(oxirgi 7 kun kunlik sotuvi, oxirgi 30 kun kunlik sotuvi).
    Zaxira OVERSTOCK_DAYS dan ko'p kunga yetsa — ko'payib ketgan. Eng katta ortiqcha birinchi."""
    out = []
    for pid, s in stock.items():
        if pid in skip_ids or s["qty"] <= 0:
            continue
        w, m = week.get(pid, 0.0), month.get(pid, 0.0)
        rate_w, rate_m = w / 7, m / 30
        rate = max(rate_w, rate_m)
        if rate <= 0:
            continue
        cover = s["qty"] / rate
        excess = s["qty"] - rate * OVERSTOCK_DAYS
        if cover >= OVERSTOCK_DAYS and excess > 0:
            out.append({"name": s["name"], "qty": s["qty"], "week": w, "month": m,
                        "cover": cover, "excess": excess, "trend": trend_mark(rate_w, rate_m)})
    out.sort(key=lambda c: -c["excess"])
    return out


def fmt_qty(q):
    return str(int(q)) if float(q).is_integer() else f"{q:.1f}"


def advice(c):
    if c["days"] is None:
        return f"{LOOKBACK} kunda umuman sotilmagan — har bir mijozga taklif qiling"
    if c["days"] >= 30:
        return "uzoq turib qolgan — birinchi navbatda sotishga harakat qiling"
    return "mijozlarga birinchi navbatda taklif qiling"


def split_counts(n_stale, n_over):
    """Jami TOTAL_N ta tovar: avval TOP_N ta sotilmagan + OVERSTOCK_N ta ko'payib ketgan.
    Biri yetmasa, qolgan joyni ikkinchisidan to'ldiradi."""
    s = min(n_stale, TOP_N)
    o = min(n_over, OVERSTOCK_N)
    rem = TOTAL_N - s - o
    if rem > 0:
        add = min(rem, n_stale - s)
        s += add
        rem -= add
    if rem > 0:
        o += min(rem, n_over - o)
    return s, o


def build_message(cands, t, over=None):
    over = over or []
    never = sum(1 for c in cands if c["days"] is None)
    head = (f"📦 <b>Sklad tahlili — {t:%d.%m.%Y}</b>\n"
            f"{STALE_DAYS}+ kun sotilmagan tovar: <b>{len(cands)} ta</b> "
            f"({LOOKBACK} kunda umuman sotilmagan: {never} ta).\n")
    if not cands and not over:
        return head + "\n✅ Hamma tovar aylanmoqda, bugun alohida ro'yxat yo'q."
    n_s, n_o = split_counts(len(cands), len(over))
    lines = [head + f"Bugungi ro'yxat: <b>{n_s + n_o} ta</b> tovar.\n"]
    k = 0
    if n_s:
        lines.append(f"<b>🕒 Uzoq vaqt sotilmagan ({n_s} ta):</b>\n")
        for c in cands[:n_s]:
            k += 1
            sold = "umuman sotilmagan" if c["days"] is None else f"{c['days']} kun sotilmagan"
            lines.append(f"<b>{k}. {html.escape(c['name'])}</b>\n"
                         f"   Ostatka: <b>{fmt_qty(c['qty'])}</b> · {sold}\n"
                         f"   💡 {advice(c)}\n")
    if n_o:
        lines.append(f"<b>📈 Ko'payib ketgan ({n_o} ta)</b> — zaxira {OVERSTOCK_DAYS}+ kunga yetadi:\n")
        for c in over[:n_o]:
            k += 1
            cover = f"{COVER_CAP}+" if c["cover"] >= COVER_CAP else f"~{round(c['cover'])}"
            lines.append(f"<b>{k}. {html.escape(c['name'])}</b>\n"
                         f"   Ostatka: <b>{fmt_qty(c['qty'])}</b> · Oy: {fmt_qty(c['month'])} sotildi · "
                         f"Hafta: {fmt_qty(c['week'])} sotildi {c['trend']}\n"
                         f"   Zaxira {cover} kunga yetadi\n"
                         f"   💡 zaxira ko'p — shu tovarni ko'proq sotishga harakat qiling\n")
    lines.append(f"📣 <b>Buyruq:</b> {html.escape(AGENT_NOTE)}")
    return "\n".join(lines)


async def get_categories(client):
    cats = await client.paginate("getProductCategory", {}, "productCategory")
    return [{"id": c["SD_id"], "name": c.get("name") or c["SD_id"]} for c in cats]


async def products_in_categories(client, tokens):
    """Kategoriyalar (nom yoki SD_id) ichidagi tovar ID'lari. Qaytaradi: (to'plam, topilmaganlar)."""
    if not tokens:
        return set(), []
    cats = await get_categories(client)
    by_key = {}
    for c in cats:
        by_key[c["id"].lower()] = c
        by_key[c["name"].strip().lower()] = c
    ids, missing = set(), []
    for token in tokens:
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
    daily = await get_sales(client, t)
    last, week, month = sales_stats(daily, t)
    stock = await get_stock(client)
    missing = []
    if INCLUDE:  # faqat shu kategoriyalar
        allowed, miss = await products_in_categories(client, INCLUDE)
        missing += miss
        stock = {pid: s for pid, s in stock.items() if pid in allowed}
    if EXCLUDE:  # shu kategoriyalardan tashqari
        banned, miss = await products_in_categories(client, EXCLUDE)
        missing += miss
        stock = {pid: s for pid, s in stock.items() if pid not in banned}
    cands = pick_top(stock, last, t)
    over = pick_overstock(stock, week, month, {c["id"] for c in cands})
    text = build_message(cands, t, over)
    if missing:
        text += "\n\n⚠️ Kategoriya topilmadi: " + html.escape(", ".join(missing)) + " (/kategoriya bilan tekshiring)"
    return text


# ------------------------------------------------------------ yuborish
def split_text(text, limit=3900):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


async def post(bot, chat_id, text, thread_id=None):
    for part in split_text(text):
        await bot.send_message(chat_id, part, message_thread_id=thread_id, parse_mode=ParseMode.HTML)


async def fresh_report(client):
    async with busy:
        return await make_report(client)


async def send_report(bot, client, chat_id, thread_id=None):
    """/sklad buyrug'i uchun: hozirning o'zida yangidan hisoblab yuboradi."""
    await post(bot, chat_id, await fresh_report(client), thread_id)


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
        await post(bot, chat_id, text, thread_id)
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
        inc = {e.lower() for e in INCLUDE}
        lines = ["<b>Kategoriyalar</b> (✅ = hisobotga kiradi, 🚫 = kirmaydi):"]
        for c in sorted(cats, key=lambda c: c["name"].lower()):
            keys = {c["id"].lower(), c["name"].strip().lower()}
            if keys & ex:
                mark = "🚫 "
            elif inc:
                mark = "✅ " if keys & inc else "🚫 "
            else:
                mark = ""
            lines.append(f"{mark}{html.escape(c['name'])}  <code>{html.escape(c['id'])}</code>")
        lines.append("\nFaqat shular: INCLUDE_CATEGORIES = id1,id2 · Chiqarish: EXCLUDE_CATEGORIES = id1,id2")
        await m.answer("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        log.exception("kategoriya")
        await m.answer(f"❌ Xato: {html.escape(str(e))}", parse_mode=ParseMode.HTML)


@dp.message(Command("tekshir"))
async def cmd_tekshir(m: Message, command: CommandObject):
    """Bitta tovar bo'yicha xom raqamlarni ko'rsatadi (birlik/hisob xatosini topish uchun)."""
    q = (command.args or "").strip().lower()
    if not q:
        await m.answer("Misol: /tekshir ШАРҚОНА")
        return
    await m.answer("⏳ Tekshiryapman...")
    try:
        async with busy:
            client, t = RT["sd"], today()
            found = {}
            for w in await client.paginate("getStock", {}, "warehouse"):
                for p in w.get("products") or []:
                    if q in (p.get("name") or "").lower():
                        f = found.setdefault(p["SD_id"], {"name": p.get("name"), "wh": [], "raw": p})
                        f["wh"].append(f"{w.get('SD_id')}: {p.get('quantity')}")
            if not found:
                await m.answer("Bunday tovar skladdan topilmadi.")
                return
            pid, info = next(iter(found.items()))
            d7 = t - dt.timedelta(days=7)
            wk = mo = 0.0
            samples = []
            cur = t - dt.timedelta(days=30)
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
                        if (line.get("product") or {}).get("SD_id") != pid:
                            continue
                        qty = float(line.get("quantity") or 0) - float(line.get("returned") or 0)  # vozvrat ayriladi
                        if qty > 0 and day:
                            d = dt.date.fromisoformat(day)
                            if d < t:
                                mo += qty
                                if d >= d7:
                                    wk += qty
                            if len(samples) < 2:
                                samples.append(line)
                cur = end + dt.timedelta(days=1)
        raw_stock = json.dumps(info["raw"], ensure_ascii=False)[:600]
        raw_line = json.dumps(samples, ensure_ascii=False)[:900]
        await m.answer(
            f"<b>{html.escape(info['name'] or '')}</b>\n"
            f"Sklad ostatkasi: {html.escape('; '.join(info['wh']))}\n"
            f"Sotuv: 7 kunda <b>{fmt_qty(wk)}</b> · 30 kunda <b>{fmt_qty(mo)}</b>\n\n"
            f"Sklad yozuvi:\n<code>{html.escape(raw_stock)}</code>\n\n"
            f"Buyurtma qatori:\n<code>{html.escape(raw_line)}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.exception("tekshir")
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
