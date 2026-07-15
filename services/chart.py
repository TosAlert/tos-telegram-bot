import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from playwright.sync_api import Error, TimeoutError
from services.browser import browser_manager

FINVIZ_URL = "https://finviz.com/quote.ashx?t={ticker}&p=d&r=m6"

BLOCKED_DOMAINS = [
    "doubleclick.net", "googlesyndication", "google-analytics",
    "googletagmanager", "adsystem", "facebook.net", "amazon-adsystem",
    "criteo", "taboola", "outbrain", "adnxs.com", "adservice.google",
]

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
        print(f"[Chart] Rasm fon yorqinligi: {avg:.0f} (threshold={threshold})")
        return avg < threshold
    except Exception as e:
        print(f"[Chart] Dark tekshirishda xato: {e}")
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
            print(f"[Chart] Route bloklashda xato: {e}")
            
    def _safe_click(self, page, locator, label):
        try:
            locator.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            print(f"[Chart] {label}: scroll_into_view timeout, davom etamiz")

        try:
            locator.click(timeout=4000, force=True)
            print(f"[Chart] {label} bosildi (click)")
            return
        except Exception as e:
            print(f"[Chart] {label} click xato: {e}")

        try:
            box = locator.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                page.mouse.move(x, y)
                page.mouse.click(x, y)
                print(f"[Chart] {label} bosildi (mouse coord)")
                return
        except Exception as e:
            print(f"[Chart] {label} mouse click xato: {e}")

        try:
            locator.evaluate("el => el.click()")
            print(f"[Chart] {label} bosildi (JS click)")
        except Exception as e:
            print(f"[Chart] {label} JS click ham xato: {e}")
            raise

    def _open_page(self, ticker):
        page = browser_manager.new_page()
        self._block_ads(page)

        print(f"[Chart] Page id: {id(page)}")
        print(f"[Chart] Opening {ticker}")

        url = FINVIZ_URL.format(ticker=ticker.upper())
        print(f"[Chart] URL: {url}")

        try:
            page.context.add_cookies([
                {"name": "theme", "value": "light", "domain": ".finviz.com", "path": "/"},
                {"name": "darkMode", "value": "false", "domain": ".finviz.com", "path": "/"},
                {"name": "chartTheme", "value": "light", "domain": ".finviz.com", "path": "/"},
                {"name": "charts", "value": "light", "domain": ".finviz.com", "path": "/"},
            ])
        except Exception as e:
            print(f"[Chart] Cookie sozlashda xato: {e}")

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except TimeoutError:
            print("[Chart] First timeout -> retry")
            page.goto(url, wait_until="commit", timeout=30000)

        page.set_viewport_size({
            "width": 700,
            "height": 1600,
        })

        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)
        
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
        print(f"[Chart] Title : {title}")

        if ticker.upper() not in title.upper():
            raise Exception(f"Unexpected Finviz page : {title}")

        return page

    def _capture_via_share_download(self, page):

        try:
            page.evaluate("""
                () => {
                    document.querySelectorAll(
                        '[class*="ic_dimm"], [class*="ic_under"], [class*="ic_fade"], [class*="overlay"]'
                    ).forEach(el => {
                        el.style.pointerEvents = 'none';
                        el.style.display = 'none';
                    });
                }
            """)
        except Exception:
            pass

        share_btn = page.locator(
            'button:has-text("Share"), a:has-text("Share"), [class*="share"]:has-text("Share")'
        ).first

        share_btn.wait_for(state="visible", timeout=8000)
        self._safe_click(page, share_btn, "Share tugmasi")

        self._safe_click(page, share_btn, "Share tugmasi")

        # Modal animatsiyasi tugashini kutamiz
        page.wait_for_timeout(800)

        # Download tugmasi chiqishini kutamiz
        download_btn = page.locator("text=Download").first
        download_btn.wait_for(
            state="visible",
            timeout=10000
        )

        print("[Chart] Download tugmasi ko'rindi")

        try:
            page.evaluate("""
                () => {
                    document.querySelectorAll(
                        '[class*="ic_dimm"], [class*="ic_under"], [class*="ic_fade"]'
                    ).forEach(el => {
                        el.style.pointerEvents = 'none';
                    });
                }
            """)
        except Exception:
            pass

        download_selectors = [
            'button:has-text("Download")',
            'a:has-text("Download")',
            '[class*="download"]',
            'button[title*="Download" i]',
            'a[download]',
        ]

        download_btn = None
        for sel in download_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.wait_for(state="visible", timeout=3000)
                    download_btn = loc
                    print(f"[Chart] Download tugma topildi: {sel}")
                    break
            except Exception:
                continue

        if download_btn is None:
            raise Exception("Download tugmasi topilmadi")

        # Chart so'rovining URL'ini ushlab olamiz (zaxira uchun)
        captured_url = {"value": None}

        def _on_request(request):
            u = request.url
            low = u.lower()
            if (("chart.ashx" in low)
                    or ("finviz.com/chart" in low)
                    or ("charts" in low and "finviz" in low)):
                captured_url["value"] = u

        def _on_response(response):
            u = response.url
            low = u.lower()
            ct = (response.headers or {}).get("content-type", "")
            if "image" in ct.lower() and "finviz" in low:
                captured_url["value"] = u

        page.on("request", _on_request)
        page.on("response", _on_response)

        img_bytes = None
        try:
            with page.expect_download(timeout=15000) as download_info:
                self._safe_click(page, download_btn, "Download tugmasi")

            download = d.value

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
        finally:
            try:
                page.remove_listener("request", _on_request)
            except Exception:
                pass
            try:
                page.remove_listener("response", _on_response)
            except Exception:
                pass

        # Zaxira: URL'da tema dark bo'lsa, light ga o'zgartirib qayta yuklaymiz
        src_url = captured_url["value"]
        if src_url and "theme=dark" in src_url.lower():
            light_url = _force_light_url(src_url)
            if light_url and light_url != src_url:
                print(f"[Chart] Dark chart URL topildi, light ga o'zgartirildi:\n  {light_url}")
                try:
                    resp = page.request.get(light_url, timeout=15000)
                    if resp.ok:
                        body = resp.body()
                        if body and len(body) > 1000:
                            img_bytes = body
                            print("[Chart] Light versiya URL orqali yuklandi")
                except Exception as e:
                    print(f"[Chart] Light URL yuklashda xato: {e}")

        if not img_bytes:
            raise Exception("Download rasmi olinmadi")

        print(f"[Chart] Share->Download OK ({len(img_bytes)//1024} KB)")

        try:
            close_btn = page.locator(
                'button:has-text("Close"), [class*="modal"] button[class*="close"]'
            ).first
            if close_btn.count() > 0:
                close_btn.click(timeout=1000)
        except Exception:
            pass

        return img_bytes

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
                    print(f"[Chart] Found container: {selector}")
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
                print(f"[Chart] Found : {selector}")
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
            print(f"[Chart] Share->Download muvaffaqiyatsiz: {e}")

        print("[Chart] Zaxira usul: screenshot")

        chart = self._find_chart(page)

        if chart:
            try:
                box = chart.bounding_box()
                if box:
                    print(f"[Chart] Size : {int(box['width'])}x{int(box['height'])}")
                    if box["width"] < 400 or box["height"] < 200:
                        print("[Chart] Element too small, page screenshot ga o'tamiz")
                        raise ValueError("Element too small")

                img = chart.screenshot(type="png")
                if _is_image_dark(img):
                    print("[Chart] ⚠️ Screenshot ham dark, page screenshot ga o'tamiz")
                    raise ValueError("Screenshot dark")
                print(f"[Chart] Chart screenshot OK ({len(img)//1024} KB)")
                return img
            except Exception as e:
                print(f"[Chart] Canvas screenshot failed : {e}")

        print("[Chart] Canvas topilmadi -> Page screenshot")
        try:
            img = page.screenshot(
                clip={"x": 0, "y": 140, "width": 1600, "height": 850},
                type="png",
            )
            print(f"[Chart] Page screenshot OK ({len(img)//1024} KB)")
            return img
        except Exception as e:
            print(f"[Chart] Page screenshot ham muvaffaqiyatsiz: {e}")
            return None


def get_chart(ticker):
    page = None
    try:
        downloader = ChartDownloader()
        page = downloader._open_page(ticker)
        img = downloader._capture_chart(page)
        if img:
            print(f"[Chart] Finviz OK : {ticker}")
        return img

    except TimeoutError as e:
        print(f"[Chart] Timeout : {e}")
    except Error as e:
        print(f"[Chart] Playwright Error : {e}")
    except Exception as e:
        print(f"[Chart] Error : {e}")
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass

    page = None
    try:
        print(f"[Chart] Qayta urinish : {ticker}")
        downloader = ChartDownloader()
        page = downloader._open_page(ticker)
        img = downloader._capture_chart(page)
        if img:
            print(f"[Chart] Qayta urinishda OK : {ticker}")
        return img
    except Exception as e:
        print(f"[Chart] Qayta urinish ham muvaffaqiyatsiz : {e}")
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass

    return None
