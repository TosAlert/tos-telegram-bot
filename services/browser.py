import os
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

FINVIZ_EMAIL = os.getenv("FINVIZ_EMAIL")
FINVIZ_PASSWORD = os.getenv("FINVIZ_PASSWORD")


class BrowserManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance.playwright = None
                cls._instance.browser = None
                cls._instance.context = None

        return cls._instance

    def start(self):
        if self.context:
            return

        self.playwright = sync_playwright().start()

        railway = os.getenv("RAILWAY_ENVIRONMENT") is not None

        if railway:
            print("[Browser] Railway mode")
            self.browser = self.playwright.chromium.launch(
                headless=True,
                chromium_sandbox=False,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-extensions",
                    "--mute-audio",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )

            self.context = self.browser.new_context(
                viewport={"width": 700, "height": 1600},
                accept_downloads=True,
                locale="en-US",
                timezone_id="UTC",
                color_scheme="light",
                device_scale_factor=1,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/138.0.7204.169 Safari/537.36"
                ),
            )

            self.context.set_extra_http_headers({
                "Accept-Language": "en-US,en;q=0.9"
            })

            self.login_finviz()

        else:
            print("[Browser] Windows mode")
            profile = str(Path.home() / "playwright_profile")

            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=profile,
                channel="chrome",
                headless=False,
                no_viewport=True,
                accept_downloads=True,
                color_scheme="light",
                args=[
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self.login_finviz()

        self.context.set_default_timeout(60000)
        self.context.set_default_navigation_timeout(60000)

    def login_finviz(self):
        if not FINVIZ_EMAIL or not FINVIZ_PASSWORD:
            print("[Finviz] Login ma'lumotlari topilmadi")
            return

        page = self.context.new_page()

        try:
            print("[Finviz] Login boshlanmoqda...")

            page.goto(
                "https://finviz.com/login-email?remember=true",
                wait_until="domcontentloaded"
            )

            page.wait_for_timeout(2000)

            page.locator('input[type="email"]').fill(FINVIZ_EMAIL)
            page.locator('input[type="password"]').fill(FINVIZ_PASSWORD)
            page.locator('button[type="submit"]').click()

            page.wait_for_load_state("networkidle")

            print(f"[Finviz] URL: {page.url}")

            if "login" in page.url.lower():
                print("[Finviz] Login muvaffaqiyatsiz")
            else:
                print("[Finviz] Login muvaffaqiyatli ✅")

        except Exception as e:
            print(f"[Finviz] Login xatosi: {e}")

        finally:
            page.close()

    def new_page(self):
        if self.context is None:
            self.start()

        page = self.context.new_page()
        page.set_viewport_size({"width": 1600, "height": 1200})

        page.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9"
        })

        return page

    def close(self):
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass

        self.context = None
        self.browser = None
        self.playwright = None


browser_manager = BrowserManager()
