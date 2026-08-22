"""
TOS Alert → Telegram Bot (v5)
Real-time Finviz screenshot + Yahoo Finance ma'lumotlari
"""

import sys
import imaplib
import email
import time
from imapclient import IMAPClient
import re
import os
import threading
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from services.chart import get_chart_safe
from PIL import Image
from PIL import ImageEnhance
import io

from http.server import BaseHTTPRequestHandler, HTTPServer

# MUHIM: Render/Docker'da stdout odatda BLOK bo'lib buferlanadi (satr
# bo'yicha emas), shuning uchun print() qilingan loglar darhol emas,
# katta to'plamlarda kechikib ko'rinishi mumkin — bu esa "kod osilib
# qoldi" deb noto'g'ri xulosaga olib kelishi mumkin. Shu sababli stdout
# va stderr'ni majburan satr-bo'yicha (unbuffered) rejimga o'tkazamiz.
# multiprocessing "spawn" bilan yaratilgan child processlar ham buni
# meros qilib oladi (environment orqali).
os.environ["PYTHONUNBUFFERED"] = "1"
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass


class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"TOS Telegram Bot is running")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))

    server = HTTPServer(("0.0.0.0", port), HealthHandler)

    print(f"[Server] HTTP server port {port} da ishga tushdi")

    server.serve_forever()


# DIQQAT: bu yerda threading.Thread(...).start() ATAYLAB yo'q — u faqat
# quyidagi `if __name__ == "__main__":` bloki ichida ishga tushiriladi.
# Sabab: services/chart.py multiprocessing("spawn") orqali grafik olish
# uchun yangi Python jarayoni ochganda, "spawn" usuli butun faylni
# QAYTADAN import qiladi. Agar shu thread module darajasida (import
# vaqtida) ishga tushirilsa, har bir yangi grafik-jarayon ham xuddi shu
# portga ulanishga urinib, "Address already in use" xatosini beradi.


load_dotenv()

GMAIL_USER       = os.getenv("GMAIL_USER")
GMAIL_APP_PASS   = os.getenv("GMAIL_APP_PASSWORD")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINVIZ_EMAIL     = os.getenv("FINVIZ_EMAIL")
FINVIZ_PASSWORD  = os.getenv("FINVIZ_PASSWORD")

# Render'da bu o'zgaruvchi avtomatik o'rnatiladi (masalan
# https://tos-telegram-bot.onrender.com) — o'z-o'zini "uyg'oq" ushlab
# turish (keep-alive) uchun ishlatiladi.
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

TOS_SENDER       = "alerts@thinkorswim.com"
IDLE_TIMEOUT     = 5 * 60    # 5 daqiqa — tarmoq ulanishini muntazam yangilab turamiz
                              # (Railway'ning tarmoq proksisi uzoq bo'sh ulanishlarni
                              # ~40 daqiqada o'zi yopib qo'yishi kuzatilgan, shuning
                              # uchun undan oldinroq o'zimiz proaktiv yangilaymiz)
FALLBACK_POLL    = 300       # 5 daqiqada bir ehtiyot uchun tekshiruv (IDLE signalni oʻtkazib yuborsa)
MIN_RVOL         = 1.0
RSI_MIN          = 30
RSI_MAX          = 80
SENT_IDS_FILE    = "sent_ids.txt"

def load_sent_ids() -> set:
    if not os.path.exists(SENT_IDS_FILE):
        return set()
    with open(SENT_IDS_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())

def save_sent_id(msg_id: str):
    with open(SENT_IDS_FILE, "a") as f:
        f.write(msg_id + "\n")

ALREADY_SENT = load_sent_ids()

# ── check_email() uchun qulf (lock) ─────────────────────────────────────────
# IDLE tsikli check_email()ni bir necha marta ketma-ket (+3s, +8s) chaqiradi,
# ehtiyot uchun. Lekin agar birinchi chaqiruv hali tugamagan bo'lsa (masalan
# chart yuklanayotgan bo'lsa), keyingi chaqiruv xuddi shu emailni ALREADY_SENT
# ga hali qo'shilmagan deb topib, QAYTA yuborishi mumkin edi (race condition).
# Shu qulf shuni oldini oladi — bir vaqtning o'zida faqat bitta check_email()
# ishga tushadi, qolganlari jim o'tkazib yuboriladi.
_check_lock = threading.Lock()

