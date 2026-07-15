import requests

BASE = "https://finviz.com/api/quote.ashx"


def get_chart(ticker: str):
    url = f"{BASE}?t={ticker.upper()}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/137.0 Safari/537.36"
        ),
        "Referer": "https://finviz.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    r = requests.get(url, headers=headers, timeout=20)
    
    print("HTTP:", r.status_code)
    print("Content-Type:", r.headers.get("content-type"))
    print("Size:", len(r.content))
    print(r.text[:300])

    if r.status_code != 200:
        return None

    if len(r.content) < 10000:
        return None

    return r.content
