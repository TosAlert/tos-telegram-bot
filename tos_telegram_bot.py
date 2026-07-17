"""
TOS Alert → Telegram Bot (v5)
Real-time Finviz screenshot + Yahoo Finance ma'lumotlari
"""

import imaplib
import email
import time
import re
import os
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from services.chart import get_chart, get_chart_and_info
from PIL import Image
from PIL import ImageEnhance
import io

load_dotenv()

GMAIL_USER       = os.getenv("GMAIL_USER")
GMAIL_APP_PASS   = os.getenv("GMAIL_APP_PASSWORD")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINVIZ_EMAIL     = os.getenv("FINVIZ_EMAIL")
FINVIZ_PASSWORD  = os.getenv("FINVIZ_PASSWORD")

TOS_SENDER       = "alerts@thinkorswim.com"
CHECK_INTERVAL   = 30
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

# ── Finviz matnli qiymatlarni raqamga o'girish ───────────────────────────────
def _parse_finviz_number(s: str) -> float:
    """'1.23M', '4.5B', '850.30K', '12.34' kabi matnlarni floatga o'giradi."""
    if not s or s in ("-", "N/A"):
        return 0.0
    s = s.strip().replace(",", "").replace("%", "").replace("+", "")
    mult = 1
    if s.endswith("B"):
        mult, s = 1_000_000_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("K"):
        mult, s = 1_000, s[:-1]
    try:
        return float(s) * mult
    except Exception:
        return 0.0

def _parse_finviz_price(s: str) -> float:
    """Narx matnidan (masalan '$59.82' yoki '59.82') raqam ajratadi."""
    if not s:
        return 0.0
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s)
    except Exception:
        return 0.0

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

