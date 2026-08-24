import re
import signal
import time
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from playwright.sync_api import Error, TimeoutError

from services.browser import browser_manager

FINVIZ_URL = "https://finviz.com/quote.ashx?t={ticker}&p=d&r=m6"

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
        def _route_handler(route):
            req = route.request
            try:
                if any(b in req.url for b in BLOCKED_DOMAINS) or req.resource_type == "media":
                    route.abort()
                else:
                    route.continue_()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass

        try:
            page.route("**/*", _route_handler)
        except Exception as e:
            log(f"[Chart] Route bloklashda xato: {e}")

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

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except TimeoutError:
            log("[Chart] First timeout -> retry")
            page.goto(url, wait_until="commit", timeout=30000)

        page.set_viewport_size({"width": 1100, "height": 850})
        page.wait_for_timeout(1500)

        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        page.wait_for_timeout(1000)

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

        page.locator("canvas").first.wait_for(state="visible", timeout=15000)
        page.wait_for_timeout(1200)

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
                        spinner.wait_for(state="hidden", timeout=15000)
                        log("[Chart] Spinner tugadi")
                    except Exception:
                        log("[Chart] Spinner timeout -> davom etamiz")
                    break
            except Exception:
                continue

        # 5) Download tugmasini kutish (maks. 20s)
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
        while time.time() - start_time < 20:
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
            raise Exception("Share modal ochildi, lekin Download tugmasi 20 soniyada topilmadi")

        # 6) Download eventni kutish
        log("[Chart] Download bosilmoqda, fayl kutilmoqda...")
        img_bytes = None
        try:
            with page.expect_download(timeout=30000) as download_info:
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
        print("[Chart] Birinchi urinishda rasm olinmadi -> qayta urinamiz", flush=True)
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
        print("[Chart] Birinchi urinishda rasm olinmadi -> qayta urinamiz", flush=True)
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
#   2) ICHKI: worker process ichida signal.alarm(...) — agar biror
#      Playwright chaqiruvi o'zining timeout'ini ham hurmat qilmay abadiy
#      osilib qolsa (kuzatilgan holat), worker ICHIDAN o'zi vaqtida
#      TimeoutError chiqarib to'xtaydi, tashqi watchdog kutmasdan.
#
# MUHIM: bot faylida to'g'ridan-to'g'ri get_chart_and_info(...) /
# get_chart(...) o'rniga quyidagi get_chart_and_info_safe(...) /
# get_chart_safe(...) chaqirilishi kerak.

import multiprocessing as mp

HARD_TIMEOUT = 90
INNER_ALARM_TIMEOUT = 75  # tashqi 90s dan kamroq — ichki alarm birinchi ishga tushsin


class _InnerHardTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _InnerHardTimeout("Ichki signal.alarm timeout")


def _chart_worker(ticker, mode, queue):
    """
    Playwright Sync API alohida process ichida ishlaydi (asyncio bilan
    to'qnashmasligi uchun). Ichida qo'shimcha signal.alarm bilan
    himoyalangan — Playwright'ning o'zi hech qachon abadiy osilib
    qolmasligini kafolatlaydi.
    """
    try:
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(INNER_ALARM_TIMEOUT)
    except Exception as e:
        # signal faqat asosiy thread'da va Unix'da ishlaydi — Render/Linux
        # uchun bu muammo bo'lmasligi kerak, lekin ehtiyot uchun try/except
        print(f"[Chart Worker] signal.alarm sozlanmadi: {e}", flush=True)

    try:
        print(f"[Chart Worker] START: {ticker} | mode={mode}", flush=True)

        if mode == "info":
            result = get_chart_and_info(ticker)
        else:
            result = get_chart(ticker)

        queue.put({"ok": True, "result": result})
        print(f"[Chart Worker] DONE: {ticker}", flush=True)

    except _InnerHardTimeout:
        print(f"[Chart Worker] ICHKI HARD TIMEOUT ({INNER_ALARM_TIMEOUT}s): {ticker}", flush=True)
        queue.put({"ok": False, "error": "inner_hard_timeout"})

    except Exception as e:
        print(f"[Chart Worker] ERROR: {ticker}: {e}", flush=True)
        queue.put({"ok": False, "error": str(e)})

    finally:
        try:
            signal.alarm(0)
        except Exception:
            pass


def _run_chart_process(ticker, mode, hard_timeout):
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(target=_chart_worker, args=(ticker, mode, queue))
    process.start()
    process.join(hard_timeout)

    if process.is_alive():
        print(f"[Chart] TASHQI HARD TIMEOUT ({hard_timeout}s) -> {ticker}", flush=True)
        try:
            process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join(3)
        except Exception:
            pass
        return None

    if queue.empty():
        print(f"[Chart] Worker natija qaytarmadi: {ticker}", flush=True)
        return None

    data = queue.get()
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