# ── Ticker cooldown ────────────────────────────────────────────────────────
# TOS ba'zan bir xil ticker uchun scanner qayta ishga tushganda YANGI,
# alohida email yuboradi (Message-ID/UID farqli). Email darajasidagi dedup
# buni "yangi" deb hisoblaydi. Shuning uchun ticker+scanner darajasida
# qo'shimcha cooldown qo'yamiz — bir xil kombinatsiya belgilangan vaqt
# ichida qayta Telegram'ga yuborilmaydi.
TICKER_COOLDOWN_MIN = 30
_last_sent_ticker = {}

def _ticker_recently_sent(ticker: str, scanner_name: str) -> bool:
    key = f"{ticker}|{scanner_name}"
    last = _last_sent_ticker.get(key)
    if last and (datetime.now() - last).total_seconds() < TICKER_COOLDOWN_MIN * 60:
        return True
    return False

def _mark_ticker_sent(ticker: str, scanner_name: str):
    key = f"{ticker}|{scanner_name}"
    _last_sent_ticker[key] = datetime.now()

# ── Finviz grafik (proksi orqali) ────────────────────────────────────────────
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "25a1884447a69ac9773958347c108f59")

def get_finviz_via_proxy(ticker: str) -> bytes | None:
    """ScraperAPI (asosiy) + bepul proksilar (zaxira) orqali Finviz grafigini oladi."""
    finviz_url = f"https://charts.finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=d&s=l&_={int(time.time())}"

    import urllib.parse
    encoded = urllib.parse.quote(finviz_url, safe="")

    proxies = [
        f"https://api.scraperapi.com/?api_key={SCRAPERAPI_KEY}&url={finviz_url}&render=false",
        f"https://api.allorigins.win/raw?url={encoded}",
        f"https://api.codetabs.com/v1/proxy?quest={finviz_url}",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://finviz.com/",
        "Accept": "image/png,image/*,*/*",
    }

    for i, proxy_url in enumerate(proxies):
        try:
            proxy_timeout = 40 if "scraperapi" in proxy_url else 20
            resp = requests.get(proxy_url, headers=headers, timeout=proxy_timeout)
            if resp.status_code == 200 and resp.content[:4] == b'\x89PNG':
                print(f"[Finviz proksi #{i+1}] {ticker} grafigi olindi ({len(resp.content)} bayt)")
                return resp.content
            else:
                print(f"[Finviz proksi #{i+1}] muvaffaqiyatsiz (status={resp.status_code}, size={len(resp.content)})")
        except Exception as e:
            print(f"[Finviz proksi #{i+1} xato] {e}")
        time.sleep(0.3)

    return None

# ── Texnik indikatorlar ───────────────────────────────────────────────────────
def calc_rsi(closes: pd.Series, period: int = 14) -> float:
    try:
        if len(closes) < period + 1:
            return 0.0
        delta = closes.diff().dropna()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta.clip(upper=0)).rolling(period).mean()
        rs    = gain / loss
        rsi   = 100 - (100 / (1 + rs))
        val   = rsi.dropna().iloc[-1]
        return round(float(val), 1) if not pd.isna(val) else 0.0
    except Exception:
        return 0.0

def calc_macd(closes: pd.Series) -> str:
    try:
        if len(closes) < 26:
            return "N/A"
        ema12  = closes.ewm(span=12, adjust=False).mean()
        ema26  = closes.ewm(span=26, adjust=False).mean()
        macd   = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        return "Bullish ↑" if macd.iloc[-1] > signal.iloc[-1] else "Bearish ↓"
    except Exception:
        return "N/A"

# ── Yahoo Finance ─────────────────────────────────────────────────────────────
YAHOO_CACHE = {}
YAHOO_CACHE_TTL = 300  # 5 daqiqa

