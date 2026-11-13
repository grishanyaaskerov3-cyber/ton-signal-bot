#!/usr/bin/env python3
"""TON token signal bot: pure script, no AI. Fetches GeckoTerminal pools + RSS news, scores tokens, sends a Telegram report."""
import json, math, os, re, sys, time, html, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

GT = "https://api.geckoterminal.com/api/v2"
UA = {"User-Agent": "ton-signal-bot/1.0", "Accept": "application/json"}
TON_ADDR = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"
SKIP_SYMBOLS = {"TON", "PTON", "WTON", "USDT", "USD₮", "JUSDT", "JUSDC", "USDC", "TSTON", "STTON", "HTON", "USDE", "TSUSDE", "TONCOIN"}
MIN_LIQ = 10_000
STATE_FILE = "state.json"
KYIV = timezone(timedelta(hours=2))
errors = []


def get(url, accept_json=True, retries=3, timeout=25):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA if accept_json else {"User-Agent": "Mozilla/5.0 ton-signal-bot"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            return json.loads(data) if accept_json else data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(15 * (i + 1)); continue
            if i == retries - 1: raise
            time.sleep(3)
        except Exception:
            if i == retries - 1: raise
            time.sleep(3)


def f(x, d=0.0):
    try: return float(x) if x is not None else d
    except (TypeError, ValueError): return d


def clip(x, lo, hi): return max(lo, min(hi, x))


def fetch_pools():
    endpoints = [
        "/networks/ton/trending_pools?include=base_token&page=1",
        "/networks/ton/trending_pools?include=base_token&page=2",
        "/networks/ton/new_pools?include=base_token&page=1",
        "/networks/ton/new_pools?include=base_token&page=2",
        "/networks/ton/pools?include=base_token&sort=h24_volume_usd_desc&page=1",
        "/networks/ton/pools?include=base_token&sort=h24_volume_usd_desc&page=2",
        "/networks/ton/pools?include=base_token&sort=h24_volume_usd_desc&page=3",
    ]
    pools, tokens = {}, {}
    for ep in endpoints:
        try:
            d = get(GT + ep)
            for t in d.get("included", []) or []:
                tokens[t["id"]] = t.get("attributes", {})
            for p in d.get("data", []) or []:
                pools[p["id"]] = p
        except Exception as e:
            errors.append(f"GeckoTerminal {ep.split('?')[0].split('/')[-1]}: {type(e).__name__}")
        time.sleep(2.5)  # free API ~30 req/min
    return pools, tokens


def score_pool(p, tokens, now):
    a = p.get("attributes", {})
    rel = p.get("relationships", {})
    base_id = (rel.get("base_token", {}).get("data") or {}).get("id", "")
    quote_id = (rel.get("quote_token", {}).get("data") or {}).get("id", "")
    tok = tokens.get(base_id, {})
    sym = (tok.get("symbol") or a.get("name", "?").split(" / ")[0]).strip()
    if sym.upper() in SKIP_SYMBOLS or base_id.endswith(TON_ADDR):
        return None
    liq = f(a.get("reserve_in_usd"))
    if liq < MIN_LIQ:
        return None
    pc = a.get("price_change_percentage") or {}
    m5, h1, h6, h24 = f(pc.get("m5")), f(pc.get("h1")), f(pc.get("h6")), f(pc.get("h24"))
    vol = a.get("volume_usd") or {}
    v1, v24 = f(vol.get("h1")), f(vol.get("h24"))
    tx = (a.get("transactions") or {}).get("h1") or {}
    buys, sells, buyers = int(f(tx.get("buys"))), int(f(tx.get("sells"))), int(f(tx.get("buyers")))
    fdv = f(a.get("fdv_usd")) or f(a.get("market_cap_usd"))
    created = a.get("pool_created_at")
    age_h = None
    if created:
        try: age_h = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 3600
        except ValueError: pass

    reasons, flags = [], []
    s = 0.0
    # 1) momentum
    s += clip(m5, -20, 30) * 0.8 + clip(h1, -30, 60) * 0.5 + clip(h6, -50, 100) * 0.15
    if h1 > 5: reasons.append(f"рост {h1:+.0f}% за 1ч")
    # 2) volume acceleration: last hour vs avg hour of 24h
    avg_h = v24 / 24 if v24 > 0 else 0
    acc = v1 / avg_h if avg_h > 0 else (2.0 if v1 > 0 else 0)
    if acc > 0: s += clip(math.log2(acc), -2, 3) * 10
    if acc >= 1.8: reasons.append(f"объём x{acc:.1f} к среднему")
    # 3) buy pressure and breadth
    tot = buys + sells
    ratio = buys / tot if tot else 0.5
    s += (ratio - 0.5) * 60 * min(1.0, tot / 30)
    s += math.log10(buyers + 1) * 5
    if ratio >= 0.65 and tot >= 20: reasons.append(f"покупок {ratio*100:.0f}%")
    if buyers >= 50: reasons.append(f"{buyers} покупателей/ч")
    # 4) liquidity depth
    s += clip(math.log10(liq / MIN_LIQ) * 5, 0, 10)
    # red flags
    fdv_liq = fdv / liq if liq and fdv else 0
    if fdv_liq > 300: s -= 30; flags.append("FDV/ликв >300")
    elif fdv_liq > 100: s -= 15; flags.append("FDV/ликв >100")
    if tot >= 20 and ratio < 0.35: s -= 15; flags.append("сильные продажи")
    if age_h is not None and age_h < 6: s -= 5; flags.append(f"пул новый ({age_h:.0f}ч)")
    if h24 > 300: s -= 10; flags.append("уже +300%/24ч")
    if v1 < 500: s -= 10; flags.append("слабый объём")
    if liq < 25_000: flags.append("малая ликвидность")
    risk = "🔴 высокий" if len(flags) >= 2 or (age_h is not None and age_h < 24) or liq < 25_000 else ("🟡 средний" if flags or liq < 100_000 else "🟢 умеренный")
    addr = base_id.split("_", 1)[-1]
    return {
        "sym": sym, "name": tok.get("name") or sym, "addr": addr, "pool": a.get("address", ""),
        "price": f(a.get("base_token_price_usd")), "m5": m5, "h1": h1, "h24": h24, "v1": v1,
        "liq": liq, "buys": buys, "sells": sells, "score": s, "reasons": reasons, "flags": flags,
        "risk": risk, "dex": (rel.get("dex", {}).get("data") or {}).get("id", ""), "quote": quote_id,
    }


def ton_price(pools):
    try:
        d = get(f"{GT}/simple/networks/ton/token_price/{TON_ADDR}")
        v = d["data"]["attributes"]["token_prices"].get(TON_ADDR)
        if v: return f(v)
    except Exception as e:
        errors.append(f"TON price: {type(e).__name__}")
    for p in pools.values():
        a = p.get("attributes", {})
        q = (p.get("relationships", {}).get("quote_token", {}).get("data") or {}).get("id", "")
        if q.endswith(TON_ADDR) and a.get("quote_token_price_usd"):
            return f(a["quote_token_price_usd"])
    return None


def news():
    feeds = ["https://cointelegraph.com/rss", "https://www.coindesk.com/arc/outboundfeeds/rss/"]
    items = []
    for u in feeds:
        try:
            root = ET.fromstring(get(u, accept_json=False))
            for it in root.iter("item"):
                t, l = (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip().split("?utm_")[0]
                if t and l: items.append((t, l))
        except Exception as e:
            errors.append(f"RSS {u.split('/')[2]}: {type(e).__name__}")
    rx = re.compile(r"\b(TON|Toncoin|Telegram|Durov|STON|Notcoin|Tonkeeper)\b", re.I)
    ton = [i for i in items if rx.search(i[0])]
    rest = [i for i in items if i not in ton]
    seen, out = set(), []
    for t, l in ton + rest:
        if t in seen: continue
        seen.add(t); out.append((t, l))
        if len(out) >= 3: break
    return out, len(ton)


def money(x):
    if x >= 1e6: return f"${x/1e6:.2f}M"
    if x >= 1e3: return f"${x/1e3:.1f}k"
    return f"${x:.0f}"


def price_fmt(x):
    if x >= 1: return f"${x:,.3f}"
    if x == 0: return "$0"
    return f"${x:.{max(2, -int(math.floor(math.log10(x))) + 3)}f}"


def build(top, tp, nws, ton_news_n, n_pools):
    e = html.escape
    now = datetime.now(KYIV).strftime("%d.%m %H:%M")
    L = [f"<b>📊 TON-монеты: отчёт {now} (Киев)</b>"]
    L.append(f"TON: <b>{price_fmt(tp)}</b>" if tp else "TON: н/д")
    L.append(f"Проанализировано пулов: {n_pools}\n")
    if not top:
        L.append("Подходящих монет сейчас нет (или API недоступно).")
    for i, t in enumerate(top, 1):
        why = ", ".join(t["reasons"]) or "умеренный импульс"
        fl = ("\n⚠️ " + ", ".join(t["flags"])) if t["flags"] else ""
        L.append(
            f"<b>{i}. {e(t['sym'])}</b> ({e(t['name'][:30])}) — скор <b>{t['score']:.0f}</b>\n"
            f"💵 {price_fmt(t['price'])} | 5м {t['m5']:+.1f}% | 1ч {t['h1']:+.1f}% | 24ч {t['h24']:+.1f}%\n"
            f"📈 объём 1ч {money(t['v1'])} | ликв. {money(t['liq'])} | 🟩{t['buys']}/🟥{t['sells']}\n"
            f"💡 {e(why)}{e(fl)}\nРиск: {t['risk']}\n"
            f"<a href=\"https://dexscreener.com/ton/{t['pool']}\">DexScreener</a> · "
            f"<a href=\"https://www.geckoterminal.com/ton/pools/{t['pool']}\">GeckoTerminal</a> · "
            f"<a href=\"https://app.ston.fi/swap?ft=TON&amp;tt={t['addr']}\">STON.fi</a>\n")
    if nws:
        L.append("<b>📰 Новости</b>" + ("" if ton_news_n else " (о TON свежих нет, общие крипто)"))
        for t, l in nws:
            L.append(f"• <a href=\"{e(l)}\">{e(t[:140])}</a>")
        L.append("")
    if errors:
        L.append("<i>Частичные данные: " + e("; ".join(errors[:4])) + "</i>")
    L.append("<i>Это эвристика по рыночным данным, не гарантия и не финсовет. Мемкоины могут обнулиться.</i>")
    msg = "\n".join(L)
    return msg[:4000]


def load_state():
    try:
        with open(STATE_FILE) as fh: return json.load(fh)
    except Exception: return {"chat_ids": [], "last_update_id": 0}


def tg(token, method, payload):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read())
    except urllib.error.HTTPError as ex:
        try: return json.loads(ex.read())
        except Exception: return {"ok": False, "description": f"HTTP {ex.code}"}
    except Exception as ex:
        return {"ok": False, "description": type(ex).__name__}


def main():
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    dry = "--dry" in sys.argv
    state = load_state()
    secret_ids = [c.strip() for c in os.environ.get("TG_CHAT_ID", "").split(",") if c.strip()]
    if token and not dry:
        # discover chats that pressed /start (only private chats); only used when no TG_CHAT_ID secret is set
        u = tg(token, "getUpdates", {"offset": state.get("last_update_id", 0) + 1, "timeout": 0})
        if u.get("ok"):
            for up in u.get("result", []):
                state["last_update_id"] = max(state.get("last_update_id", 0), up["update_id"])
                chat = (up.get("message") or {}).get("chat") or {}
                if chat.get("type") == "private" and not secret_ids and str(chat["id"]) not in state["chat_ids"]:
                    state["chat_ids"].append(str(chat["id"]))
    chats = secret_ids or state.get("chat_ids", [])

    now = datetime.now(timezone.utc)
    pools, tokens = fetch_pools()
    best = {}
    for p in pools.values():
        try:
            r = score_pool(p, tokens, now)
        except Exception as ex:
            errors.append(f"score: {type(ex).__name__}"); continue
        if r and (r["addr"] not in best or r["liq"] > best[r["addr"]]["liq"]):
            best[r["addr"]] = r
    top = sorted(best.values(), key=lambda x: x["score"], reverse=True)[:5]
    tp = ton_price(pools)
    nws, ton_n = news()
    msg = build(top, tp, nws, ton_n, len(pools))

    state["last_run"] = now.isoformat(timespec="seconds")
    state["keepalive_day"] = now.strftime("%Y-%m-%d")
    with open(STATE_FILE, "w") as fh: json.dump(state, fh, indent=1)

    if dry or not token:
        print(msg); return 0
    if not chats:
        print("No chat id yet: press /start on the bot."); return 0
    fails = 0
    for c in chats:
        r = tg(token, "sendMessage", {"chat_id": c, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})
        if not r.get("ok"):
            r2 = tg(token, "sendMessage", {"chat_id": c, "text": re.sub(r"<[^>]+>", "", html.unescape(msg)), "disable_web_page_preview": True})
            print(f"HTML send failed ({r.get('description')}); plain-text fallback ok={r2.get('ok')}")
            if not r2.get("ok"): fails += 1
        else:
            print(f"sent ok to chat ...{str(c)[-3:]} (message_id {r['result']['message_id']})")
    print(f"pools={len(pools)} candidates={len(best)} errors={errors}")
    return 1 if fails == len(chats) else 0


if __name__ == "__main__":
    sys.exit(main())
