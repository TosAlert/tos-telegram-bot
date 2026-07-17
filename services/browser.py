    def login_finviz(self):
        if not FINVIZ_EMAIL or not FINVIZ_PASSWORD:
            print("[Finviz] Login ma'lumotlari topilmadi")
            return

        page = self.context.new_page()

        try:
            print("[Finviz] Login boshlanmoqda...")

            page.goto(
                "https://finviz.com/login-email?remember=true",
                wait_until="domcontentloaded",
                timeout=60000,
            )

            page.wait_for_timeout(3000)

            # Agar allaqachon login bo'lgan bo'lsa
            if "login" not in page.url.lower():
                print("[Finviz] Allaqachon login qilingan ✅")
                return

            email = page.locator('input[autocomplete="username"]')
            password = page.locator('input[name="password"]')
            submit = page.locator('button[type="submit"]')

            email.wait_for(state="visible", timeout=10000)
            password.wait_for(state="visible", timeout=10000)

            email.fill(FINVIZ_EMAIL)
            password.fill(FINVIZ_PASSWORD)

            submit.click()

            page.wait_for_timeout(4000)

            if "login" in page.url.lower():
                print("[Finviz] Login muvaffaqiyatsiz ❌")
            else:
                print("[Finviz] Login muvaffaqiyatli ✅")
                print(f"[Finviz] URL: {page.url}")

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
