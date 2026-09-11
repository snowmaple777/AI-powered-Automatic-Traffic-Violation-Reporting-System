import time
from typing import Any
from plugins.base_plugin import BasePlugin
from playwright.sync_api import Response

class HumanVerificationHelperPlugin(BasePlugin):
    """
    人機協作手動驗證插件。
    當網頁需要輸入驗證碼或進行信箱驗證時，暫停自動化流程，並等待使用者手動完成後再繼續。
    """
    def __init__(self, mode: str = "auto", timeout_sec: int = 180):
        """
        Args:
            mode: 偵測模式。
                - 'api': 監聽特定的驗證成功 API 回應。
                - 'dom': 監聽 DOM 節點狀態改變。
                - 'signal': 在 CLI 提示使用者確認後按 Enter 鍵繼續。
                - 'auto': 依據 driver.config 裡的配置自動判斷；若無特別配置，則降級為智慧自動模式。
            timeout_sec: 等待手動驗證之超時時間 (秒)。
        """
        self.mode = mode
        self.timeout_sec = timeout_sec
        self._verified = False

    def on_verification(self, driver: Any) -> bool:
        print("\n" + "="*80)
        print(" 【人機協作提示】偵測到網頁需要進行身份驗證 (驗證碼 或 信箱 OTP 驗證)。")
        print(" 請直接在瀏覽器視窗中進行手動操作 (填寫驗證碼或完成 Gmail 信箱認證)。")
        print(" 系統將在偵測到驗證通過後，自動繼續填寫剩下的表單...")
        print("="*80 + "\n")

        self._verified = False
        start_time = time.time()
        
        # 1. API 攔截偵測模式
        if self.mode == "api" or (self.mode == "auto" and "verification_api" in driver.config):
            api_config = driver.config.get("verification_api", {})
            success_endpoint = api_config.get("endpoint")
            
            if not success_endpoint:
                print("[系統警告] 已配置 API 監聽模式但設定檔中缺少 [verification_api][endpoint]，降級為智慧自動模式。")
                return self._run_smart_auto_mode(driver)

            print(f"[系統監聽] 正在攔截驗證端點: {success_endpoint} ...")
            
            def response_handler(response: Response):
                if success_endpoint in response.url and 200 <= response.status < 300:
                    try:
                        res_json = response.json()
                        success_key = api_config.get("success_key", "success")
                        success_value = api_config.get("success_value", True)
                        
                        if res_json.get(success_key) == success_value:
                            print(f"[系統偵測] 偵測到驗證 API 成功回傳！({success_key}={success_value})。")
                            self._verified = True
                        else:
                            print(f"[系統偵測] 偵測到驗證請求但未成功。API 回傳: {res_json}")
                    except Exception:
                        print("[系統偵測] 偵測到驗證請求成功傳送！")
                        self._verified = True

            driver.page.on("response", response_handler)
            try:
                while not self._verified:
                    if time.time() - start_time > self.timeout_sec:
                        raise TimeoutError("等待手動驗證超時。")
                    driver.page.wait_for_timeout(500)
            finally:
                driver.page.remove_listener("response", response_handler)
            return True

        # 2. DOM 狀態監聽模式
        elif self.mode == "dom" or (self.mode == "auto" and "verification_dom" in driver.config):
            dom_config = driver.config.get("verification_dom", {})
            success_selector = dom_config.get("success_indicator")
            
            if not success_selector:
                print("[系統警告] 已配置 DOM 監聽模式但設定檔中缺少 [verification_dom][success_indicator]，降級為智慧自動模式。")
                return self._run_smart_auto_mode(driver)

            print(f"[系統監聽] 正在監視 DOM 節點: {success_selector} 是否出現...")
            try:
                driver.page.wait_for_selector(success_selector, state="visible", timeout=self.timeout_sec * 1000)
                print("[系統偵測] 偵測到驗證成功標記元素！繼續執行表單填寫。")
                return True
            except Exception:
                raise TimeoutError("等待手動驗證超時，找不到成功的 DOM 標記。")

        # 3. 預設智慧自動模式
        elif self.mode == "auto":
            return self._run_smart_auto_mode(driver)

        # 4. 手動信號模式 (CLI 按下 Enter 繼續)
        else:
            return self._run_signal_mode()

    def _run_smart_auto_mode(self, driver: Any) -> bool:
        print("[系統監聽] 智慧自動偵測模式已啟動，請直接在網頁操作...")
        start_time = time.time()
        initial_url = driver.page.url
        
        while not self._verified:
            if time.time() - start_time > self.timeout_sec:
                raise TimeoutError("等待手動驗證超時。")
                
            # A. 偵測頁面網址改變 (跨網頁登入跳轉)
            current_url = driver.page.url
            if current_url != initial_url:
                if "application-data-form" in current_url or "draft-selection" in current_url or "Create" in current_url or "traffic_write" in current_url or "D0102" in current_url:
                    print(f"[系統偵測] 網頁已順利跳轉至: {current_url}，自動恢復表單填寫。")
                    self._verified = True
                    break
            
            # B. 針對驗證碼輸入框 (如 6 碼) 的智慧自動提交
            try:
                otp_input = driver.page.locator("#otpCode")
                if otp_input.is_visible():
                    otp_val = otp_input.evaluate("el => el.value")
                    if len(otp_val) == 6:
                        confirm_btn = driver.page.locator("button:has-text('確認驗證碼')").first
                        if confirm_btn.is_visible() and not confirm_btn.evaluate("el => el.disabled"):
                            print("[系統偵測] 偵測到驗證碼輸入滿 6 碼，自動為您點擊「確認驗證碼」！")
                            confirm_btn.click()
                            driver.page.wait_for_timeout(1000)
            except Exception:
                pass
 
            # C. 偵測主要表單欄位是否出現 (表示已進入填表區)
            try:
                form_selectors = [
                    "#FromName", "#Name", "#IdentityNumber", "#Pid", 
                    "input[placeholder*='身分證']", "#checkCarNum", 
                    "#agreement_2078", "#violationdatetime",
                    "#cardate", "#CarNum", "#carTime",
                    "#sRuldec", "#sViladd"
                ]
                for selector in form_selectors:
                    if driver.page.locator(selector).is_visible():
                        print(f"[系統偵測] 偵測到表單欄位 {selector} 已呈現，自動繼續填表。")
                        self._verified = True
                        break
            except Exception:
                pass

            driver.page.wait_for_timeout(500)
            
        return True

    def _run_signal_mode(self) -> bool:
        print("[系統提示] 請在網頁上完成驗證動作。")
        try:
            input(">>> 確認您已手動驗證完畢後，請按 [Enter] 鍵以繼續填寫表單：")
        except EOFError:
            print("[系統警告] 無法讀取命令列輸入，將暫停 10 秒後自動嘗試繼續。")
            time.sleep(10)
        print("[系統] 收到繼續指令，恢復表單自動填寫。")
        return True
