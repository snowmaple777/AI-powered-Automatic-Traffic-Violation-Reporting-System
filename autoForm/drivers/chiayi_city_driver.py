import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class ChiayiCityDriver(BaseDriver):
    """
    嘉義市交通違規檢舉填表策略 (Strategy Pattern)。
    對應網址: https://trn.ccpb.gov.tw/user-login/2/6
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.violation_district_name = "西區" # 預設行政區

    def navigate_to_start(self) -> None:
        print(f"[Chiayi City] 正在導覽至起始驗證頁面: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        
        # 註冊彈窗自動確認處理器，確保流程不被彈窗阻擋
        self.page.on("dialog", lambda dialog: dialog.accept())

    def handle_pre_actions(self) -> None:
        # 嘉義市需要在第一頁先完成 Email 驗證 (Pre-verification OTP)
        email = self.input_data.get("reporter_email", "")
        print(f"[Chiayi City] 填寫檢舉人信箱並發送驗證碼: {email}")
        
        try:
            # 等待 2.5 秒以確保 React/Vue 等前端框架已完全掛載與初始化，避免填入的值被框架初始化覆蓋清空
            self.page.wait_for_timeout(2500)
            
            # 雙重保險：1. 模擬實體鍵盤敲擊觸發框架 binding；2. 利用 JS 移除 disabled 屬性
            email_loc = self.page.locator(self.config["selectors"]["login_email"])
            email_loc.wait_for(state="visible", timeout=10000)
            email_loc.focus()
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Backspace")
            self.page.keyboard.type(email, delay=50)
            email_loc.blur()
            
            # 強制利用 JS 移除「發送驗證碼」按鈕的 disabled 屬性，確保能直接點擊
            self.page.locator(self.config["selectors"]["login_send_otp"]).evaluate("el => el.removeAttribute('disabled')")
            self.page.wait_for_timeout(500)
            
            print("[Chiayi City] 點擊「發送驗證碼」...")
            self.page.click(self.config["selectors"]["login_send_otp"])
        except Exception as e:
            print(f"[Chiayi City 提示] 自動發送驗證碼遭遇異常 (可能需要您手動點擊): {e}")
        
        # 等待 6 位數 OTP 輸入框出現在畫面上 (進行容錯捕獲，避免程序崩潰)
        try:
            self.page.wait_for_selector(self.config["selectors"]["login_otp_input"], timeout=6000)
        except Exception:
            print("\n[Chiayi City 提示] 自動等待驗證碼輸入框超時。")
            print("這通常是因為伺服器的發送頻率限制（例如：Email 驗證碼每 60 秒只能發送一次）。")
            print("請直接在瀏覽器視窗中手動點擊「發送驗證碼」，並在收信後輸入 6 位數驗證碼。")
            print("完成後，請回到終端機按 [Enter] 鍵，系統將為您自動填寫後續表單欄位。\n")
        
        # 呼叫人機協作插件暫停流程，讓使用者到信箱收取驗證碼並填入
        print("[Chiayi City] 觸發信箱 OTP 手動驗證 Hook...")
        self.trigger_hook("on_verification")
        
        # 使用者按下 Enter 後，如果網頁還沒有自動跳轉，便程式點擊確認驗證碼
        if self.page.locator(self.config["selectors"]["login_verify_otp"]).is_visible():
            try:
                print("[Chiayi City] 點擊「確認驗證碼」進入主要填表表單...")
                self.page.click(self.config["selectors"]["login_verify_otp"])
            except Exception:
                pass
                
        # 等待跳轉進入主要填表頁面 (application-data-form) 或草稿選擇頁面 (draft-selection)
        try:
            self.page.wait_for_url("**/application-data-form**", timeout=5000)
        except Exception:
            # 沒直接進表單，可能跳到草稿頁
            try:
                self.page.wait_for_url("**/draft-selection**", timeout=15000)
            except Exception as e:
                print(f"[Chiayi City 錯誤] 等待跳轉失敗: {e}")
                raise e
                
        print(f"[Chiayi City] 跳轉後目前 URL: {self.page.url}")
        
        # 如果進入了草稿選擇頁面 (draft-selection)
        if "draft-selection" in self.page.url:
            print("[Chiayi City] 偵測到未完成的申請草稿選擇頁...")
            
            # 1. 尋找「刪除」按鈕，如果存在則點擊刪除舊草稿
            delete_btn = self.page.locator("button:has-text('刪除'), a:has-text('刪除'), .btn-danger").first
            try:
                if delete_btn.is_visible(timeout=3000):
                    print("[Chiayi City] 正在點擊刪除舊申請草稿...")
                    delete_btn.click()
                    self.page.wait_for_timeout(1000)
                    
                    # 尋找自訂 HTML 彈窗中的「確定刪除」按鈕並點擊
                    confirm_delete_btn = self.page.locator("button:has-text('確定刪除'), a:has-text('確定刪除')").first
                    if confirm_delete_btn.is_visible(timeout=3000):
                        print("[Chiayi City] 點擊自訂彈窗內之「確定刪除」...")
                        confirm_delete_btn.click()
                        self.page.wait_for_timeout(2000) # 等待刪除 Ajax 完成
            except Exception as de:
                print(f"[Chiayi City 警告] 刪除舊草稿過程遭遇異常: {de}")
                
            # 2. 點擊「開始申請」按鈕
            start_btn = self.page.locator("button:has-text('開始申請'), a:has-text('開始申請'), button:has-text('開始'), a:has-text('開始')").first
            try:
                if start_btn.is_visible(timeout=3000):
                    print("[Chiayi City] 點擊「開始申請」按鈕啟動新流程...")
                    start_btn.click()
            except Exception as se:
                print(f"[Chiayi City 警告] 無法點擊開始新申請: {se}")
                
            # 3. 等待跳轉至填表頁面
            self.page.wait_for_url("**/application-data-form**", timeout=15000)
            
        print(f"[Chiayi City] 成功進入主要填表表單: {self.page.url}")
        self.page.wait_for_load_state("domcontentloaded")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Chiayi City] 正在填寫檢舉人基本資料...")
        
        # 1. 勾選法規條款同意
        self.page.check(self.config["selectors"]["agreement_checkbox"])
        
        # 2. 身分證字號
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        
        # 3. 聯絡電話與行動電話 (行動電話為 required)
        phone = reporter_data.get("reporter_phone", "")
        self.page.fill(self.config["selectors"]["reporter_phone_land"], phone)
        self.page.fill(self.config["selectors"]["reporter_phone_mobile"], phone)
        
        # 4. 聯絡地址 (智慧比對嘉義市東/西區)
        address = reporter_data.get("reporter_address", "")
        self.page.select_option(self.config["selectors"]["reporter_address_city"], value="嘉義市")
        
        if "東區" in address:
            self.page.select_option(self.config["selectors"]["reporter_address_district"], value="東區")
            detail_addr = address.split("東區")[-1].strip()
        elif "西區" in address:
            self.page.select_option(self.config["selectors"]["reporter_address_district"], value="西區")
            detail_addr = address.split("西區")[-1].strip()
        else:
            self.page.select_option(self.config["selectors"]["reporter_address_district"], value="西區")
            detail_addr = address.replace("嘉義市", "").strip()
            
        self.page.fill(self.config["selectors"]["reporter_address_detail"], detail_addr)

    def select_district(self, district_name: str) -> None:
        """暫存行政區，後續在違規發生地址中填寫"""
        if district_name:
            self.violation_district_name = district_name
            print(f"[Chiayi City] 暫存違規行政區: {district_name}")

    def select_village(self, village_name: str) -> None:
        # 嘉義市無此欄位，直接 pass
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Chiayi City] 正在填寫違規內容與時間...")
        
        # 1. 選擇車輛種類 (radio)
        car_type = violation_data.get("car_type", "汽車")
        if "機車" in car_type or "輕機" in car_type or "重機" in car_type:
            self.page.check(self.config["selectors"]["car_type_scooter"])
        else:
            self.page.check(self.config["selectors"]["car_type_car"])
            
        # 2. 車牌號碼 (單一欄位不拆分)
        license_plate = violation_data.get("license_plate", "")
        self.page.fill(self.config["selectors"]["violation_plate"], license_plate)
        
        # 3. 違規日期 (民國年、月、日連動 dropdowns)
        raw_date = violation_data.get("violation_date", "")
        date_parts = re.split(r"[-/.]", raw_date.strip())
        if len(date_parts) == 3:
            year_ad = int(date_parts[0])
            year_roc = str(year_ad - 1911)
            month = str(int(date_parts[1]))
            day = str(int(date_parts[2]))
            
            print(f"  - 選擇民國年: {year_roc}，月份: {month}，日期: {day}")
            # 民國年
            self.page.select_option(self.config["selectors"]["violation_year"], value=year_roc)
            self.page.wait_for_timeout(500)
            # 月份
            self.page.select_option(self.config["selectors"]["violation_month"], value=month)
            self.page.wait_for_timeout(500)
            # 日期
            self.page.select_option(self.config["selectors"]["violation_day"], value=day)
            self.page.wait_for_timeout(500)
        else:
            print(f"[Chiayi City 錯誤] 日期格式不符合規範，無法選擇: {raw_date}")
            
        # 4. 違規時間 (HTML5 time input)
        time_str = violation_data.get("violation_time", "") # HH:MM
        self.page.fill(self.config["selectors"]["violation_time"], time_str)
        
        # 5. 違規項目 (檢舉法條 select)
        category = violation_data.get("violation_category", "")
        subcategory = violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")
        
        print(f"  - 選擇違規項目: '{category}' (小類關鍵字: '{subcategory[:10]}')")
        kw = subcategory[:10] if subcategory else category
        success = self._select_option_by_keyword(self.config["selectors"]["violation_category"], kw)
        if not success:
            success = self._select_option_by_keyword(self.config["selectors"]["violation_category"], category)
        if not success:
            print("[Chiayi City 警告] 無法匹配違規法條，將嘗試選擇第一個可選項目。")
            try:
                self.page.select_option(self.config["selectors"]["violation_category"], index=1)
            except Exception:
                pass
                
        # 6. 違規詳細發生地點 (連動縣市/行政區/詳細地址)
        location = violation_data.get("violation_location", "")
        self.page.select_option(self.config["selectors"]["violation_address_city"], value="嘉義市")
        
        # 判斷東區或西區
        district_kw = self.violation_district_name
        if "東區" in location or "東區" in district_kw:
            self.page.select_option(self.config["selectors"]["violation_address_district"], value="東區")
            v_detail = location.replace("嘉義市", "").replace("東區", "").strip()
        else:
            self.page.select_option(self.config["selectors"]["violation_address_district"], value="西區")
            v_detail = location.replace("嘉義市", "").replace("西區", "").strip()
            
        self.page.fill(self.config["selectors"]["violation_address_detail"], v_detail)
        
        # 7. 違規敘述 (寫入其他意見 textarea)
        self.page.fill(self.config["selectors"]["violation_description"], violation_data.get("violation_description", ""))

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Chiayi City] 沒有提供附加媒體檔案。")
            return
            
        # 區分圖片與影片檔案
        image_paths = []
        video_paths = []
        for path in media_paths:
            if not os.path.exists(path):
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext in ['.mp4', '.avi', '.mov', '.webm', '.wmv', '.mkv']:
                video_paths.append(path)
            else:
                image_paths.append(path)
                
        # 上傳圖片 (最常用，最多 3 張)
        if image_paths:
            print(f"[Chiayi City] 正在上傳圖片 (最多 3 張): {image_paths[:3]}")
            self.page.set_input_files(self.config["selectors"]["file_uploads_image"], image_paths[:3])
            self.page.wait_for_timeout(1000)
            
        # 上傳影片 (最多 2 支)
        if video_paths:
            print(f"[Chiayi City] 正在上傳影片 (最多 2 支): {video_paths[:2]}")
            self.page.set_input_files(self.config["selectors"]["file_uploads_video"], video_paths[:2])
            self.page.wait_for_timeout(1000)

    def handle_verification(self) -> None:
        # 嘉義市已在最開頭完成 Pre-verification，此處直接跳過驗證碼步驟 (若表單底部無額外圖形驗證碼的話)
        pass

    def submit(self) -> bool:
        if "application-data-form" not in self.page.url:
            print("[Chiayi City] 偵測到網頁已提前完成跳轉，無須重複點擊送出。")
            return self.wait_for_success()
            
        print("[Chiayi City] 執行最後送出點擊...")
        self.page.click(self.config["selectors"]["submit_btn"])
        return self.wait_for_success()

    def wait_for_success(self) -> bool:
        """等待並判斷是否發送成功"""
        print("[Chiayi City] 正在驗證是否檢舉成功...")
        try:
            self.page.wait_for_function("() => !window.location.href.includes('application-data-form')", timeout=10000)
            current_url = self.page.url
            print(f"[Chiayi City] 已跳離填寫頁面，目前 URL: {current_url}")
            return True
        except Exception as e:
            print(f"[Chiayi City 警告] 驗證成功狀態判定超時: {e}")
            
        return False

    # ==================== 輔助方法 ====================

    def _select_option_by_keyword(self, selector: str, keyword: str) -> bool:
        """根據關鍵字模糊比對並選取下拉選單選項"""
        try:
            options = self.page.locator(f"{selector} option").all()
            for opt in options:
                val = opt.evaluate("el => el.value")
                text = opt.evaluate("el => el.text")
                if keyword in text or keyword == val:
                    self.page.select_option(selector, value=val)
                    print(f"  - 下拉選單 [{selector}] 已成功選擇: {text}")
                    return True
        except Exception as e:
            print(f"[Chiayi City 警告] 無法由關鍵字選取下拉選單 [{selector}]: {e}")
        return False
