import re
import time
import io as _io
import requests
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from playwright.sync_api import Error, TimeoutError

from services.browser import browser_manager

FINVIZ_URL = "https://finviz.com/quote.ashx?t={ticker}&p=d&r=m6"
FINVIZ_DIRECT_CHART_URL = "https://charts2.finviz.com/chart.ashx"

BLOCKED_DOMAINS = [
    "doubleclick.net", "googlesyndication", "google-analytics",
    "googletagmanager", "adsystem", "facebook.net", "amazon-adsystem",
    "criteo", "taboola", "outbrain", "adnxs.com", "adservice.google",
]

DEBUG = True


def log(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs, flush=True)


def _force_light_url(url):
    """Chart URL'idagi temani light ga majburlaydi."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        changed = False
        theme_val = qs.get("theme", [None])[0]
        if theme_val is None:
            qs["theme"] = ["light"]
            changed = True
        elif theme_val.lower() != "light":
            qs["theme"] = ["light"]
            changed = True
        new_query = urlencode(qs, doseq=True)
        new_url = urlunparse(parsed._replace(query=new_query))
        new_url = re.sub(r"theme=dark", "theme=light", new_url, flags=re.IGNORECASE)
        return new_url if (changed or new_url != url) else url
    except Exception:
        if "theme=dark" in url:
            return url.replace("theme=dark", "theme=light")
        if "theme=" not in url:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}theme=light"
        return url


def _is_image_dark(img_bytes, threshold=90):
    """Rasm fonining o'rtacha yorqinligini tekshiradi. Dark bo'lsa True."""
    try:
        from PIL import Image
        import io as _io

        img = Image.open(_io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        points = [
            (5, 5), (w - 5, 5), (5, h - 5), (w - 5, h - 5),
            (w // 2, 3), (3, h // 2),
        ]
        total = 0
        for x, y in points:
            x = max(0, min(w - 1, x))
            y = max(0, min(h - 1, y))
            px = img.getpixel((x, y))
            r, g, b = px[:3]
            total += (r + g + b) / 3
        avg = total / len(points)
        log(f"[Chart] Rasm fon yorqinligi: {avg:.0f} (threshold={threshold})")
        return avg < threshold
    except Exception as e:
        log(f"[Chart] Dark tekshirishda xato: {e}")
        return False


class ChartDownloader:
    def __init__(self):
        browser_manager.start()

    def _block_ads(self, page):
        """Finviz chart uchun og'ir reklama/analytics resurslarini bloklaydi.
        Callback sync bo'lishi shart: chart worker ham Playwright Sync API ishlatadi.
        """
        def handle_route(route):
            try:
                req = route.request
                url = (req.url or "").lower()
                resource_type = req.resource_type

                if any(domain in url for domain in BLOCKED_DOMAINS):
                    route.abort()
                    return

                # 512 MB Render uchun chartga kerak bo'lmagan og'ir resurslar.
                if resource_type in {"font", "media"}:
                    route.abort()
                    return

                route.continue_()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass

        try:
            page.route("**/*", handle_route)
            log("[Chart] Reklama/analytics route filter yoqildi")
        except Exception as e:
            log(f"[Chart] Route filter yoqilmadi: {e}")

    def _safe_click(self, page, locator, label):
        try:
            locator.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            log(f"[Chart] {label}: scroll_into_view timeout, davom etamiz")

        try:
            locator.click(timeout=4000, force=True)
            log(f"[Chart] {label} bosildi (click)")
            return
        except Exception as e:
            log(f"[Chart] {label} click xato: {e}")

        try:
            box = locator.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                page.mouse.move(x, y)
                page.mouse.click(x, y)
                log(f"[Chart] {label} bosildi (mouse coord)")
                return
        except Exception as e:
            log(f"[Chart] {label} mouse click xato: {e}")

        try:
            locator.evaluate("el => el.click()", timeout=4000)
            log(f"[Chart] {label} bosildi (JS click)")
        except Exception as e:
            log(f"[Chart] {label} JS click ham xato: {e}")
            raise

    def _open_page(self, ticker):
        page = browser_manager.new_page()
        self._block_ads(page)

        try:
            page.emulate_media(color_scheme="light")
        except Exception as e:
            log(f"[Chart] emulate_media xato: {e}")

        log(f"[Chart] Page id: {id(page)}")
        log(f"[Chart] Opening {ticker}")

        url = FINVIZ_URL.format(ticker=ticker.upper())
        log(f"[Chart] URL: {url}")

        try:
            page.context.add_cookies([
                {"name": "theme", "value": "light", "domain": ".finviz.com", "path": "/"},
                {"name": "darkMode", "value": "false", "domain": ".finviz.com", "path": "/"},
                {"name": "chartTheme", "value": "light", "domain": ".finviz.com", "path": "/"},
                {"name": "charts", "value": "light", "domain": ".finviz.com", "path": "/"},
            ])
        except Exception as e:
            log(f"[Chart] Cookie sozlashda xato: {e}")

        # "load/networkidle"ni kutmaymiz: Finviz reklama/analytics sabab
        # uzoq vaqt networkni band qilib turishi mumkin. Chart uchun document
        # commit bo'lishining o'zi yetarli; keyin canvasni alohida kutamiz.
        try:
            page.goto(url, wait_until="commit", timeout=15000)
        except TimeoutError:
            log("[Chart] Navigation 15s timeout -> sahifa qisman yuklangan bo'lsa davom etamiz")

        page.set_viewport_size({"width": 1600, "height": 1200})

        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass

        page.wait_for_timeout(700)

        try:
            page.evaluate("""
                () => {
                    const selectors = [
                        '[class*="cookie"]', '[class*="consent"]',
                        '[class*="tooltip"]', '[class*="popup"]',
                        '[class*="banner"]', '[id*="cookie"]',
                        '[class*="new-compare"]', '[class*="promo"]',
                        '.chart-tooltip', '.overlay-tooltip',
                    ];
                    selectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => {
                            el.style.display = 'none';
                            el.remove();
                        });
                    });
                    document.querySelectorAll('div, span, section').forEach(el => {
                        const text = (el.textContent || '').trim();
                        if (text.length > 0 && text.length < 300 &&
                            (text.includes('New Compare') ||
                             text.includes('multi-timeframe') ||
                             text.includes('sector ranking'))) {
                            el.style.display = 'none';
                            el.remove();
                        }
                    });
                }
            """)
        except Exception:
            pass

        # Avval chart canvasini kutamiz. Oddiy "first canvas" reklama canvasiga
        # tushib qolishi mumkin, shuning uchun chart containerlarni ham tekshiramiz.
        chart_ready = False
        for sel in [
            "#chart-container canvas",
            "div[id^='chart'] canvas",
            "div[class*='chart'] canvas",
            "canvas",
        ]:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=12000)
                box = loc.bounding_box()
                if box and box["width"] >= 400 and box["height"] >= 200:
                    chart_ready = True
                    log(f"[Chart] Chart canvas tayyor: {sel} ({int(box['width'])}x{int(box['height'])})")
                    break
            except Exception:
                continue

        if not chart_ready:
            raise TimeoutError("Finviz chart canvas 12 soniyada tayyor bo'lmadi")

        page.wait_for_timeout(500)

        try:
            page.evaluate("""
                () => {
                    document.querySelectorAll(
                        '[class*="tooltip"], [class*="popup"], [class*="new-compare"], [class*="banner"]'
                    ).forEach(el => { el.style.display = 'none'; el.remove(); });
                    document.querySelectorAll('div, span, section').forEach(el => {
                        const text = (el.textContent || '').trim();
                        if (text.length > 0 && text.length < 300 &&
                            (text.includes('New Compare') ||
                             text.includes('multi-timeframe') ||
                             text.includes('sector ranking'))) {
                            el.style.display = 'none';
                            el.remove();
                        }
                    });
                }
            """)
        except Exception:
            pass

        page.wait_for_timeout(300)

        title = page.title()
        log(f"[Chart] Title : {title}")

        if ticker.upper() not in title.upper():
            raise Exception(f"Unexpected Finviz page : {title}")

        return page

    def parse_finviz_info(self, page):
        """Finviz sahifasidan asosiy ma'lumotlarni o'qiydi."""
        try:
            data = page.evaluate("""
                () => {
                    const result = {
                        company: "",
                        sector: "",
                        industry: "",
                        price: "",
                        change_pct: "",
                        volume: "",
                        avg_volume: "",
                        market_cap: ""
                    };

                    const title = document.querySelector("title");
                    if (title) {
                        result.company = title.innerText.split(" Stock")[0].trim();
                    }

                    document.querySelectorAll("table td").forEach(td => {
                        const key = td.innerText.trim();
                        const valueCell = td.nextElementSibling;
                        if (!valueCell) return;
                        const value = valueCell.innerText.trim();
                        switch (key) {
                            case "Sector":
                                result.sector = value;
                                break;
                            case "Industry":
                                result.industry = value;
                                break;
                            case "Market Cap":
                                result.market_cap = value;
                                break;
                            case "Volume":
                                result.volume = value;
                                break;
                            case "Avg Volume":
                                result.avg_volume = value;
                                break;
                        }
                    });

                    const price = document.querySelector("[data-test='instrument-price-last']");
                    if (price)
                        result.price = price.innerText.trim();

                    const change = document.querySelector("[data-test='instrument-price-change']");
                    if (change)
                        result.change_pct = change.innerText.trim();

                    return result;
                }
            """)
            log(f"[Finviz] {data}")
            return data
        except Exception as e:
            log(f"[Finviz Parser] {e}")
            return {
                "company": "",
                "sector": "",
                "industry": "",
                "price": "",
                "change_pct": "",
                "volume": "",
                "avg_volume": "",
                "market_cap": ""
            }

    def _capture_via_share_download(self, page):
        """
        Finviz'dagi Share -> Download orqali ORIGINAL yuqori sifatli
        chart rasmini olish. Har bir bosqich aniq log bilan belgilangan —
        agar kelajakda yana osilib qolsa, LOG orqali AYNAN qaysi bosqichda
        to'xtaganini bilib olamiz.
        """
        log("[Chart] Share -> Download jarayoni boshlandi")

        # 1) Share tugmasini topish
        share_selectors = [
            '[data-testid="chart-toolbar-publish"]',
            'button:has-text("Share")',
            'a:has-text("Share")',
            '[class*="share"]:has-text("Share")',
        ]

        share_btn = None
        for sel in share_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.wait_for(state="visible", timeout=4000)
                    share_btn = loc
                    log(f"[Chart] Share tugmasi topildi: {sel}")
                    break
            except Exception:
                continue

        if share_btn is None:
            raise Exception("Share tugmasi topilmadi")

        # 2) Share bosish
        log("[Chart] Share tugmasini bosishga urinilmoqda...")
        self._safe_click(page, share_btn, "Share tugmasi")
        page.wait_for_timeout(1000)

        # 3) Modal aniqlash (majburiy emas)
        log("[Chart] Modal qidirilmoqda...")
        try:
            modal = page.locator(
                '[role="dialog"], [class*="modal"], [class*="dialog"], [data-testid*="publish"]'
            ).first
            if modal.count() > 0:
                try:
                    modal.wait_for(state="visible", timeout=3000)
                    log("[Chart] Share modal topildi")
                except Exception:
                    log("[Chart] Modal aniq topilmadi, davom etamiz")
        except Exception:
            pass

        # 4) Spinner bo'lsa kutamiz
        log("[Chart] Spinner tekshirilmoqda...")
        spinner_selectors = [
            '[data-testid="charts-publish-chart-spinner"]',
            '[class*="spinner"]',
            '[class*="loading"]',
        ]
        for sel in spinner_selectors:
            try:
                spinner = page.locator(sel).first
                if spinner.count() == 0:
                    continue
                if spinner.is_visible():
                    log(f"[Chart] Spinner topildi: {sel}")
                    try:
                        spinner.wait_for(state="hidden", timeout=8000)
                        log("[Chart] Spinner tugadi")
                    except Exception:
                        log("[Chart] Spinner timeout -> davom etamiz")
                    break
            except Exception:
                continue

        # 5) Download tugmasini kutish (maks. 10s)
        log("[Chart] Download tugmasi qidirilmoqda...")
        download_selectors = [
            'button:has-text("Download")',
            'a:has-text("Download")',
            'button[title*="Download" i]',
            'a[download]',
            '[data-testid*="download"]',
            '[class*="download"]',
        ]

        download_btn = None
        start_time = time.time()
        while time.time() - start_time < 10:
            for sel in download_selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        download_btn = loc
                        log(f"[Chart] Download tugmasi topildi: {sel}")
                        break
                except Exception:
                    continue
            if download_btn is not None:
                break
            page.wait_for_timeout(500)

        if download_btn is None:
            raise Exception("Share modal ochildi, lekin Download tugmasi 10 soniyada topilmadi")

        # 6) Download eventni kutish
        log("[Chart] Download bosilmoqda, fayl kutilmoqda...")
        img_bytes = None
        try:
            with page.expect_download(timeout=15000) as download_info:
                self._safe_click(page, download_btn, "Download tugmasi")
            download = download_info.value
            log(f"[Chart] Download boshlandi: {download.suggested_filename}")

            import tempfile
            import os as _os

            tmp_path = _os.path.join(tempfile.gettempdir(), download.suggested_filename)
            download.save_as(tmp_path)
            with open(tmp_path, "rb") as f:
                img_bytes = f.read()
            try:
                _os.remove(tmp_path)
            except Exception:
                pass

            log(f"[Chart] Download fayli olindi: {len(img_bytes) // 1024} KB")
        except Exception as e:
            log(f"[Chart] Download event xato: {e}")

        # 7) Rasm haqiqiyligini tekshirish
        if img_bytes:
            valid_image = False
            try:
                from PIL import Image
                import io as _io

                img = Image.open(_io.BytesIO(img_bytes))
                log(f"[Chart] Download rasmi: {img.width}x{img.height}, format={img.format}")
                if img.width >= 500 and img.height >= 250:
                    valid_image = True
            except Exception as e:
                log(f"[Chart] Download rasmi tekshirilmadi: {e}")

            if valid_image:
                log(f"[Chart] Share->Download OK ({len(img_bytes) // 1024} KB)")
                try:
                    close_btn = page.locator(
                        'button:has-text("Close"), [class*="modal"] button[class*="close"], [aria-label="Close"]'
                    ).first
                    if close_btn.count() > 0:
                        close_btn.click(timeout=1500)
                except Exception:
                    pass
                return img_bytes

        raise Exception("Share -> Download orqali sifatli grafik olinmadi")

    def _resize_to_target_ratio(self, img_bytes, target_ratio=12 / 7):
        """
        Rasmni berilgan en:bo'y nisbatiga moslaydi. Finviz rasmi juda keng
        chiqadi — kenglikni markazdan kesib (crop), grafik mazmuni ramkani
        to'liq to'ldiradigan qilamiz.
        """
        from PIL import Image
        import io as _io

        img = Image.open(_io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        current_ratio = w / h

        if current_ratio > target_ratio:
            new_w = int(h * target_ratio)
            x_offset = (w - new_w) // 2
            img = img.crop((x_offset, 0, x_offset + new_w, h))
        elif current_ratio < target_ratio:
            new_h = int(w / target_ratio)
            y_offset = (h - new_h) // 2
            img = img.crop((0, y_offset, w, y_offset + new_h))

        out = _io.BytesIO()
        img.save(out, format="PNG")
        result = out.getvalue()
        log(f"[Chart] Qayta o'lchamlandi: {w}x{h} -> {img.size[0]}x{img.size[1]}")
        return result

    def _find_chart(self, page):
        container_selectors = [
            "#chart-container",
            "div[class*='chart-wrap']",
            "div[id^='chart']",
            "div[class*='chart']:has(canvas)",
        ]
        for selector in container_selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=2000)
                box = locator.bounding_box()
                if box and box["width"] > 400 and box["height"] > 250:
                    log(f"[Chart] Found container: {selector}")
                    return locator
            except Exception:
                pass

        selectors = [
            "canvas.second",
            "canvas",
            "div[id^='chart'] canvas",
            "div[class*='chart'] canvas",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=2000)
                log(f"[Chart] Found : {selector}")
                return locator
            except Exception:
                pass

        return None

    def _capture_chart(self, page):
        try:
            img = self._capture_via_share_download(page)
            if img:
                return img
        except Exception as e:
            log(f"[Chart] Share->Download muvaffaqiyatsiz: {e}")

        log("[Chart] Zaxira usul: screenshot")
        chart = self._find_chart(page)
        if chart:
            try:
                box = chart.bounding_box()
                if box:
                    log(f"[Chart] Size : {int(box['width'])}x{int(box['height'])}")
                    if box["width"] < 400 or box["height"] < 200:
                        log("[Chart] Element too small, page screenshot ga o'tamiz")
                        raise ValueError("Element too small")

                img = chart.screenshot(type="png")
                if _is_image_dark(img):
                    log("[Chart] ⚠️ Screenshot ham dark, page screenshot ga o'tamiz")
                    raise ValueError("Screenshot dark")

                log(f"[Chart] Chart screenshot OK ({len(img)//1024} KB)")
                return img
            except Exception as e:
                log(f"[Chart] Canvas screenshot failed : {e}")

        log("[Chart] Canvas topilmadi -> Page screenshot")
        try:
            img = page.screenshot(
                clip={"x": 0, "y": 140, "width": 1600, "height": 850},
                type="png",
            )
            log(f"[Chart] Page screenshot OK ({len(img)//1024} KB)")
            return img
        except Exception as e:
            log(f"[Chart] Page screenshot ham muvaffaqiyatsiz: {e}")
            return None


def _get_direct_finviz_chart(ticker):
    """
    Browser/Chromium crash bo'lsa Finviz chartni to'g'ridan-to'g'ri
    Finviz charts2 endpointidan oladi.

    ta=1 sababli fallback ham Finvizning texnik overlaylarini
    (SMA20/50/200 va trendline/TA chiziqlari) saqlab qoladi.
    Bu fallback browserga bog'liq emas.
    """
    ticker = (ticker or "").upper().strip()
    if not re.fullmatch(r"[A-Z.]{1,10}", ticker):
        log(f"[Direct Chart] Noto'g'ri ticker: {ticker}")
        return None

    # Legacy Finviz chart endpointdagi ta=1 texnik overlaylarni yoqadi:
    # SMA20/50/200 va Finviz trendline/technical-analysis chiziqlari.
    # sf=2 esa mavjud bo'lsa 2x yuqori aniqlikni so'raydi.
    params = {
        "t": ticker,
        "ty": "c",
        "ta": "1",
        "p": "d",
        "s": "l",
        "sf": "2",
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": f"https://finviz.com/quote.ashx?t={ticker}",
    }

    try:
        log(f"[Direct Chart] START: {ticker}")
        resp = requests.get(
            FINVIZ_DIRECT_CHART_URL,
            params=params,
            headers=headers,
            timeout=15,
        )

        if resp.status_code != 200:
            log(f"[Direct Chart] HTTP {resp.status_code}: {ticker}")
            return None

        content_type = (resp.headers.get("Content-Type") or "").lower()
        img_bytes = resp.content

        if not img_bytes or len(img_bytes) < 10_000:
            log(
                f"[Direct Chart] Rasm juda kichik/bo'sh: "
                f"{len(img_bytes)} bytes ({ticker})"
            )
            return None

        # HTML login/error sahifasi qaytib qolgan bo'lsa, uni rasm deb qabul qilmaymiz.
        if "text/html" in content_type or img_bytes[:20].lower().startswith(b"<!doctype"):
            log(f"[Direct Chart] HTML/error javob qaytdi: {ticker}")
            return None

        try:
            from PIL import Image

            img = Image.open(_io.BytesIO(img_bytes))
            img.verify()

            img = Image.open(_io.BytesIO(img_bytes))
            if img.width < 500 or img.height < 250:
                log(
                    f"[Direct Chart] Rasm o'lchami juda kichik: "
                    f"{img.width}x{img.height} ({ticker})"
                )
                return None

            # Finviz 2x (sf=2) chart odatda yuqori sifatli 648x360 atrofida
            # qaytadi; Telegramga yuborish uchun original PNGni saqlaymiz.

            log(
                f"[Direct Chart] OK: {ticker} | "
                f"{img.width}x{img.height} | {len(img_bytes) // 1024} KB"
            )
            return img_bytes

        except Exception as e:
            log(f"[Direct Chart] Rasm tekshiruvida xato ({ticker}): {e}")
            return None

    except Exception as e:
        log(f"[Direct Chart] Xato ({ticker}): {e}")
        return None


def get_chart_and_info(ticker):
    """
    Bitta Finviz sahifa ochilishidan HAM grafik, HAM matnli ma'lumotlarni oladi.
    Qaytaradi: (img_bytes, info_dict)
    """
    page = None
    try:
        downloader = ChartDownloader()
        page = downloader._open_page(ticker)
        info = downloader.parse_finviz_info(page)
        img = downloader._capture_chart(page)
        if img:
            print(f"[Chart] Finviz OK : {ticker}", flush=True)
            return img, info
        print("[Chart] Birinchi urinishda rasm olinmadi -> direct fallback", flush=True)
        direct_img = _get_direct_finviz_chart(ticker)
        if direct_img:
            print(f"[Chart] DIRECT FALLBACK OK : {ticker}", flush=True)
            return direct_img, info
        print("[Chart] Direct fallback ham ishlamadi -> qayta urinamiz", flush=True)
    except TimeoutError as e:
        print(f"[Chart] Timeout : {e}", flush=True)
    except Error as e:
        print(f"[Chart] Playwright Error : {e}", flush=True)
    except Exception as e:
        print(f"[Chart] Error : {e}", flush=True)
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass

    # Browser crash/timeout bo'lsa, 2-marta Chromium ochishdan oldin
    # browserga bog'liq bo'lmagan direct Finviz chartni sinab ko'ramiz.
    direct_img = _get_direct_finviz_chart(ticker)
    if direct_img:
        print(f"[Chart] DIRECT FALLBACK OK: {ticker}", flush=True)
        return direct_img, None

    try:
        if hasattr(browser_manager, "restart"):
            browser_manager.restart()
    except Exception as e:
        print(f"[Chart] Browser restart xato: {e}", flush=True)

    page = None
    try:
        print(f"[Chart] Qayta urinish : {ticker}", flush=True)
        downloader = ChartDownloader()
        page = downloader._open_page(ticker)
        info = downloader.parse_finviz_info(page)
        img = downloader._capture_chart(page)
        if img:
            print(f"[Chart] Qayta urinishda OK : {ticker}", flush=True)
        return img, info
    except Exception as e:
        print(f"[Chart] Qayta urinish ham muvaffaqiyatsiz : {e}", flush=True)
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass

    return None, None


def get_chart(ticker):
    page = None
    try:
        downloader = ChartDownloader()
        page = downloader._open_page(ticker)
        img = downloader._capture_chart(page)
        if img:
            print(f"[Chart] Finviz OK : {ticker}", flush=True)
            return img
        print("[Chart] Birinchi urinishda rasm olinmadi -> direct fallback", flush=True)
        direct_img = _get_direct_finviz_chart(ticker)
        if direct_img:
            print(f"[Chart] DIRECT FALLBACK OK : {ticker}", flush=True)
            return direct_img
        print("[Chart] Direct fallback ham ishlamadi -> qayta urinamiz", flush=True)
    except TimeoutError as e:
        print(f"[Chart] Timeout : {e}", flush=True)
    except Error as e:
        print(f"[Chart] Playwright Error : {e}", flush=True)
    except Exception as e:
        print(f"[Chart] Error : {e}", flush=True)
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass

    # Browser crash/timeout bo'lsa, 2-marta Chromium ochishdan oldin
    # direct Finviz chart fallbackni sinab ko'ramiz.
    direct_img = _get_direct_finviz_chart(ticker)
    if direct_img:
        print(f"[Chart] DIRECT FALLBACK OK: {ticker}", flush=True)
        return direct_img

    try:
        if hasattr(browser_manager, "restart"):
            browser_manager.restart()
    except Exception as e:
        print(f"[Chart] Browser restart xato: {e}", flush=True)

    page = None
    try:
        print(f"[Chart] Qayta urinish : {ticker}", flush=True)
        downloader = ChartDownloader()
        page = downloader._open_page(ticker)
        img = downloader._capture_chart(page)
        if img:
            print(f"[Chart] Qayta urinishda OK : {ticker}", flush=True)
        return img
    except Exception as e:
        print(f"[Chart] Qayta urinish ham muvaffaqiyatsiz : {e}", flush=True)
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# HARD TIMEOUT PROCESS WORKER
# ---------------------------------------------------------------------------
# Ikki qatlamli himoya:
#   1) TASHQI: alohida process (multiprocessing) + process.join(hard_timeout)
#      — agar worker javob bermasa, majburan terminate qilinadi.
#   2) Playwright chaqiruvlarining o'z timeoutlari ishlaydi.
#      Tashqi watchdog javob bo'lmasa butun worker + Chromium daraxtini
#      tugatadi.
#
# MUHIM: bot faylida to'g'ridan-to'g'ri get_chart_and_info(...) /
# get_chart(...) o'rniga quyidagi get_chart_and_info_safe(...) /
# get_chart_safe(...) chaqirilishi kerak.

import multiprocessing as mp

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

HARD_TIMEOUT = 45

def _kill_process_tree(pid, timeout=5):
    """
    MUHIM: process.terminate() faqat worker Python jarayonini o'chiradi,
    lekin uning ICHIDA Playwright ochgan Chromium — alohida "bola" jarayon.
    Faqat worker'ni o'chirish Chromium'ni "etim" (orphan) holatda qoldirib,
    xotirada ishlab qolaveradi. Bir necha marta takrorlangan hang'lardan
    keyin bu ko'plab Chromium nusxalarini to'plab, Render konteynerini
    OOM (xotira yetishmasligi) sababli butunlay qayta ishga tushirishga
    majbur qiladi — aynan kuzatilgan holat. Shuning uchun butun jarayon
    daraxtini (Chromium bilan birga) o'chiramiz.
    """
    if not _HAS_PSUTIL:
        # psutil yo'q bo'lsa, hech bo'lmasa asosiy jarayonni o'chiramiz
        try:
            os_kill_fallback = __import__("os").kill
            os_kill_fallback(pid, 15)
        except Exception:
            pass
        return

    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    children = parent.children(recursive=True)
    for child in children:
        try:
            child.terminate()
        except Exception:
            pass

    _, alive = psutil.wait_procs(children, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass

    try:
        parent.terminate()
        parent.wait(timeout)
    except Exception:
        try:
            parent.kill()
        except Exception:
            pass


def _chart_worker(ticker, mode, queue):
    """Run Playwright Sync API in an isolated process.

    Do not use signal.alarm here. Interrupting Playwright while it is
    dispatching a route callback can produce RouteHandler.handle warnings,
    broken driver connections and EPIPE errors. The parent process provides
    the hard timeout and kills the complete Chromium process tree.
    """
    try:
        print(f"[Chart Worker] START: {ticker} | mode={mode}", flush=True)
        result = get_chart_and_info(ticker) if mode == "info" else get_chart(ticker)
        queue.put({"ok": True, "result": result})
        print(f"[Chart Worker] DONE: {ticker}", flush=True)
    except Exception as e:
        print(f"[Chart Worker] ERROR: {ticker}: {e}", flush=True)
        try:
            queue.put({"ok": False, "error": str(e)})
        except Exception:
            pass

def _run_chart_process(ticker, mode, hard_timeout):
    """
    MUHIM: navbat (Queue) katta ma'lumot (masalan rasm bytes) bilan
    to'ldirilganda, bola jarayon uni fon oqimi orqali OS pipe'iga yozadi.
    Agar asosiy jarayon avval faqat process.join()ni kutib, navbatni hali
    o'qimagan bo'lsa, pipe to'lib qolishi mumkin — bola jarayon chiqib
    keta olmay "osilib qolganday" ko'rinadi, garchi u ishini ALLAQACHON
    tugatgan bo'lsa ham! Shuning uchun avval navbatdan o'qiymiz (bu bola
    jarayonni pipe orqali "bo'shatadi"), keyingina join() bilan jarayonni
    yig'ishtiramiz.
    """
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(target=_chart_worker, args=(ticker, mode, queue))
    process.start()

    data = None
    try:
        data = queue.get(timeout=hard_timeout)
    except Exception:
        data = None  # vaqt tugadi yoki navbat bo'sh — hech narsa kelmadi

    # Natija kelgan-kelmaganidan qat'iy nazar, jarayonni tugatish uchun
    # qisqa vaqt beramiz (natija kelgan bo'lsa, bu deyarli darhol tugaydi)
    process.join(5)

    if process.is_alive():
        print(f"[Chart] TASHQI HARD TIMEOUT ({hard_timeout}s) -> {ticker}", flush=True)
        _kill_process_tree(process.pid)
        return None

    if data is None:
        print(f"[Chart] Worker natija qaytarmadi: {ticker}", flush=True)
        return None

    if not data.get("ok"):
        print(f"[Chart] Worker error: {data.get('error')}", flush=True)
        return None

    return data.get("result")


def get_chart_and_info_safe(ticker, hard_timeout=HARD_TIMEOUT):
    try:
        result = _run_chart_process(ticker, "info", hard_timeout)
        if result:
            print(f"[Chart] SAFE INFO OK: {ticker}", flush=True)
        return result if result else (None, None)
    except Exception as e:
        print(f"[Chart] get_chart_and_info_safe xato: {e}", flush=True)
        return None, None


def get_chart_safe(ticker, hard_timeout=HARD_TIMEOUT):
    try:
        result = _run_chart_process(ticker, "chart", hard_timeout)
        if result:
            print(f"[Chart] SAFE CHART OK: {ticker}", flush=True)
        return result
    except Exception as e:
        print(f"[Chart] get_chart_safe xato: {e}", flush=True)
        return None