# ── Matplotlib bilan candlestick grafik (zaxira) ─────────────────────────────
def get_matplotlib_chart(ticker: str) -> bytes | None:
    """Yahoo Finance dan data olib, matplotlib bilan Finviz uslubida grafik yasaydi."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as patches
        import io

        stock = yf.Ticker(ticker)
        hist  = stock.history(period="6mo")
        if hist.empty or len(hist) < 5:
            print(f"[Chart] {ticker} data yoq")
            return None

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(12, 7),
            gridspec_kw={"height_ratios": [3, 1]},
            facecolor="#0d1117"
        )

        # Candlestick
        ax1.set_facecolor("#0d1117")
        for i, (_, row) in enumerate(hist.iterrows()):
            bull  = row["Close"] >= row["Open"]
            color = "#00b386" if bull else "#ff4444"
            ax1.plot([i, i], [row["Low"], row["High"]], color=color, linewidth=0.9, zorder=1)
            h = max(abs(row["Close"] - row["Open"]), row["Close"] * 0.001)
            rect = patches.Rectangle(
                (i - 0.35, min(row["Open"], row["Close"])),
                0.7, h, linewidth=0, facecolor=color, zorder=2
            )
            ax1.add_patch(rect)

        # Moving averages
        close = hist["Close"]
        x = range(len(close))
        if len(close) >= 20:
            ax1.plot(x, close.rolling(20).mean(), color="#f0a500", linewidth=1.2, label="SMA 20")
        if len(close) >= 50:
            ax1.plot(x, close.rolling(50).mean(), color="#7b68ee", linewidth=1.2, label="SMA 50")
        if len(close) >= 200:
            ax1.plot(x, close.rolling(200).mean(), color="#ff7f50", linewidth=1.2, label="SMA 200")

        # X labels
        step = max(1, len(hist) // 8)
        ax1.set_xticks(range(0, len(hist), step))
        ax1.set_xticklabels(
            [hist.index[i].strftime("%b %d") for i in range(0, len(hist), step)],
            color="#8b949e", fontsize=8, rotation=0
        )
        ax1.tick_params(axis="y", colors="#8b949e", labelsize=9)
        ax1.set_xlim(-1, len(hist))
        ax1.set_title(f"{ticker}  ·  Daily  ·  6mo", color="#e6edf3", fontsize=13, pad=8, loc="left")
        ax1.legend(facecolor="#161b22", labelcolor="#e6edf3", fontsize=8, loc="upper left")
        for sp in ax1.spines.values():
            sp.set_color("#30363d")
        ax1.yaxis.grid(True, color="#21262d", linewidth=0.6)
        ax1.set_axisbelow(True)

        # Volume
        ax2.set_facecolor("#0d1117")
        vol_colors = ["#00b386" if c >= o else "#ff4444"
                      for c, o in zip(hist["Close"], hist["Open"])]
        ax2.bar(range(len(hist)), hist["Volume"] / 1_000_000, color=vol_colors, alpha=0.85)
        ax2.set_ylabel("Vol M", color="#8b949e", fontsize=8)
        ax2.tick_params(colors="#8b949e", labelsize=7)
        ax2.set_xlim(-1, len(hist))
        for sp in ax2.spines.values():
            sp.set_color("#30363d")
        ax2.yaxis.grid(True, color="#21262d", linewidth=0.5)
        ax2.set_axisbelow(True)

        plt.tight_layout(pad=1.2)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#0d1117")
        buf.seek(0)
        img = buf.read()
        plt.close()
        print(f"[Chart] {ticker} grafigi yaratildi ({len(img)//1024}KB)")
        return img
    except Exception as e:
        print(f"[Chart xato] {ticker}: {e}")
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

# ── Ma'lumot: Finviz (asosiy) + Yahoo Finance (RSI/MACD/S-R va zaxira) ───────
def get_stock_info(ticker: str, finviz_data: dict = None) -> dict:
    """
    finviz_data berilsa (parse_finviz_info natijasi), narx/hajm/sektor/company
    o'shandan olinadi. RSI, MACD, Support/Resistance har doim Yahoo Finance
    tarixiy narxlaridan hisoblanadi (Finviz sahifasida bu ma'lumot yo'q).
    finviz_data bo'sh/yaroqsiz bo'lsa — hammasi Yahoo Finance'dan olinadi (zaxira).
    """
    price = change_pct = volume = avg_vol = rvol = market_cap = 0.0
    sector = "N/A"
    company = ticker

    # 1) Finviz'dan asosiy ma'lumotlar
    if finviz_data:
        price      = _parse_finviz_price(finviz_data.get("price", ""))
        change_pct = _parse_finviz_number(finviz_data.get("change_pct", ""))
        volume     = _parse_finviz_number(finviz_data.get("volume", ""))
        avg_vol    = _parse_finviz_number(finviz_data.get("avg_volume", ""))
        market_cap = _parse_finviz_number(finviz_data.get("market_cap", ""))
        sector     = finviz_data.get("sector") or "N/A"
        company    = finviz_data.get("company") or ticker
        rvol       = round(volume / avg_vol, 2) if avg_vol else 0.0
        if price:
            print(f"[Finviz] {ticker} ma'lumotlar Finviz'dan olindi (price=${price})")

    # 2) Yahoo Finance — RSI/MACD/S-R uchun har doim kerak, va Finviz
    #    ma'lumot bermagan bo'lsa narx/hajm uchun ham zaxira bo'ladi
    rsi, macd_trend, support, resistance = 0.0, "N/A", 0.0, 0.0
    try:
        stock = yf.Ticker(ticker)

        if not price:
            info = stock.info
            price = float(
                info.get("currentPrice") or
                info.get("regularMarketPrice") or
                info.get("navPrice") or 0.0
            )
            prev_close = float(info.get("previousClose") or info.get("regularMarketPreviousClose") or 0.0)
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
            volume     = int(info.get("volume") or info.get("regularMarketVolume") or 0)
            avg_vol    = int(info.get("averageVolume") or 0)
            rvol       = round(volume / avg_vol, 2) if avg_vol else 0.0
            market_cap = info.get("marketCap") or 0
            sector     = info.get("sector") or sector
            company    = info.get("longName") or info.get("shortName") or company
            print(f"[Yahoo] {ticker} ma'lumotlar Yahoo Finance'dan olindi (zaxira)")

        hist = stock.history(period="1y")
        if not hist.empty:
            closes     = hist["Close"].dropna()
            rsi        = calc_rsi(closes)
            macd_trend = calc_macd(closes)
            support    = round(float(hist["Low"].min()), 2)
            resistance = round(float(hist["High"].max()), 2)
    except Exception as e:
        print(f"[Yahoo xato] {ticker}: {e}")

    if not price:
        return {}

    return {
        "company": company, "sector": sector,
        "price": price, "change_pct": change_pct,
        "volume": volume, "avg_volume": avg_vol, "rvol": rvol,
        "market_cap": market_cap, "rsi": rsi,
        "macd_trend": macd_trend, "support": support, "resistance": resistance,
    }

def format_number(n) -> str:
    n = float(n or 0)
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.2f}M"
    if n >= 1_000:         return f"{n/1_000:.2f}K"
    return str(round(n, 2))

# ── Signal filtri ─────────────────────────────────────────────────────────────
def is_strong_signal(d: dict) -> tuple:
    reasons = []
    if d["rvol"] > 0 and d["rvol"] < MIN_RVOL:
        reasons.append(f"RVol past ({d['rvol']} < {MIN_RVOL})")
    if d["rsi"] > 0 and (d["rsi"] < RSI_MIN or d["rsi"] > RSI_MAX):
        reasons.append(f"RSI chegaradan ({d['rsi']})")
    return (False, " | ".join(reasons)) if reasons else (True, "OK")

# ── Xabar yasash ──────────────────────────────────────────────────────────────
def build_message(ticker: str, scanner_name: str, finviz_data: dict = None) -> tuple:
    d = get_stock_info(ticker, finviz_data)
    if not d or d["price"] == 0:
        return "", False, "Ma'lumot olinmadi"

    passed, reason = is_strong_signal(d)
    if not passed:
        return "", False, reason

    arrow   = "🟢" if d["change_pct"] >= 0 else "🔴"
    rsi     = d["rsi"]
    rsi_label = (f"{rsi} ⚠️ Overbought" if rsi >= 70
                 else f"{rsi} ⚠️ Oversold" if 0 < rsi <= 30
                 else str(rsi) if rsi > 0 else "N/A")

    msg = (
        f"🧠 <b>Algorithm:</b> {scanner_name}\n"
        f"📌 <b>Ticker:</b> <code>{ticker}</code>\n"
        f"🏢 <b>Company:</b> {d['company']}\n"
        f"🏭 <b>Sector:</b> {d['sector']}\n"
        f"💰 <b>Price:</b> ${d['price']:.2f}\n"
        f"📊 <b>% Change:</b> {arrow} {d['change_pct']:+.2f}%\n"
        f"📉 <b>Yesterday Vol:</b> {format_number(d['avg_volume'])}\n"
        f"📈 <b>Current Vol:</b> {format_number(d['volume'])}\n"
        f"⚡ <b>RVol:</b> {d['rvol']}\n"
        f"📊 <b>Market Cap:</b> {format_number(d['market_cap'])}\n"
        f"〰️ <b>RSI (14):</b> {rsi_label}\n"
        f"📉 <b>MACD:</b> {d['macd_trend']}\n"
        f"🎯 <b>Support:</b> ${d['support']} | <b>Resistance:</b> ${d['resistance']}\n"
        f"🕐 <b>Time:</b> {datetime.now().strftime('%H:%M, %d-%b-%Y')}"
    )
    return msg, True, "OK"

# ── Telegram ─────────────────────────────────────────────────────────────────
def process_ticker_and_send(ticker: str, scanner_name: str):
    """
    1) Avval Yahoo Finance orqali TEZ RVol/RSI oldindan tekshiradi.
       Filtrdan o'tmasa — Finviz'ga umuman murojaat qilinmaydi (vaqt tejaladi).
    2) Filtrdan o'tsa, Finviz sahifasi ochilib, HAM grafik, HAM aniq
       matnli ma'lumotlar (narx, sektor, hajm) olinadi.
    3) Yakuniy xabar shu Finviz ma'lumotlari bilan yasaladi va yuboriladi.
    """
    # 1) Tezkor oldindan tekshiruv — faqat Yahoo Finance orqali
    pre_data = get_stock_info(ticker)
    if not pre_data or pre_data.get("price", 0) == 0:
        print(f"[Filter] {ticker}: dastlabki ma'lumot olinmadi, o'tkazib yuborildi")
        return

    passed, reason = is_strong_signal(pre_data)
    if not passed:
        print(f"[Filter] {ticker} o'tmadi (Yahoo tekshiruvi): {reason}")
        return

    # 2) Filtrdan o'tdi — endi Finviz'dan grafik + aniq ma'lumot olamiz
    img, finviz_data = get_chart_and_info(ticker)

    caption, passed, reason = build_message(ticker, scanner_name, finviz_data)
    if not passed:
        # Finviz ma'lumoti asosida qayta tekshirilganda ham o'tmasligi mumkin
        # (masalan Finviz narxi biroz farq qilsa), lekin bu kamdan-kam holat
        print(f"[Filter] {ticker} o'tmadi (Finviz tekshiruvi): {reason}")
        return

    img_bytes = None
    if img:
        try:
            image = Image.open(io.BytesIO(img))
            image = image.resize(
                (image.width * 2, image.height * 2),
                Image.LANCZOS,
            )
            image = ImageEnhance.Sharpness(image).enhance(1.4)
            image = ImageEnhance.Contrast(image).enhance(1.05)
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            img_bytes = output.getvalue()
            print(f"[Chart] Finviz HD OK: {ticker}")
        except Exception as e:
            print(f"[Chart] Pillow error: {e}")
            img_bytes = img

    if not img_bytes:
        print("[Chart] Fallback → matplotlib")
        img_bytes = get_matplotlib_chart(ticker)

    if img_bytes:
        try:
            url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            resp = requests.post(url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"photo": (f"{ticker}.png", img_bytes, "image/png")},
                timeout=90
            )
            if resp.ok:
                print(f"[Telegram] {ticker} grafik bilan yuborildi ✅")
                return
            print(f"[Telegram xato] {resp.text}")
        except Exception as e:
            print(f"[Telegram xato] {e}")

    send_telegram_text(caption)

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
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASS)
        mail.select("inbox")

        since = datetime.now().strftime("%d-%b-%Y")
        _, data = mail.search(None, f'(UNSEEN FROM "{TOS_SENDER}" SINCE "{since}")')
        ids = data[0].split()
        print(f"[Email] {len(ids)} ta yangi TOS alert")

        for eid in ids:
            _, msg_data = mail.fetch(eid, "(RFC822)")
            msg     = email.message_from_bytes(msg_data[0][1])
            subject = msg.get("Subject", "")
            msg_id  = msg.get("Message-ID", str(eid))

            if msg_id in ALREADY_SENT:
                continue

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

            print(f"[Email] Subject: {subject}")

            # "New symbol: X was added to Y" yoki "Following list of Y: X" formatlarini qabul qilamiz
            is_new_symbol = re.search(r"New symbols?\s*:", subject, re.IGNORECASE)
            is_following  = re.search(r"Following list", subject, re.IGNORECASE)

            if not is_new_symbol and not is_following:
                print("[Skip] Noma'lum email formati")
                continue

            # "Following list of SCANNER: TICKER1, TICKER2" formatidan ticker olish
            if is_following and not is_new_symbol:
                # Subject: "Alert: Following list of trend + breakout + volume oldin: MGNI."
                m_follow = re.search(r"Following list of (?:symbols? )?(?:were )?added to (.+?)\s+oldin\s*:\s*([A-Z, .]+)", subject, re.IGNORECASE)
                if m_follow:
                    scanner_name = m_follow.group(1).strip().rstrip()
                    raw_tickers  = m_follow.group(2)
                    tickers = [t.strip().rstrip('.') for t in raw_tickers.split(",") if re.match(r"^[A-Z]{1,5}$", t.strip().rstrip('.'))]
                    print(f"[Following] Scanner: '{scanner_name}', Tickers: {tickers}")
                    for ticker in tickers:
                        process_ticker_and_send(ticker, scanner_name)
                        time.sleep(2)
                    ALREADY_SENT.add(msg_id)
                    save_sent_id(msg_id)
                else:
                    print(f"[Skip] Following list formati tanilmadi: {subject}")
                continue

            tickers, scanner_name = extract_tickers_and_scanner(subject, body)

            for ticker in tickers:
                process_ticker_and_send(ticker, scanner_name)
                time.sleep(2)

            ALREADY_SENT.add(msg_id)
            save_sent_id(msg_id)

    except Exception as e:
        print(f"[Xato] {e}")
    finally:
        try:
            if mail:
                mail.logout()
        except Exception:
            pass

# ── Asosiy tsikl ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 TOS → Telegram bot v5 ishga tushdi!")
    print(f"   Gmail: {GMAIL_USER}")
    print(f"   Kanal: {TELEGRAM_CHAT_ID}")
    print(f"   Har {CHECK_INTERVAL}s tekshiradi...")
    print(f"   Filter: RVol>={MIN_RVOL}, RSI {RSI_MIN}-{RSI_MAX}\n")

    while True:
        check_email()
        time.sleep(CHECK_INTERVAL)
