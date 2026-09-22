import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class ChiayiDriver(BaseDriver):
    """
    嘉義縣交通違規檢舉填表策略 (Strategy Pattern)。
    對應網址: https://www.cypd.gov.tw/TrafficMailbox/f21e7f75-04e2-082d-d2ae-1d038340ed7b
    """

    def navigate_to_start(self) -> None:
        print(f"[Chiayi] 正在導覽至起始聲明頁面: {self.config['start_url']}")
        self.page.goto(self.config["start_url"])
        self.page.wait_for_load_state("domcontentloaded")
        
        # 註冊對話方塊自動確認，避免彈窗阻塞
        self.page.on("dialog", lambda dialog: dialog.accept())

    def handle_pre_actions(self) -> None:
        print("[Chiayi] 正在執行前置同意書勾選...")
        self.page.check(self.config["selectors"]["agreement_checkbox"])
        
        print("[Chiayi] 點擊「送出」按鈕...")
        self.page.click(self.config["selectors"]["agreement_submit"])
        
        # 等待網址跳轉至 Create 填表頁面
        self.page.wait_for_url("**/TrafficMailbox/Create**", timeout=15000)
        print(f"[Chiayi] 成功進入資料填寫頁面: {self.page.url}")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Chiayi] 正在填寫檢舉人基本資料...")
        
        # 1. 姓名
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        
        # 2. 身分證字號
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        
        # 3. 電話
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        
        # 4. 地址
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))
        
        # 5. E-MAIL
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))

    def select_district(self, district_name: str) -> None:
        """選擇違規行政區"""
        if not district_name:
            return
            
        print(f"[Chiayi] 正在選擇違規行政區: {district_name}")
        
        # 清理行政區名稱 (例如 "嘉義縣民雄鄉" -> "民雄")
        clean_district = district_name.replace("嘉義縣", "").replace("嘉義市", "").replace("嘉義", "").replace("分局", "").replace("派出所", "").strip()
        clean_district = clean_district.rstrip("區鄉鎮市")
        
        # 選擇「違規點行政區」 #checkCountry
        self._select_option_by_keyword(self.config["selectors"]["violation_district"], clean_district)

    def select_village(self, village_name: str) -> None:
        # 嘉義縣表單無此欄位，直接 pass
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Chiayi] 正在填寫違規內容與時間...")
        
        # 1. 車牌號碼 (嘉義縣為單一欄位，直接寫入完整車牌，不進行拆分)
        license_plate = violation_data.get("license_plate", "")
        self.page.fill(self.config["selectors"]["violation_plate"], license_plate)
        
        # 2. 違規地點描述
        self.page.fill(self.config["selectors"]["violation_location_address"], violation_data.get("violation_location", ""))
        
        # 3. 違規日期 (嘉義為 select 下拉選單，格式為 YYYY/MM/DD)
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_date_slashes(raw_date)
        print(f"  - 選擇違規日期 (格式化後: {formatted_date}):")
        date_selected = self._select_option_by_keyword(self.config["selectors"]["violation_date"], formatted_date)
        if not date_selected:
            print(f"[Chiayi 警告] 無法匹配日期選單選項 '{formatted_date}'，將嘗試選擇第一個非空日期。")
            try:
                self.page.select_option(self.config["selectors"]["violation_date"], index=0)
            except Exception:
                pass
                
        # 4. 違規時間 (嘉義拆分為 checkHour 與 checkMin 兩個 dropdown，支援單雙位 value)
        time_str = violation_data.get("violation_time", "") # HH:MM
        time_parts = time_str.split(":")
        if len(time_parts) == 2:
            try:
                h_val = str(int(time_parts[0]))
                h_val_2d = f"{int(time_parts[0]):02d}"
                try:
                    self.page.select_option(self.config["selectors"]["violation_time_hours"], value=h_val_2d)
                except Exception:
                    self.page.select_option(self.config["selectors"]["violation_time_hours"], value=h_val)
                    
                m_val = str(int(time_parts[1]))
                m_val_2d = f"{int(time_parts[1]):02d}"
                try:
                    self.page.select_option(self.config["selectors"]["violation_time_minutes"], value=m_val_2d)
                except Exception:
                    self.page.select_option(self.config["selectors"]["violation_time_minutes"], value=m_val)
                print(f"  - 下拉時間設定成功：{time_parts[0]} 時 {time_parts[1]} 分")
            except Exception as te:
                print(f"  - 無法設定下拉時間: {te}")
                
        # 5. 違規事項 (TrafficCategoryId 下拉選單，頁面加載時已全部預載)
        category = violation_data.get("violation_category", "")
        subcategory = violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")
        
        print(f"  - 選擇違規項目: '{category}' (小類關鍵字: '{subcategory[:10]}')")
        kw = subcategory[:10] if subcategory else category
        success = self._select_option_by_keyword(self.config["selectors"]["violation_category"], kw)
        if not success:
            success = self._select_option_by_keyword(self.config["selectors"]["violation_category"], category)
        if not success:
            print("[Chiayi 警告] 無法匹配違規事項，將嘗試選擇第一個可選項目。")
            try:
                self.page.select_option(self.config["selectors"]["violation_category"], index=1)
            except Exception:
                pass

        # 6. 違規描述 (Memo)
        self.page.fill(self.config["selectors"]["violation_description"], violation_data.get("violation_description", ""))
        
        # 7. 勾選同意注意事項與個資條款
        self.page.check(self.config["selectors"]["agreement_collect"])

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Chiayi] 沒有提供附加媒體檔案。")
            return
            
        print(f"[Chiayi] 正在上傳附加檔案 (最多 5 個): {media_paths}")
        
        # 嘉義提供 5 個獨立的上傳輸入框 (checkFile1 ~ checkFile5)
        upload_selectors = self.config["selectors"]["file_uploads"]
        for idx, path in enumerate(media_paths[:5]):
            if idx < len(upload_selectors) and os.path.exists(path):
                print(f"  - 上傳檔案到槽位 {idx+1}: {path}")
                self.page.set_input_files(upload_selectors[idx], path)

    def handle_verification(self) -> None:
        # 呼叫註冊的人機協作驗證插件，暫停主流程等待使用者輸入驗證碼
        print("[Chiayi] 觸發手動驗證 Hook...")
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        if "Create" not in self.page.url:
            print("[Chiayi] 偵測到網頁已提前完成跳轉，無須重複點擊送出。")
            return self.wait_for_success()
            
        print("[Chiayi] 執行最後送出點擊...")
        self.page.click(self.config["selectors"]["submit_btn"])
        return self.wait_for_success()

    def wait_for_success(self) -> bool:
        """等待並判斷是否發送成功"""
        print("[Chiayi] 正在驗證是否檢舉成功...")
        try:
            self.page.wait_for_function("() => !window.location.href.includes('Create')", timeout=10000)
            current_url = self.page.url
            print(f"[Chiayi] 已跳離填寫頁面，目前 URL: {current_url}")
            return True
        except Exception as e:
            print(f"[Chiayi 警告] 驗證成功狀態判定超時: {e}")
            
        return False

    # ==================== 輔助與格式化方法 ====================
    
    def _format_date_slashes(self, date_str: str) -> str:
        """將各種日期格式（如 20260706、2026-07-06）格式化為 YYYY/MM/DD 以匹配嘉義下拉選單"""
        date_str = date_str.replace("-", "/").replace(".", "/").strip()
        if re.match(r"^\d{8}$", date_str):
            return f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
        match = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", date_str)
        if match:
            y, m, d = match.groups()
            return f"{y}/{int(m):02d}/{int(d):02d}"
        return date_str

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
            print(f"[Chiayi 警告] 無法由關鍵字選取下拉選單 [{selector}]: {e}")
        return False
