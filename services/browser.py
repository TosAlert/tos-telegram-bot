import os
import threading

from playwright.sync_api import sync_playwright

FINVIZ_EMAIL = os.getenv("FINVIZ_EMAIL")
FINVIZ_PASSWORD = os.getenv("FINVIZ_PASSWORD")
FINVIZ_STATE_FILE = os.getenv("FINVIZ_STATE_FILE", "/tmp/finviz_state.json")


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

        print("[Browser] Render/Linux mode")

        self.playwright = sync_playwright().start()

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

        context_kwargs = {
            "viewport": {"width": 1600, "height": 1200},
            "accept_downloads": True,
            "locale": "en-US",
            "timezone_id": "UTC",
            "color_scheme": "light",
            "device_scale_factor": 1,
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        }

        # Har bir chart worker yangi processda ishga tushadi.
        # Finviz sessiyasini qayta ishlatish login timeoutlarini keskin kamaytiradi.
        if os.path.exists(FINVIZ_STATE_FILE):
            try:
                context_kwargs["storage_state"] = FINVIZ_STATE_FILE
                print("[Finviz] Saqlangan sessiya yuklanmoqda...")
            except Exception as e:
                print(f"[Finviz] Sessiya faylini sozlashda xato: {e}")

        try:
            self.context = self.browser.new_context(**context_kwargs)
        except Exception as e:
            # Eski/buzilgan storage_state bo'lsa, toza context bilan davom etamiz.
            print(f"[Finviz] Saqlangan sessiya yaroqsiz -> yangi sessiya: {e}")
            context_kwargs.pop("storage_state", None)
            self.context = self.browser.new_context(**context_kwargs)

        self.context.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9"
        })

        self.context.set_default_timeout(15000)
        self.context.set_default_navigation_timeout(20000)

        # Birinchi worker login qiladi. Keyingi workerlar /tmp dagi sessiyani ishlatadi.
        if not os.path.exists(FINVIZ_STATE_FILE):
            self.login_finviz()
        else:
            print("[Finviz] Saqlangan sessiya ishlatiladi ✅")

        print("[Browser] Chromium ishga tushdi ✅")

    def login_finviz(self, force=False):
        if not FINVIZ_EMAIL or not FINVIZ_PASSWORD:
            print("[Finviz] Login ma'lumotlari topilmadi")
            return False

        page = self.context.new_page()

        try:
            print("[Finviz] Login boshlanmoqda...")

            page.goto(
                "https://finviz.com/login-email?remember=true",
                wait_until="domcontentloaded",
                timeout=20000,
            )

            page.wait_for_timeout(1000)

            if "login" not in page.url.lower() and not force:
                print("[Finviz] Allaqachon login qilingan ✅")
                try:
                    self.context.storage_state(path=FINVIZ_STATE_FILE)
                    print("[Finviz] Sessiya saqlandi ✅")
                except Exception as e:
                    print(f"[Finviz] Sessiyani saqlash xatosi: {e}")
                return True

            email = page.locator('input[autocomplete="username"]').first
            password = page.locator('input[name="password"]').first
            submit = page.locator('button[type="submit"]').first

            email.wait_for(state="visible", timeout=7000)
            password.wait_for(state="visible", timeout=7000)
            submit.wait_for(state="visible", timeout=5000)

            email.fill(FINVIZ_EMAIL)
            password.fill(FINVIZ_PASSWORD)
            submit.click(timeout=5000)

            # Login javobini ko'p kutmaymiz. Sessiya cookie olingan bo'lsa yetarli.
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            if "login" in page.url.lower():
                print("[Finviz] Login muvaffaqiyatsiz ❌")
                return False

            print("[Finviz] Login muvaffaqiyatli ✅")
            print(f"[Finviz] URL: {page.url}")

            try:
                self.context.storage_state(path=FINVIZ_STATE_FILE)
                print("[Finviz] Sessiya saqlandi ✅")
            except Exception as e:
                print(f"[Finviz] Sessiyani saqlash xatosi: {e}")

            return True

        except Exception as e:
            print(f"[Finviz] Login xatosi: {e}")
            return False

        finally:
            try:
                page.close()
            except Exception:
                pass

    def new_page(self):
        if self.context is None:
            self.start()

        page = self.context.new_page()

        page.set_viewport_size({
            "width": 1600,
            "height": 1200
        })

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

    def restart(self):
        print("[Browser] Restart boshlandi...")

        try:
            self.close()
        except Exception as e:
            print(f"[Browser] Close xato: {e}")

        try:
            self.start()
            print("[Browser] Restart muvaffaqiyatli ✅")
        except Exception as e:
            print(f"[Browser] Restart xato: {e}")


browser_manager = BrowserManager()
