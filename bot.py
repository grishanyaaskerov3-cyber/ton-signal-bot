#!/usr/bin/env python3
"""TON token signal bot. Pure script, no AI.
  python bot.py report        -> scheduled report (logs signals to history.json for calibration)
  python bot.py report --dry  -> print report, send nothing
  python bot.py poll          -> answer Telegram commands for ~4 min (long polling)
"""
import json, math, os, re, sys, time, html, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

GT = "https://api.geckoterminal.com/api/v2"
UA = {"User-Agent": "ton-signal-bot/1.1", "Accept": "application/json"}
TON_ADDR = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"
SKIP_SYMBOLS = {"TON", "PTON", "WTON", "USDT", "USD₮", "JUSDT", "JUSDC", "USDC", "TSTON", "STTON", "HTON", "USDE", "TSUSDE", "TONCOIN", "WTTON", "TTON", "JTON", "AQUSD"}
MIN_LIQ = 10_000
HIST = "history.json"
KYIV = timezone(timedelta(hours=2))
BUCKETS = [(-1e9, 10, "<10"), (10, 20, "10-20"), (20, 35, "20-35"), (35, 1e9, "35+")]
HEUR = {"<10": 10, "10-20": 20, "20-35": 30, "35+": 40}  # conservative prior, % chance of >=+5% within 6h
HEUR_DOWN = {"<10": 40, "10-20": 35, "20-35": 30, "35+": 25}  # prior, % chance of <=-5% within 6h
MIN_SAMPLES = 20
HIT_PCT = 5.0
errors = []
e = html.escape