# Company/Sector/Market Cap uchun stock.info chaqiruvi Yahoo'ning eng tez
# rate-limit qiladigan endpointi (quoteSummary). Render'ning umumiy IP'ida
# bu tez-tez "Too Many Requests" beradi va HAR safar chaqirilgani uchun
# asl narx/tarix so'rovini ham bloklanish xavfiga qo'yadi. Standart holatda
# o'chirilgan — kerak bo'lsa FETCH_COMPANY_INFO=true bilan yoqiladi.
FETCH_COMPANY_INFO = os.getenv("FETCH_COMPANY_INFO", "false").lower() == "true"

def format_number(n) -> str:
    n = float(n or 0)

    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"

    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"

    if n >= 1_000:
        return f"{n/1_000:.2f}K"

    return str(round(n, 2))


def _fetch_history_with_retry(stock, retries: int = 2, delay: float = 3.0):
    """
    Yahoo Finance vaqti-vaqti bilan (ayniqsa umumiy/paylashilgan IP'larda,
    masalan Render) "Too Many Requests" qaytaradi. Bir necha marta qisqa
    kutish bilan qayta urinamiz.
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            hist = stock.history(period="1y", interval="1d", auto_adjust=False)
            if not hist.empty:
                return hist
            last_err = "bo'sh natija"
        except Exception as e:
            last_err = e
        if attempt < retries:
            time.sleep(delay)
    print(f"[Yahoo xato] history: {last_err}")
    return None


def get_stock_info(ticker: str) -> dict:

    ticker = ticker.upper().strip()

    now = time.time()
    cached = YAHOO_CACHE.get(ticker)
    if cached:
        cached_time, cached_data = cached
        if now - cached_time < YAHOO_CACHE_TTL:
            print(f"[Yahoo] {ticker}: cache ishlatildi")
            return cached_data

    try:
        print(f"[Yahoo] {ticker}: ma'lumot olinmoqda...")

        stock = yf.Ticker(ticker)
        hist = _fetch_history_with_retry(stock)

        if hist is None or hist.empty:
            print(f"[Yahoo xato] {ticker}: history bo'sh")
            return {}

        closes = hist["Close"].dropna()
        if closes.empty:
            print(f"[Yahoo xato] {ticker}: Close ma'lumoti yo'q")
            return {}

        price = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else price
        change_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0.0

        volume = int(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else 0
        avg_vol = int(hist["Volume"].tail(20).mean()) if "Volume" in hist.columns else 0
        rvol = round(volume / avg_vol, 2) if avg_vol else 0.0

        rsi = calc_rsi(closes)
        macd_trend = calc_macd(closes)

        support = round(float(hist["Low"].min()), 2)
        resistance = round(float(hist["High"].max()), 2)

        company = ticker
        sector = "N/A"
        market_cap = 0

        if FETCH_COMPANY_INFO:
            try:
                info = stock.info
                company = info.get("longName") or info.get("shortName") or ticker
                sector = info.get("sector") or "N/A"
                market_cap = info.get("marketCap") or 0
            except Exception as e:
                print(f"[Yahoo] {ticker}: info olinmadi: {e}")

        result = {
            "price": price,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "volume": volume,
            "avg_vol": avg_vol,
            "rvol": rvol,
            "market_cap": market_cap,
            "sector": sector,
            "company": company,
            "rsi": rsi,
            "macd_trend": macd_trend,
            "support": support,
            "resistance": resistance,
        }

        YAHOO_CACHE[ticker] = (now, result)
        print(f"[Yahoo] {ticker}: muvaffaqiyatli olindi ✅")
        return result

    except Exception as e:
        print(f"[Yahoo xato] {ticker}: {e}")
        return {}

# ── Signal filtri ─────────────────────────────────────────────────────────────
def is_strong_signal(d: dict) -> tuple:
    reasons = []
    if d["rvol"] > 0 and d["rvol"] < MIN_RVOL:
        reasons.append(f"RVol past ({d['rvol']} < {MIN_RVOL})")
    if d["rsi"] > 0 and (d["rsi"] < RSI_MIN or d["rsi"] > RSI_MAX):
        reasons.append(f"RSI chegaradan ({d['rsi']})")
    return (False, " | ".join(reasons)) if reasons else (True, "OK")

# ── Xabar yasash ──────────────────────────────────────────────────────────────
def build_message(ticker: str, scanner_name: str) -> tuple:
    d = get_stock_info(ticker)
    if not d or d["price"] == 0:
        return "", False, "Ma'lumot olinmadi"

    passed, reason = is_strong_signal(d)
    if not passed:
        return "", False, reason

    arrow   = "🟢" if d["change_pct"] >= 0 else "🔴"

    msg = (
        f"🧠 <b>Algorithm:</b> {scanner_name}\n"
        f"📌 <b>Ticker:</b> <code>{ticker}</code>\n"
        f"🏢 <b>Company:</b> {d['company']}\n"
        f"🏭 <b>Sector:</b> {d['sector']}\n"
        f"📊 <b>% Change:</b> {arrow} {d['change_pct']:+.2f}%\n"
        f"📉 <b>Yesterday Vol:</b> {format_number(d['avg_vol'])}\n"
        f"📈 <b>Current Vol:</b> {format_number(d['volume'])}\n"
        f"⚡ <b>RVol:</b> {d['rvol']}\n"
        f"📊 <b>Market Cap:</b> {format_number(d['market_cap'])}\n"
        f"📉 <b>MACD:</b> {d['macd_trend']}\n"
        f"🎯 <b>Support:</b> ${d['support']} | <b>Resistance:</b> ${d['resistance']}\n"
        f"🕐 <b>Time:</b> {datetime.now().strftime('%H:%M, %d-%b-%Y')}"
    )
    return msg, True, "OK"

# ── Telegram ─────────────────────────────────────────────────────────────────
def get_chart_image(ticker: str) -> bytes | None:
    img = get_chart_safe(ticker)

    if not img:
        print(f"[Chart] {ticker} uchun Finviz grafigi olinmadi — o'tkazib yuborildi")
        return None

    try:
        image = Image.open(io.BytesIO(img))
        image = image.resize((image.width * 2, image.height * 2), Image.LANCZOS)
        image = ImageEnhance.Sharpness(image).enhance(1.4)
        image = ImageEnhance.Contrast(image).enhance(1.05)

        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)

        print(f"[Chart] Finviz HD OK: {ticker}")
        return output.getvalue()

    except Exception as e:
        print(f"[Chart] Pillow error: {e}")
        return img

def send_telegram_photo(caption: str, ticker: str) -> bool:
    img_bytes = get_chart_image(ticker)

    if not img_bytes:
        print(f"[Telegram] {ticker} — grafik yo'q, o'tkazib yuborildi (yuborilmadi)")
        return False

    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        resp = requests.post(url,
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": (f"{ticker}.png", img_bytes, "image/png")},
            timeout=90
        )
        if resp.ok:
            print(f"[Telegram] {ticker} grafik bilan yuborildi ✅")
            return True
        print(f"[Telegram xato] {resp.text}")
    except Exception as e:
        print(f"[Telegram xato] {e}")

    return False

def send_telegram_text(text: str):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
    }, timeout=15)
    if not resp.ok:
        print(f"[Telegram matn xato] {resp.text}")

# ── Email parsing ─────────────────────────────────────────────────────────────
def extract_tickers_and_scanner(subject: str, body: str):
    text = subject
    m = re.search(r"added to ([^:]+?)(?:\s*:|\.\s*$|\s+oldin\b)", text, re.IGNORECASE)
    scanner_name = m.group(1).strip() if m else (
        re.search(r"added to (.+?)(?:\.|$)", text, re.IGNORECASE) or type("", (), {"group": lambda s, n: "TOS Scanner"})()
    ).group(1).strip()

    tickers = []
    m2 = re.search(r"symbols?\s*:\s*([\w ,]+?)\s+(?:was|were)\b", text, re.IGNORECASE)
    if m2:
        tickers = [t.strip() for t in m2.group(1).split(",") if re.match(r"^[A-Z]{1,5}$", t.strip())]

    if not tickers and body:
        m3 = re.search(r"symbols?\s*:\s*([\w ,]+?)\s+(?:was|were)\b", body, re.IGNORECASE)
        if m3:
            tickers = [t.strip() for t in m3.group(1).split(",") if re.match(r"^[A-Z]{1,5}$", t.strip())]

    print(f"[Parser] Scanner: '{scanner_name}', Tickers: {tickers}")
    return list(dict.fromkeys(tickers)), scanner_name

# ── Email tekshirish ──────────────────────────────────────────────────────────
def check_email():
    """
    Bir vaqtning o'zida faqat bitta nusxasi ishlashini kafolatlaydi
    (qarang: _check_lock izohi yuqorida).
    """
    if not _check_lock.acquire(blocking=False):
        print("[Email] Oldingi tekshiruv hali tugamagan, bu chaqiruv o'tkazib yuborildi")
        return
    try:
        _check_email_impl()
    finally:
        _check_lock.release()


def _check_email_impl():
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASS)
        mail.select("inbox")

        since = datetime.now().strftime("%d-%b-%Y")
        _, data = mail.uid("search", None, f'(FROM "{TOS_SENDER}" SINCE "{since}")')
        ids = data[0].split()
        print(f"[Email] {len(ids)} ta email topildi (jami, SEEN/UNSEEN farqisiz)")

        for eid in ids:
            uid_str = eid.decode() if isinstance(eid, bytes) else str(eid)
            _, msg_data = mail.uid("fetch", eid, "(RFC822)")
            if not msg_data or msg_data[0] is None:
                continue
            msg     = email.message_from_bytes(msg_data[0][1])
            subject = msg.get("Subject", "")
            msg_id  = msg.get("Message-ID", f"uid-{uid_str}")

            if msg_id in ALREADY_SENT:
                continue

            # MUHIM: emailni ALREADY_SENT'ga DARHOL (ticker'larni qayta
            # ishlashdan OLDIN) qo'shamiz. Aks holda, agar shu email uchun
            # chart yuklash/Telegram'ga yuborish uzoq davom etsa va shu
            # vaqt ichida yana bir check_email() chaqirilsa (garchi lock
            # asosiy race condition'ni oldini olsa ham, ehtiyot chorasi
            # sifatida), email qayta ko'rinmaydi.
            ALREADY_SENT.add(msg_id)
            save_sent_id(msg_id)

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

            print(f"[Email] Subject: {subject}")

            is_new_symbol = re.search(r"New symbols?\s*:", subject, re.IGNORECASE)
            is_following  = re.search(r"Following list", subject, re.IGNORECASE)

            if not is_new_symbol and not is_following:
                print("[Skip] Noma'lum email formati")
                continue

            if is_following and not is_new_symbol:
                m_follow = re.search(r"Following list of (?:symbols? )?(?:were )?added to (.+?)\s+oldin\s*:\s*([A-Z, .]+)", subject, re.IGNORECASE)
                if m_follow:
                    scanner_name = m_follow.group(1).strip().rstrip()
                    raw_tickers  = m_follow.group(2)
                    tickers = [t.strip().rstrip('.') for t in raw_tickers.split(",") if re.match(r"^[A-Z]{1,5}$", t.strip().rstrip('.'))]
                    print(f"[Following] Scanner: '{scanner_name}', Tickers: {tickers}")
                    for ticker in tickers:
                        if _ticker_recently_sent(ticker, scanner_name):
                            print(f"[Cooldown] {ticker} ({scanner_name}) yaqinda yuborilgan, o'tkazib yuborildi")
                            continue
                        caption, passed, reason = build_message(ticker, scanner_name)
                        if not passed:
                            print(f"[Filter] {ticker} o'tmadi: {reason}")
                            continue
                        sent = send_telegram_photo(caption, ticker)
                        if sent:
                            _mark_ticker_sent(ticker, scanner_name)
                            print(f"[Telegram] {ticker} yuborildi ✅")
                        time.sleep(2)
                else:
                    print(f"[Skip] Following list formati tanilmadi: {subject}")
                continue

            tickers, scanner_name = extract_tickers_and_scanner(subject, body)

            for ticker in tickers:
                if _ticker_recently_sent(ticker, scanner_name):
                    print(f"[Cooldown] {ticker} ({scanner_name}) yaqinda yuborilgan, o'tkazib yuborildi")
                    continue
                caption, passed, reason = build_message(ticker, scanner_name)
                if not passed:
                    print(f"[Filter] {ticker} o'tmadi: {reason}")
                    continue
                sent = send_telegram_photo(caption, ticker)
                if sent:
                    _mark_ticker_sent(ticker, scanner_name)
                    print(f"[Telegram] {ticker} yuborildi ✅")
                time.sleep(2)

    except Exception as e:
        print(f"[Xato] {e}")
    finally:
        try:
            if mail:
                mail.logout()
        except Exception:
            pass

# ── Render uchun keep-alive (bepul tarif uzoq harakatsizlikdan keyin
#    o'zini o'chirib qo'yadi, shuning uchun o'z-o'ziga davriy so'rov
#    yuborib "uyg'oq" ushlab turamiz) ─────────────────────────────────────
def keep_alive_loop():
    if not RENDER_EXTERNAL_URL:
        return
    while True:
        time.sleep(600)  # 10 daqiqada bir
        try:
            requests.get(RENDER_EXTERNAL_URL, timeout=15)
            print("[Keep-alive] Ping yuborildi")
        except Exception as e:
            print(f"[Keep-alive xato] {e}")


# ── IMAP IDLE tsikli ──────────────────────────────────────────────────────────
def imap_idle_loop():
    while True:
        client = None
        try:
            client = IMAPClient("imap.gmail.com", ssl=True)
            client.login(GMAIL_USER, GMAIL_APP_PASS)
            client.select_folder("INBOX")
            print("📡 IMAP IDLE rejimida kutilmoqda (email kelishi bilan darhol javob beradi)...")

            check_email()
            last_poll = time.time()

            while True:
                client.idle()
                try:
                    responses = client.idle_check(timeout=IDLE_TIMEOUT)
                finally:
                    client.idle_done()

                if responses:
                    print(f"[IDLE] Yangi faoliyat aniqlandi -> tekshirilmoqda")
                    time.sleep(3)
                    check_email()
                    time.sleep(5)
                    check_email()
                    last_poll = time.time()

                if time.time() - last_poll > FALLBACK_POLL:
                    print("[IDLE] Ehtiyot uchun davriy tekshiruv")
                    check_email()
                    last_poll = time.time()

        except Exception as e:
            print(f"[IDLE xato] {e} -> 15s dan keyin qayta ulanamiz")
            try:
                if client:
                    client.logout()
            except Exception:
                pass
            time.sleep(15)


# ── Asosiy tsikl ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()

    print("🚀 TOS → Telegram bot v6 (IMAP IDLE, Render) ishga tushdi!")
    print(f"   Gmail: {GMAIL_USER}")
    print(f"   Kanal: {TELEGRAM_CHAT_ID}")
    print(f"   Rejim: IMAP IDLE (real-time, polling yo'q)")
    print(f"   Ehtiyot tekshiruvi: har {FALLBACK_POLL // 60} daqiqada")
    print(f"   Filter: RVol>={MIN_RVOL}, RSI {RSI_MIN}-{RSI_MAX}")
    company_info_status = "yoqilgan" if FETCH_COMPANY_INFO else "o'chirilgan (Yahoo rate-limit kamaytirish uchun)"
    print(f"   Company info so'rovi: {company_info_status}\n")

    if RENDER_EXTERNAL_URL:
        threading.Thread(target=keep_alive_loop, daemon=True).start()
        print(f"   Keep-alive: {RENDER_EXTERNAL_URL} ga har 10 daqiqada ping\n")

    check_email()
    imap_idle_loop()
