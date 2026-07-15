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
from services.chart import get_chart
from PIL import Image
from PIL import ImageEnhance
import io

load_dotenv()

GMAIL_USER       = os.getenv("GMAIL_USER")
GMAIL_APP_PASS   = os.getenv("GMAIL_APP_PASSWORD")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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

# ── Yahoo Finance ─────────────────────────────────────────────────────────────
def get_stock_info(ticker: str) -> dict:
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info

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
        sector     = info.get("sector") or "N/A"
        company    = info.get("longName") or info.get("shortName") or ticker

        hist = stock.history(period="1y")
        if not hist.empty:
            closes     = hist["Close"].dropna()
            rsi        = calc_rsi(closes)
            macd_trend = calc_macd(closes)
            support    = round(float(hist["Low"].min()), 2)
            resistance = round(float(hist["High"].max()), 2)
        else:
            rsi, macd_trend, support, resistance = 0.0, "N/A", 0.0, 0.0

        return {
            "company": company, "sector": sector,
            "price": price, "change_pct": change_pct,
            "volume": volume, "avg_volume": avg_vol, "rvol": rvol,
            "market_cap": market_cap, "rsi": rsi,
            "macd_trend": macd_trend, "support": support, "resistance": resistance,
        }
    except Exception as e:
        print(f"[Yahoo xato] {ticker}: {e}")
        return {}

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
def build_message(ticker: str, scanner_name: str) -> tuple:
    d = get_stock_info(ticker)
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
def get_chart_image(ticker: str) -> bytes | None:
    img = get_chart(ticker)
    
    if img:
        try:
            image = Image.open(io.BytesIO(img))
            
            # 2x kattalashtirish
            image = image.resize(
            (image.width * 2, image.height * 2),
            Image.LANCZOS,
            )

            # Sharpness
            image = ImageEnhance.Sharpness(image).enhance(1.4)

            # Contrast
            image = ImageEnhance.Contrast(image).enhance(1.05)

            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
        
            print(f"[Chart] Finviz HD OK: {ticker}")
        
            return output.getvalue()

        except Exception as e:
            print(f"[Chart] Pillow error: {e}")
            return img

    print("[Chart] Fallback → matplotlib")
    return get_matplotlib_chart(ticker)

def send_telegram_photo(caption: str, ticker: str):
    img_bytes = get_chart_image(ticker)

    if img_bytes:
        try:
            url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            resp = requests.post(url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"photo": (f"{ticker}.png", img_bytes, "image/png")},
                timeout=30
            )
            if resp.ok:
                print(f"[Telegram] {ticker} grafik bilan yuborildi ✅")
                return
            print(f"[Telegram xato] {resp.text}")
        except Exception as e:
            print(f"[Telegram xato] {e}")

    # Grafik chiqmasa — matn yuboradi
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
                        caption, passed, reason = build_message(ticker, scanner_name)
                        if not passed:
                            print(f"[Filter] {ticker} o'tmadi: {reason}")
                            continue
                        send_telegram_photo(caption, ticker)
                        print(f"[Telegram] {ticker} yuborildi ✅")
                        time.sleep(2)
                    ALREADY_SENT.add(msg_id)
                    save_sent_id(msg_id)
                else:
                    print(f"[Skip] Following list formati tanilmadi: {subject}")
                continue

            tickers, scanner_name = extract_tickers_and_scanner(subject, body)

            for ticker in tickers:
                caption, passed, reason = build_message(ticker, scanner_name)
                if not passed:
                    print(f"[Filter] {ticker} o'tmadi: {reason}")
                    continue
                send_telegram_photo(caption, ticker)
                print(f"[Telegram] {ticker} yuborildi ✅")
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
