import os, time, json, sys, random, logging, requests
from datetime import datetime

"""
Pump.fun Solana Worker v4
- Dexscreener + Birdeye as data sources
- Tick logging + 5-min test mode
- Rate-limit handling with backoff
- Telegram alerts
"""

# ------------------------ Logging ------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("pumpbonk-worker")

# ------------------------ ENV ----------------------------
DEX_URLS = os.getenv(
    "DEX_URLS",
    "https://api.dexscreener.com/latest/dex/search?q=chain:solana%20dex:pumpfun"
)
BIRDEYE_API = os.getenv("BIRDEYE_API", "https://public-api.birdeye.so/defi/tokenlist?chain=solana")
BIRDEYE_KEY = os.getenv("BIRDEYE_KEY", "")

MIN_LIQ_USD = float(os.getenv("MIN_LIQ_USD", "1500"))
MAX_LIQ_USD = float(os.getenv("MAX_LIQ_USD", "25000"))
FDV_MIN     = float(os.getenv("FDV_MIN", "20000"))
FDV_MAX     = float(os.getenv("FDV_MAX", "80000"))
MAX_AGE_MIN = int(os.getenv("MAX_AGE_MIN", "360"))
PREF_AGE_MIN= int(os.getenv("PREF_AGE_MIN", "40"))
SPREAD_MAX  = float(os.getenv("SPREAD_MAX", "1.5"))

W_MOMENTUM_BUYS     = float(os.getenv("W_MOMENTUM_BUYS", "2.0"))
W_MOMENTUM_BUYDOM   = float(os.getenv("W_MOMENTUM_BUYDOM", "1.5"))
W_AGE_PREFERRED     = float(os.getenv("W_AGE_PREFERRED", "1.0"))
W_PRICE_M5_UP       = float(os.getenv("W_PRICE_M5_UP", "1.0"))
MIN_SCORE_TO_ALERT  = float(os.getenv("MIN_SCORE_TO_ALERT", "2.5"))

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

POLL_SEC = int(os.getenv("POLL_SEC", "35"))
BASE_BACKOFF_SEC = int(os.getenv("BASE_BACKOFF_SEC", "60"))
MAX_BACKOFF_SEC  = int(os.getenv("MAX_BACKOFF_SEC", "300"))

# ------------------- Globals -----------------------------
SEEN = set()
START_TIME = time.time()
TEST_DURATION = 5 * 60  # 5 minutes
CONSEC_429 = 0

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "pumpbonk-worker/4.0 (+https://render.com)"})
if BIRDEYE_KEY:
    SESSION.headers.update({"x-api-key": BIRDEYE_KEY})

# ------------------- Helpers -----------------------------
def mins_since(ts_ms):
    try:
        now_ms = int(time.time() * 1000)
        return (now_ms - int(ts_ms)) / 1000.0 / 60.0
    except Exception:
        return None

def get_nested(d, path, default=None):
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur

def pairs_from_any_response(data):
    if not data:
        return []
    if isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list):
        return data["pairs"]
    if isinstance(data, dict) and "results" in data and isinstance(data["results"], list):
        return data["results"]
    return []

def http_get_json(url, timeout=10):
    try:
        r = SESSION.get(url, timeout=timeout)
        if r.status_code == 429:
            return None, True
        r.raise_for_status()
        return r.json(), False
    except requests.exceptions.HTTPError as e:
        log.warning(f"GET {url} failed: {e}")
        return None, False
    except Exception as e:
        log.warning(f"GET {url} failed: {e}")
        return None, False

# ------------------- Data sources ------------------------
def fetch_dexscreener():
    urls = [u.strip() for u in DEX_URLS.split(",") if u.strip()]
    pairs = []
    ratelimited = False
    for u in urls:
        data, limited = http_get_json(u)
        if limited:
            ratelimited = True
            break
        fetched = pairs_from_any_response(data) or []
        pairs.extend(fetched)
    return pairs, ratelimited

def fetch_birdeye():
    url = BIRDEYE_API
    try:
        r = SESSION.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        tokens = data.get("data", {}).get("tokens", [])
        pairs = []
        for t in tokens:
            pairs.append({
                "chainId": "solana",
                "dexId": "pumpfun",
                "pairAddress": t.get("address"),
                "baseToken": {"symbol": t.get("symbol"), "address": t.get("address")},
                "liquidity": {"usd": t.get("liquidity", 0)},
                "fdv": t.get("fdv", 0),
                "pairCreatedAt": int(time.time() * 1000),
                "url": f"https://birdeye.so/token/{t.get('address')}?chain=solana"
            })
        return pairs
    except Exception as e:
        log.warning(f"Birdeye fetch failed: {e}")
        return []