def get(url, accept_json=True, retries=3, timeout=25):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA if accept_json else {"User-Agent": "Mozilla/5.0 ton-signal-bot"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            return json.loads(data) if accept_json else data
        except urllib.error.HTTPError as ex:
            if ex.code == 429:
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


def gt_ton_price():
    try:
        d = get(f"{GT}/simple/networks/ton/token_price/{TON_ADDR}")
        return f(d["data"]["attributes"]["token_prices"].get(TON_ADDR)) or None
    except Exception as ex:
        errors.append(f"TON: {type(ex).__name__}")
        return None


def universe(tonp):
    """Cheap pre-scan of ALL TON pools (STON.fi + DeDust + DexScreener). Returns (ranked candidate pool addrs, tokens scanned)."""
    cands, toks = {}, set()
    try:  # STON.fi: ~50k pools with liquidity + 24h volume in USD
        for p in get("https://api.ston.fi/v1/pools", timeout=60).get("pool_list", []):
            toks.add(p.get("token0_address")); toks.add(p.get("token1_address"))
            if p.get("deprecated"): continue
            liq, v = f(p.get("lp_total_supply_usd")), f(p.get("volume_24h_usd"))
            if liq >= MIN_LIQ and v >= 300:
                cands[p["address"]] = v + liq * 0.01
    except Exception as ex:
        errors.append(f"STON.fi: {type(ex).__name__}")
    try:  # DeDust: reserves only (its volume stats are empty), TON-paired pools
        for p in get("https://api.dedust.io/v2/pools-lite", timeout=60):
            a = p.get("assets", [])
            for x in a: toks.add(x)
            if "native" in a and tonp:
                i = a.index("native")
                liq = int(p["reserves"][i]) / 1e9 * 2 * tonp
                if liq >= MIN_LIQ:
                    cands.setdefault(p["address"], liq * 0.01)
    except Exception as ex:
        errors.append(f"DeDust: {type(ex).__name__}")
    try:  # DexScreener search
        for q in ("ton", "jetton"):
            for p in get(f"https://api.dexscreener.com/latest/dex/search?q={q}").get("pairs", []) or []:
                if p.get("chainId") == "ton" and f((p.get("liquidity") or {}).get("usd")) >= MIN_LIQ:
                    toks.add((p.get("baseToken") or {}).get("address"))
                    cands.setdefault(p["pairAddress"], f((p.get("volume") or {}).get("h24")))
    except Exception as ex:
        errors.append(f"DexScreener: {type(ex).__name__}")
    toks.discard(None)
    return [a for a, _ in sorted(cands.items(), key=lambda kv: -kv[1])], len(toks)


def fetch_pools(detail=150):
    eps = [f"trending_pools?include=base_token&page={i}" for i in (1, 2, 3)] + \
          [f"new_pools?include=base_token&page={i}" for i in (1, 2, 3)] + \
          [f"pools?include=base_token&sort=h24_volume_usd_desc&page={i}" for i in range(1, 6)] + \
          [f"pools?include=base_token&sort=h24_tx_count_desc&page={i}" for i in (1, 2, 3)]
    pools, tokens = {}, {}
    def take(d):
        for t in d.get("included", []) or []:
            tokens[t["id"]] = t.get("attributes", {})
        for p in d.get("data", []) or []:
            pools[p["id"]] = p
    for ep in eps:
        try: take(get(f"{GT}/networks/ton/{ep}"))
        except Exception as ex: errors.append(f"GT {ep.split('?')[0]}: {type(ex).__name__}")
        time.sleep(2.1)  # free API ~30 req/min
    tonp = gt_ton_price()
    cands, scanned = universe(tonp)
    known = {p["attributes"].get("address") for p in pools.values()}
    extra = [a for a in cands if a not in known][:detail]
    for i in range(0, len(extra), 30):  # detailed stats for top pre-filtered candidates
        try: take(get(f"{GT}/networks/ton/pools/multi/{','.join(extra[i:i + 30])}?include=base_token"))
        except Exception as ex: errors.append(f"GT multi: {type(ex).__name__}")
        time.sleep(2.1)
    stats = {"scanned": max(scanned, len(pools)), "prefiltered": len(set(cands) | known), "detailed": len(pools), "tonp": tonp}
    return pools, tokens, stats


def parse_pool(p, tokens, now):
    """Common fields; None for TON/stables/low liquidity."""
    a = p.get("attributes", {})
    rel = p.get("relationships", {})
    base_id = (rel.get("base_token", {}).get("data") or {}).get("id", "")
    tok = tokens.get(base_id, {})
    sym = (tok.get("symbol") or a.get("name", "?").split(" / ")[0]).strip()
    if sym.upper() in SKIP_SYMBOLS or base_id.endswith(TON_ADDR):
        return None
    if "USD" in sym.upper() and 0.95 <= f(a.get("base_token_price_usd")) <= 1.05:
        return None  # stablecoin
    liq = f(a.get("reserve_in_usd"))
    if liq < MIN_LIQ:
        return None
    pc = a.get("price_change_percentage") or {}
    vol = a.get("volume_usd") or {}
    tx = (a.get("transactions") or {}).get("h1") or {}
    age_h = None
    if a.get("pool_created_at"):
        try: age_h = (now - datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00"))).total_seconds() / 3600
        except ValueError: pass
    return {"sym": sym, "name": tok.get("name") or sym, "addr": base_id.split("_", 1)[-1], "pool": a.get("address", ""),
            "price": f(a.get("base_token_price_usd")), "m5": f(pc.get("m5")), "h1": f(pc.get("h1")),
            "h6": f(pc.get("h6")), "h24": f(pc.get("h24")), "v1": f(vol.get("h1")), "v24": f(vol.get("h24")),
            "liq": liq, "buys": int(f(tx.get("buys"))), "sells": int(f(tx.get("sells"))), "buyers": int(f(tx.get("buyers"))),
            "fdv": f(a.get("fdv_usd")) or f(a.get("market_cap_usd")), "age_h": age_h}


def best_by_token(pools, tokens, now):
    best = {}
    for p in pools.values():
        try: r = parse_pool(p, tokens, now)
        except Exception: r = None
        if r and (r["addr"] not in best or r["liq"] > best[r["addr"]]["liq"]):
            best[r["addr"]] = r
    return best


def score(t):
    s, reasons, flags, severe = 0.0, [], [], False
    s += clip(t["m5"], -20, 30) * 0.8 + clip(t["h1"], -30, 60) * 0.5 + clip(t["h6"], -50, 100) * 0.15
    if t["h1"] > 5: reasons.append(f"рост {t['h1']:+.0f}%/1ч")
    avg_h = t["v24"] / 24 if t["v24"] > 0 else 0
    acc = t["v1"] / avg_h if avg_h > 0 else (2.0 if t["v1"] > 0 else 0)
    if acc > 0: s += clip(math.log2(acc), -2, 3) * 10
    if acc >= 1.8: reasons.append(f"объём x{acc:.1f}")
    tot = t["buys"] + t["sells"]
    ratio = t["buys"] / tot if tot else 0.5
    s += (ratio - 0.5) * 60 * min(1.0, tot / 30)
    s += math.log10(t["buyers"] + 1) * 5
    if ratio >= 0.65 and tot >= 20: reasons.append(f"покупок {ratio*100:.0f}%")
    s += clip(math.log10(t["liq"] / MIN_LIQ) * 5, 0, 10)
    fl = t["fdv"] / t["liq"] if t["liq"] and t["fdv"] else 0
    if fl > 300: s -= 30; flags.append("FDV≫ликв"); severe = True
    elif fl > 100: s -= 15; flags.append("FDV/ликв>100")
    if tot >= 20 and ratio < 0.35: s -= 15; flags.append("сливают"); severe = True
    if t["age_h"] is not None and t["age_h"] < 6: s -= 5; flags.append(f"новый {t['age_h']:.0f}ч")
    if t["h24"] > 300: s -= 10; flags.append("уже +300%")
    if t["v1"] < 500: s -= 10; flags.append("мало объёма")
    if t["liq"] < 25_000: flags.append("мелкая ликв")
    risk = "🔴" if len(flags) >= 2 or (t["age_h"] is not None and t["age_h"] < 24) or t["liq"] < 25_000 else ("🟡" if flags or t["liq"] < 100_000 else "🟢")
    t.update(score=s, reasons=reasons, flags=flags, severe=severe, risk=risk)
    return t


def bucket(s):
    for lo, hi, name in BUCKETS:
        if lo <= s < hi: return name
    return "<10"


def success(t, hist):
    """(rost%, padenie%, calibrated). Historical rates per score bucket, else conservative heuristic."""
    b = bucket(t["score"])
    st = hist.get("stats", {}).get(b, {})
    if st.get("n", 0) >= MIN_SAMPLES:
        return round(100 * st["hits"] / st["n"]), round(100 * st.get("downs", 0) / st["n"]), True
    nf = len(t["flags"]) + (2 if t["severe"] else 0)
    up = int(clip(HEUR[b] - 5 * nf, 3, 60))
    down = int(clip(HEUR_DOWN[b] + 5 * nf, 5, 90))
    return up, down, False


def position(t, up, down, calibrated):
    """Suggested max share of deposit, %. 0 = don't enter."""
    if t["severe"] or t["risk"] == "🔴" or up <= down:
        return 0
    size = clip((up - down) / 5, 1, 5)
    size = min(size, 1 if t["liq"] < 50_000 else 3 if t["liq"] < 200_000 else 5)
    if not calibrated:
        size = min(size, 2)  # no track record yet -> stay tiny
    return int(round(size))


def token_prices(addrs):
    out = {}
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        try:
            d = get(f"{GT}/simple/networks/ton/token_price/{','.join(chunk)}")
            for k, v in (d["data"]["attributes"]["token_prices"] or {}).items():
                if v: out[k] = f(v); out[k.lower()] = f(v)
        except Exception as ex:
            errors.append(f"prices: {type(ex).__name__}")
        time.sleep(2.2)
    return out


def ton_price(pools):
    try:
        d = get(f"{GT}/simple/networks/ton/token_price/{TON_ADDR}")
        v = d["data"]["attributes"]["token_prices"].get(TON_ADDR)
        if v: return f(v)
    except Exception as ex:
        errors.append(f"TON: {type(ex).__name__}")
    for p in pools.values():
        q = (p.get("relationships", {}).get("quote_token", {}).get("data") or {}).get("id", "")
        if q.endswith(TON_ADDR) and p["attributes"].get("quote_token_price_usd"):
            return f(p["attributes"]["quote_token_price_usd"])
    return None


def news(n=2):
    items = []
    for u in ["https://cointelegraph.com/rss", "https://www.coindesk.com/arc/outboundfeeds/rss/"]:
        try:
            root = ET.fromstring(get(u, accept_json=False))
            for it in root.iter("item"):
                t, l = (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip().split("?utm_")[0]
                if t and l: items.append((t, l))
        except Exception as ex:
            errors.append(f"RSS {u.split('/')[2]}: {type(ex).__name__}")
    rx = re.compile(r"\b(TON|Toncoin|Telegram|Durov|STON|Notcoin|Tonkeeper)\b", re.I)
    ton = [i for i in items if rx.search(i[0])]
    out, seen = [], set()
    for t, l in ton + [i for i in items if i not in ton]:
        if t not in seen:
            seen.add(t); out.append((t, l))
        if len(out) >= n: break
    return out, bool(ton)


def money(x):
    if x >= 1e6: return f"${x/1e6:.1f}M"
    if x >= 1e3: return f"${x/1e3:.0f}k"
    return f"${x:.0f}"


def pfmt(x):
    if not x: return "$0"
    if x >= 1: return f"${x:,.2f}"
    return f"${x:.{max(2, -int(math.floor(math.log10(x))) + 2)}f}"


def links(t):
    return (f"<a href=\"https://dexscreener.com/ton/{t['pool']}\">DS</a>·"
            f"<a href=\"https://app.ston.fi/swap?ft=TON&amp;tt={t['addr']}\">STON</a>")


def short(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"


# ---------------- history / calibration ----------------
def load_hist():
    try:
        with open(HIST) as fh: return json.load(fh)
    except Exception:
        return {"signals": [], "stats": {}}


def update_hist(hist, best, now):
    """Fill 1h/6h/24h prices of logged signals; finalize 6h outcome into per-bucket stats."""
    open_sig = [s for s in hist["signals"] if not s.get("closed")]
    if not open_sig: return
    cur = {a: t["price"] for a, t in best.items()}
    missing = sorted({s["addr"] for s in open_sig if s["addr"] not in cur})
    cur.update(token_prices(missing))
    for s in open_sig:
        age = (now - datetime.fromisoformat(s["t"])).total_seconds() / 3600
        p = cur.get(s["addr"]) or cur.get(s["addr"].lower())
        if p and age <= 6.25:
            s["max6"] = max(s.get("max6", s["p0"]), p)
            s["min6"] = min(s.get("min6", s["p0"]), p)
        if p and age >= 1 and "p1" not in s: s["p1"] = p
        if age >= 6 and not s.get("done6"):
            if p: s["p6"] = p
            s["done6"] = True
            hit = s.get("max6", s["p0"]) >= s["p0"] * (1 + HIT_PCT / 100)
            st = hist["stats"].setdefault(s["b"], {"n": 0, "hits": 0})
            down = s.get("min6", s["p0"]) <= s["p0"] * (1 - HIT_PCT / 100)
            st["n"] += 1; st["hits"] += int(hit); st["downs"] = st.get("downs", 0) + int(down)
            s["hit"], s["down"] = hit, down
        if age >= 24:
            if p: s["p24"] = p
            s["closed"] = True
    cutoff = now - timedelta(days=3)
    hist["signals"] = [s for s in hist["signals"] if not s.get("closed") or datetime.fromisoformat(s["t"]) > cutoff]


def log_signals(hist, top, now):
    recent = {s["addr"] for s in hist["signals"] if (now - datetime.fromisoformat(s["t"])) < timedelta(hours=6)}
    for t in top:
        if t["addr"] in recent or not t["price"]: continue  # one sample per coin per 6h (avoid correlated duplicates)
        hist["signals"].append({"t": now.isoformat(timespec="seconds"), "sym": t["sym"], "addr": t["addr"],
                                "p0": t["price"], "score": round(t["score"], 1), "b": bucket(t["score"]), "v": t["verdict"]})


# ---------------- messages ----------------
def build_report(log=False):
    now = datetime.now(timezone.utc)
    hist = load_hist()
    pools, tokens, st = fetch_pools()
    best = best_by_token(pools, tokens, now)
    if log:
        try: update_hist(hist, best, now)
        except Exception as ex: errors.append(f"history: {type(ex).__name__}")
    top = sorted((score(t) for t in best.values()), key=lambda x: x["score"], reverse=True)[:7]
    tp = st["tonp"] or ton_price(pools)
    L = [f"<b>TON {pfmt(tp) if tp else 'н/д'}</b> · скан {st['scanned']:,} токенов → {len(best)} с ликв ≥$10k".replace(",", " ")]
    if not top: L.append("Подходящих монет нет (или API недоступно).")
    for t in top:
        up, down, cal = success(t, hist)
        pos = position(t, up, down, cal)
        t["verdict"] = f"buy{pos}" if pos else "no"
        m = "" if cal else "~"
        v = f"✅ вложить до {pos}% депозита" if pos else "❌ не вкладывать"
        L.append(f"<a href=\"https://dexscreener.com/ton/{t['pool']}\">{e(short(t['sym'], 10))}</a> — рост {m}{up}% / падение {m}{down}% — {v}")
    if errors: L.append("<i>⚠ частично: " + e("; ".join(errors[:2])) + "</i>")
    L.append("<i>% — оценка, не гарантия</i>")
    if log:
        log_signals(hist, top, now)
        hist["keepalive_day"] = now.strftime("%Y-%m-%d"); hist["last_run"] = now.isoformat(timespec="seconds")
        with open(HIST, "w") as fh: json.dump(hist, fh, indent=0, ensure_ascii=False)
    return "\n".join(L)[:4000]


def change_7d(pool):
    d = get(f"{GT}/networks/ton/pools/{pool}/ohlcv/day?limit=8&currency=usd")
    ol = d["data"]["attributes"]["ohlcv_list"]  # newest first: [ts,o,h,l,c,v]
    if len(ol) < 2: return None
    old = ol[7][4] if len(ol) >= 8 else ol[-1][1]
    return (ol[0][4] / old - 1) * 100 if old else None


def movers(direction, period):
    now = datetime.now(timezone.utc)
    pools, tokens, _ = fetch_pools()
    best = [t for t in best_by_token(pools, tokens, now).values() if t["v24"] >= 1000]
    if period == "7d":
        cands = sorted(best, key=lambda t: t["v24"] + t["liq"] * 0.2, reverse=True)[:40]
        for t in cands:
            try: t["ch"] = change_7d(t["pool"])
            except Exception: t["ch"] = None
            time.sleep(2.1)
        best = [t for t in cands if t.get("ch") is not None]
    else:
        for t in best: t["ch"] = t["h1"] if period == "1h" else t["h24"]
    best.sort(key=lambda t: t["ch"], reverse=(direction == "gainers"))
    title = {"gainers": "🚀 Выросли", "losers": "📉 Упали"}[direction]
    per = {"1h": "1ч", "24h": "24ч", "7d": "7д"}[period]
    L = [f"<b>{title} за {per}</b>"]
    for t in best[:5]:
        L.append(f"<a href=\"https://dexscreener.com/ton/{t['pool']}\">{e(short(t['sym'], 10))}</a> {t['ch']:+.1f}% · {pfmt(t['price'])}")
    if len(L) == 1: L.append("Нет данных (API недоступно).")
    if period == "7d": L.append(f"<i>по дневным свечам, топ-{len(best)} пулов</i>")
    if errors: L.append("<i>⚠ частично: " + e("; ".join(errors[:3])) + "</i>")
    return "\n".join(L)


HELP = ("<b>Команды</b>\n/report — отчёт сейчас\n/gainers_1h /gainers_24h /gainers_7d — топ-5 выросших\n"
        "/losers_1h /losers_24h /losers_7d — топ-5 упавших\n/news — новости\n(можно и /gainers 24h)\nАвтоотчёт каждые 30 мин.")
COMMANDS = [("report", "Отчёт сейчас"), ("gainers_1h", "Топ-5 роста за 1ч"), ("gainers_24h", "Топ-5 роста за 24ч"),
            ("gainers_7d", "Топ-5 роста за 7д"), ("losers_1h", "Топ-5 падения за 1ч"), ("losers_24h", "Топ-5 падения за 24ч"),
            ("losers_7d", "Топ-5 падения за 7д"), ("news", "Крипто-новости"), ("help", "Помощь")]


# ---------------- telegram ----------------
def tg(token, method, payload, timeout=30):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read())
    except urllib.error.HTTPError as ex:
        try: return json.loads(ex.read())
        except Exception: return {"ok": False, "description": f"HTTP {ex.code}"}
    except Exception as ex:
        return {"ok": False, "description": type(ex).__name__}


def send(token, chat, msg):
    r = tg(token, "sendMessage", {"chat_id": chat, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True})
    if not r.get("ok"):
        print(f"HTML send failed: {r.get('description')}")
        r = tg(token, "sendMessage", {"chat_id": chat, "text": re.sub(r"<[^>]+>", "", html.unescape(msg)), "disable_web_page_preview": True})
    print(f"send ok={r.get('ok')}")
    return r.get("ok")


def arg_safe(cmd):
    return re.sub(r"[^a-z0-9]", "", cmd[1].lower())[:5] if len(cmd) > 1 else ""


def handle(token, chat, text):
    cmd = text.strip().split()
    if not cmd or not cmd[0].startswith("/"):
        print("non-command message ignored"); return
    c = cmd[0][1:].split("@")[0].lower()
    print(f"command: /{c[:20]} {arg_safe(cmd)}")
    arg = cmd[1].lower() if len(cmd) > 1 else ""
    m = re.fullmatch(r"(gainers|losers)(?:_(1h|24h|7d))?", c)
    del errors[:]
    if m:
        period = m.group(2) or {"1ч": "1h", "24ч": "24h", "7д": "7d", "1d": "24h", "d": "24h", "w": "7d"}.get(arg, arg) or "24h"
        if period not in ("1h", "24h", "7d"): period = "24h"
        if period == "7d": tg(token, "sendMessage", {"chat_id": chat, "text": "⏳ Считаю 7д (~2 мин)…"})
        send(token, chat, movers(m.group(1), period))
    elif c == "report":
        send(token, chat, build_report(log=False))
    elif c == "news":
        nws, _ = news(3)
        send(token, chat, "\n".join(f"• <a href=\"{e(l)}\">{e(short(t, 80))}</a>" for t, l in nws) or "Новости недоступны.")
    elif c in ("help", "start"):
        send(token, chat, HELP)
    else:
        send(token, chat, "Не знаю такую команду. " + HELP)


def poll(token, owner, budget=240):
    tg(token, "setMyCommands", {"commands": [{"command": a, "description": b} for a, b in COMMANDS]})
    start, offset, handled = time.time(), None, 0
    while time.time() - start < budget:
        left = int(budget - (time.time() - start))
        payload = {"timeout": max(1, min(50, left)), "allowed_updates": ["message"]}
        if offset: payload["offset"] = offset
        u = tg(token, "getUpdates", payload, timeout=60)
        if not u.get("ok"):
            print("getUpdates failed:", u.get("description")); time.sleep(5); continue
        for up in u["result"]:
            offset = up["update_id"] + 1
            # confirm right away so a crash never replays a command
            tg(token, "getUpdates", {"offset": offset, "timeout": 0, "limit": 1})
            msg = up.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != owner:
                continue  # ignore everyone except the owner
            try:
                handle(token, owner, msg.get("text") or ""); handled += 1
            except Exception as ex:
                print("handler error:", type(ex).__name__)
                send(token, owner, f"Ошибка: {e(type(ex).__name__)}")
    print(f"poll done, handled={handled}")


def main():
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT_ID", "").strip()
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    dry = "--dry" in sys.argv
    if mode == "poll":
        if not (token and chat): print("missing secrets"); return 1
        poll(token, chat, int(os.environ.get("POLL_SECONDS", "240"))); return 0
    if mode == "movers":
        print(movers(sys.argv[2], sys.argv[3])); return 0
    msg = build_report(log=not dry)
    if dry or not (token and chat):
        print(msg); return 0
    ok = send(token, chat, msg)
    print(f"errors={errors}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
