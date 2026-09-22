import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class TainanDriver(BaseDriver):
    """
    台南市交通違規檢舉填表策略 (Strategy Pattern)。
    對應網址: https://tr.tnpd.gov.tw/TrafficMailbox/Index/92929b01-cf5e-99d6-2539-bee668350a6d
    """

    def navigate_to_start(self) -> None:
        print(f"[Tainan] 正在導覽至起始聲明頁面: {self.config['start_url']}")
        self.page.goto(self.config["start_url"])
        self.page.wait_for_load_state("domcontentloaded")
        
        # 註冊對話方塊 (Alert/Confirm) 自動確認處理器，避免 ASP.NET 彈窗阻塞
        self.page.on("dialog", lambda dialog: dialog.accept())

    def handle_pre_actions(self) -> None:
        print("[Tainan] 正在執行前置同意書勾選...")
        self.page.check(self.config["selectors"]["agreement_checkbox"])
        
        print("[Tainan] 點擊「下一步」按鈕...")
        self.page.click(self.config["selectors"]["agreement_submit"])
        
        # 等待網址跳轉至 Create 填表頁面
        self.page.wait_for_url("**/TrafficMailbox/Create**", timeout=15000)
        print(f"[Tainan] 成功進入資料填寫頁面: {self.page.url}")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Tainan] 正在填寫檢舉人基本資料...")
        
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
        """選擇違規行政區，並等待違規事項 Ajax 載入"""
        if not district_name:
            return
            
        print(f"[Tainan] 正在選擇違規行政區: {district_name}")
        
        # 清理行政區名稱 (例如 "台南市永康區" -> "永康")
        clean_district = district_name.replace("台南市", "").replace("台南", "").replace("分局", "").replace("派出所", "").strip()
        clean_district = clean_district.rstrip("區鄉鎮市")
        
        # 選擇「違規行政區」 #violation_place_area
        success = self._select_option_by_keyword(self.config["selectors"]["violation_district"], clean_district)
        if success:
            # 選擇完行政區後，網頁會透過 Ajax 動態加載違規事項 (#itemno)，等待 2.5 秒
            print("[Tainan] 已選取行政區，等待違規事項下拉選單更新...")
            self.page.wait_for_timeout(2500)

    def select_village(self, village_name: str) -> None:
        # 台南市表單無此欄位，直接 pass
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Tainan] 正在填寫違規內容與時間...")
        
        # 1. 主題 (Tainan 欄位為 Subject，填寫違規事实敘述)
        desc = violation_data.get("violation_description", "")
        # 台南主題有限制 200 字以內，進行截斷避免出錯
        if len(desc) > 200:
            desc = desc[:197] + "..."
        self.page.fill(self.config["selectors"]["violation_subject"], desc)
        
        # 2. 違規地點 (台南包含：一個可搜尋的自訂路段下拉選單，與一個詳細地點/門牌輸入框)
        location = violation_data.get("violation_location", "")
        print(f"  - 嘗試選擇違規路段 (違規地點: {location}):")
        
        try:
            # 點擊道路搜尋框顯示下拉列表
            self.page.click(self.config["selectors"]["violation_road_search"])
            self.page.wait_for_timeout(1000)
            
            # 獲取所有下拉路段選項
            items = self.page.locator("#roadDropdownList .dropdown-item").all()
            matched_item = None
            longest_match_len = 0
            
            # 從地點名稱（例如 "中山南路與東橋七路路口"）中分割多條可能路段
            import re
            candidates = [c.strip() for c in re.split(r"[與和跟及]", location) if c.strip()]
            
            for item in items:
                item_text = item.inner_text().strip()
                if not item_text:
                    continue
                # 檢查選項是否為候選路段，或者該選項文字被包含在完整地點字串中
                is_sub = item_text in location
                if is_sub and len(item_text) > longest_match_len:
                    matched_item = item
                    longest_match_len = len(item_text)
            
            if matched_item:
                matched_text = matched_item.inner_text().strip()
                print(f"    -> 成功匹配並選擇路段: {matched_text}")
                matched_item.click()
                self.page.wait_for_timeout(800)
            else:
                print("    -> 未能匹配到合適路段，嘗試選擇第一個預設項目...")
                if items:
                    items[0].click()
                    self.page.wait_for_timeout(500)
        except Exception as re_err:
            print(f"  - [Tainan 警告] 無法完成路段下拉選單選取 (忽略並改由全文字填寫): {re_err}")
            
        # 填寫詳細路段地點
        self.page.fill(self.config["selectors"]["violation_location_address"], location)
        
        # 3. 違規日期 (台南為 select 下拉選單，格式為 YYYY/MM/DD)
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_date_slashes(raw_date)
        print(f"  - 選擇違規日期 (格式化後: {formatted_date}):")
        date_selected = self._select_option_by_keyword(self.config["selectors"]["violation_date"], formatted_date)
        if not date_selected:
            print(f"[Tainan 警告] 無法匹配日期選單選項 '{formatted_date}'，將嘗試選擇第一個非空日期。")
            # 備用選取第一個有效的日期 (index 1)
            try:
                self.page.select_option(self.config["selectors"]["violation_date"], index=1)
            except Exception:
                pass
                
        # 4. 違規時間 (台南拆分為 violation_time1 與 violation_time2 兩個 dropdown)
        time_str = violation_data.get("violation_time", "") # HH:MM
        time_parts = time_str.split(":")
        if len(time_parts) == 2:
            try:
                h_val = str(int(time_parts[0]))
                m_val = str(int(time_parts[1]))
                self.page.select_option(self.config["selectors"]["violation_time_hours"], value=h_val)
                self.page.select_option(self.config["selectors"]["violation_time_minutes"], value=m_val)
                print(f"  - 下拉時間設定成功：{h_val} 時 {m_val} 分")
            except Exception as te:
                print(f"  - 無法設定下拉時間: {te}")
                
        # 5. 車牌號碼拆分 (台南拆分為前/後兩欄)
        license_plate = violation_data.get("license_plate", "")
        parts = license_plate.split("-")
        if len(parts) == 2:
            self.page.fill(self.config["selectors"]["violation_plate_part1"], parts[0].strip())
            self.page.fill(self.config["selectors"]["violation_plate_part2"], parts[1].strip())
        else:
            self.page.fill(self.config["selectors"]["violation_plate_part1"], license_plate[:3])
            self.page.fill(self.config["selectors"]["violation_plate_part2"], license_plate[3:])
            
        # 6. 違規事項 (itemno 下拉選單，Ajax 已載入)
        category = violation_data.get("violation_category", "")
        subcategory = violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")
        
        print(f"  - 選擇違規事項: '{category}' (小類關鍵字: '{subcategory[:10]}')")
        # 優先嘗試以 subcategory (如 "闖紅燈") 做為關鍵字搜尋選項
        kw = subcategory[:10] if subcategory else category
        success = self._select_option_by_keyword(self.config["selectors"]["violation_category"], kw)
        if not success:
            # 備用嘗試大類比對
            success = self._select_option_by_keyword(self.config["selectors"]["violation_category"], category)
        if not success:
            # 若皆失敗，嘗試選擇第一個有效選項
            print("[Tainan 警告] 無法匹配違規事項，將嘗試選擇第一個可選項目。")
            try:
                self.page.select_option(self.config["selectors"]["violation_category"], index=1)
            except Exception:
                pass

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Tainan] 沒有提供附加媒體檔案。")
            return
            
        print(f"[Tainan] 正在上傳附加檔案 (最多 6 個): {media_paths}")
        
        # 台南市提供 6 個獨立的上傳輸入框 (Upfile1 ~ Upfile6)
        upload_selectors = self.config["selectors"]["file_uploads"]
        for idx, path in enumerate(media_paths[:6]):
            if idx < len(upload_selectors) and os.path.exists(path):
                print(f"  - 上傳檔案到槽位 {idx+1}: {path}")
                self.page.set_input_files(upload_selectors[idx], path)

    def handle_verification(self) -> None:
        # 台南市在驗證碼填寫前，必須先點擊「寄送認證郵件」按鈕發送確認郵件
        print("[Tainan] 點擊「寄送認證郵件」按鈕...")
        try:
            self.page.click(self.config["selectors"]["email_send_verify"])
            self.page.wait_for_timeout(1000) # 給予發送請求一個短暫緩衝
        except Exception as e:
            print(f"[Tainan 警告] 點擊認證郵件按鈕失敗 (可能已點擊或無需點擊): {e}")

        # 呼叫註冊的人機協作驗證插件，暫停主流程等待使用者輸入驗證碼
        print("[Tainan] 觸發手動驗證 Hook...")
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        if "Create" not in self.page.url:
            print("[Tainan] 偵測到網頁已提前完成跳轉，無須重複點擊送出。")
            return self.wait_for_success()
            
        print("[Tainan] 執行最後送出點擊...")
        self.page.click(self.config["selectors"]["submit_btn"])
        return self.wait_for_success()

    def wait_for_success(self) -> bool:
        """等待並判斷是否發送成功"""
        print("[Tainan] 正在驗證是否檢舉成功...")
        try:
            # 成功送出後，應跳轉離開 Create 頁面 (或提示成功的 DOM)
            self.page.wait_for_function("() => !window.location.href.includes('Create')", timeout=10000)
            current_url = self.page.url
            print(f"[Tainan] 已跳離填寫頁面，目前 URL: {current_url}")
            return True
        except Exception as e:
            print(f"[Tainan 警告] 驗證成功狀態判定超時: {e}")
            
        return False

    # ==================== 輔助與格式化方法 ====================
    
    def _format_date_slashes(self, date_str: str) -> str:
        """將各種日期格式（如 20260706、2026-07-06）格式化為 YYYY/MM/DD 以匹配台南下拉選單"""
        date_str = date_str.replace("-", "/").replace(".", "/").strip()
        # 情況 A: 8碼純數字 (例如 20260706)
        if re.match(r"^\d{8}$", date_str):
            return f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
        # 情況 B: 已經是 YYYY/MM/DD 或 YYYY/M/D
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
            print(f"[Tainan 警告] 無法由關鍵字選取下拉選單 [{selector}]: {e}")
        return False