# ------------------- Filters ------------------------------
def pass_hard_gates(pair):
    chain = pair.get("chainId") or pair.get("chain")
    dexid = pair.get("dexId")
    if str(chain).lower() != "solana":
        return False, {"reason": f"chainId {chain}"}
    if str(dexid).lower() != "pumpfun":
        return False, {"reason": f"dexId {dexid}"}

    age = mins_since(pair.get("pairCreatedAt"))
    if age is None or age > MAX_AGE_MIN:
        return False, {"reason": f"age {age}"}

    liq = get_nested(pair, "liquidity.usd")
    if liq is None:
        return False, {"reason": "no liquidity"}

    fdv = pair.get("fdv")
    if fdv is None or fdv < FDV_MIN or fdv > FDV_MAX:
        return False, {"reason": f"fdv {fdv}"}

    spread = pair.get("priceSpread")
    if spread is not None:
        try:
            if float(spread) > SPREAD_MAX:
                return False, {"reason": f"spread {spread}"}
        except Exception:
            pass

    return True, {"reason": "pass"}

def soft_score(pair):
    score = 0.0
    buys = get_nested(pair, "txns.m5.buys", 0) or 0
    sells = get_nested(pair, "txns.m5.sells", 0) or 0
    if buys >= 12:
        score += W_MOMENTUM_BUYS
    if buys >= sells and (buys + sells) >= 6:
        score += W_MOMENTUM_BUYDOM

    age = mins_since(pair.get("pairCreatedAt"))
    if age is not None and age <= PREF_AGE_MIN:
        score += W_AGE_PREFERRED

    m5 = get_nested(pair, "priceChange.m5", 0.0) or 0.0
    if isinstance(m5, (int, float)) and m5 >= 0:
        score += W_PRICE_M5_UP

    return score

# ------------------- Telegram -----------------------------
def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code != 200:
            log.warning(f"Telegram send failed: {r.status_code} {r.text}")
    except Exception as e:
        log.warning(f"Telegram send exception: {e}")

# ------------------ Core Loop -----------------------------
def process_once():
    global CONSEC_429
    elapsed = time.time() - START_TIME
    test_mode = elapsed < TEST_DURATION

    min_liq = 500 if test_mode else MIN_LIQ_USD
    max_liq = 50000 if test_mode else MAX_LIQ_USD
    min_score = 1.0 if test_mode else MIN_SCORE_TO_ALERT

    pairs = []
    dex_pairs, ratelimited = fetch_dexscreener()
    if ratelimited:
        CONSEC_429 += 1
        backoff = min(BASE_BACKOFF_SEC * (2 ** (CONSEC_429 - 1)), MAX_BACKOFF_SEC)
        jitter = random.randint(0, 10)
        log.warning(f"429 Too Many Requests – backing off for {backoff + jitter}s (consec={CONSEC_429})")
        time.sleep(backoff + jitter)
        return
    else:
        if CONSEC_429 > 0:
            log.info("Rate limit recovered; resetting backoff counter.")
        CONSEC_429 = 0
        pairs.extend(dex_pairs)

    # add Birdeye fallback
    pairs.extend(fetch_birdeye())

    total = len(pairs)
    alerts = 0

    for p in pairs:
        pid = p.get("pairAddress") or get_nested(p, "baseToken.address") or json.dumps(p, sort_keys=True)[:64]
        if pid in SEEN:
            continue
        ok, _ = pass_hard_gates(p)
        if not ok:
            continue
        liq = get_nested(p, "liquidity.usd", 0)
        if liq < min_liq or liq > max_liq:
            continue
        sc = soft_score(p)
        if sc >= min_score:
            SEEN.add(pid)
            symbol = get_nested(p, "baseToken.symbol") or get_nested(p, "baseToken.name") or "UNK"
            url = p.get("url") or (f"https://dexscreener.com/solana/{p.get('pairAddress')}" if p.get("pairAddress") else "")
            msg = f"🟢 ALERT {symbol} (TEST={test_mode}) | LIQ={liq} | SCORE={round(sc,2)}\n{url}"
            log.info(msg.replace("\n", " | "))
            tg_send(msg)
            alerts += 1

    log.info(f"Tick {datetime.now().strftime('%H:%M:%S')} – checked {total} tokens – {alerts} alerts (test_mode={test_mode})")

def run():
    log.info("Worker v4 running (Dexscreener+Birdeye, test mode 5min, RL handling)...")
    while True:
        try:
            process_once()
        except Exception:
            log.exception("process_once error")
        time.sleep(POLL_SEC + random.randint(0,5))

if __name__ == "__main__":
    run()
