import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class KaohsiungDriver(BaseDriver):
    """
    高雄市交通違規檢舉填表策略 (Strategy Pattern)。
    對應網址: https://policemail.kcg.gov.tw/Statement.aspx
    """

    def navigate_to_start(self) -> None:
        print(f"[Kaohsiung] 正在導覽至起始聲明頁面: {self.config['start_url']}")
        self.page.goto(self.config["start_url"])
        self.page.wait_for_load_state("domcontentloaded")

    def handle_pre_actions(self) -> None:
        print("[Kaohsiung] 正在執行前置同意書勾選...")
        # 勾選四個條款
        for selector in self.config["selectors"]["agreement_checkboxes"]:
            self.page.check(selector)
            
        print("[Kaohsiung] 點擊「我要檢舉」按鈕...")
        self.page.click(self.config["selectors"]["agreement_submit"])
        
        # 等待網址跳轉至 Mail.aspx
        self.page.wait_for_url("**/Mail.aspx**", timeout=15000)
        print(f"[Kaohsiung] 成功進入資料填寫頁面: {self.page.url}")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Kaohsiung] 正在填寫檢舉人基本資料...")
        
        # 1. 姓名
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        
        # 2. 身份證字號
        reporter_id = reporter_data.get("reporter_id", "").upper()
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_id)
        
        # 3. 國籍 (預設本國籍)
        self.page.check(self.config["selectors"]["reporter_nationality_domestic"])
        
        # 4. 性別 (依身分證字號第二碼自動判定)
        if len(reporter_id) > 1:
            gender_code = reporter_id[1]
            if gender_code == '1':
                self.page.check(self.config["selectors"]["reporter_gender_male"])
            elif gender_code == '2':
                self.page.check(self.config["selectors"]["reporter_gender_female"])
                
        # 5. 地址
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))
        
        # 6. E-mail
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))
        
        # 7. 電話
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))

    def select_district(self, district_name: str) -> None:
        """選擇違規行政區 (處理兩個行政區 dropdown，支援模糊地名與分局剔除)"""
        if not district_name:
            return
            
        print(f"[Kaohsiung] 正在選擇違規行政區: {district_name}")
        
        # 清理行政區贅字與後綴，如「高雄市」、「分局」、「派出所」、「區」、「鄉」、「鎮」
        clean_district = district_name.replace("高雄市", "").replace("分局", "").replace("派出所", "").strip()
        clean_district = clean_district.rstrip("區鄉鎮市")

        # 選擇「違規行政區」 #ContentPlaceHolder1_ViolationArea
        self._select_option_by_keyword(self.config["selectors"]["violation_district"], clean_district)
        
        # 選擇「交通違規地點行政區」 #ContentPlaceHolder1_uscPlace_ddlArea
        self._select_option_by_keyword(self.config["selectors"]["violation_location_district"], clean_district)

    def select_village(self, village_name: str) -> None:
        # 高雄市表單無此欄位，直接 pass
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Kaohsiung] 正在填寫違規內容與時間...")
        
        # 1. 格式化並填寫違規日期/時間 (相容 YYYYMMDD 等非標準格式)
        date_str = self._format_date(violation_data.get("violation_date", ""))
        time_str = violation_data.get("violation_time", "") # HH:MM
        datetime_val = f"{date_str} {time_str}".strip()
        
        self.page.fill(self.config["selectors"]["violation_date"], datetime_val)
        
        # 填寫並調整隱藏或輔助的 hours / minutes range 滑桿以防萬一
        time_parts = time_str.split(":")
        if len(time_parts) == 2:
            try:
                h_val = str(int(time_parts[0]))
                m_val = str(int(time_parts[1]))
                self.page.fill(self.config["selectors"]["time_hours"], h_val)
                self.page.fill(self.config["selectors"]["time_minutes"], m_val)
                print(f"  - 輔助滑桿設定成功：{h_val} 時 {m_val} 分")
            except Exception as te:
                print(f"  - 無法設定輔助滑桿 (忽略): {te}")
        
        # 2. 車牌號碼拆分 (高雄市拆分為兩欄：前/後)
        license_plate = violation_data.get("license_plate", "")
        parts = license_plate.split("-")
        if len(parts) == 2:
            self.page.fill(self.config["selectors"]["violation_plate_part1"], parts[0].strip())
            self.page.fill(self.config["selectors"]["violation_plate_part2"], parts[1].strip())
        else:
            self.page.fill(self.config["selectors"]["violation_plate_part1"], license_plate[:3])
            self.page.fill(self.config["selectors"]["violation_plate_part2"], license_plate[3:])
            
        # 3. 車種 (汽車/重機/輕機)
        car_type = violation_data.get("car_type", "汽車")
        if "重機" in car_type or "大型重機" in car_type:
            self.page.check(self.config["selectors"]["car_type_heavy_moto"])
        elif "輕機" in car_type or "機車" in car_type:
            self.page.check(self.config["selectors"]["car_type_light_moto"])
        else:
            self.page.check(self.config["selectors"]["car_type_auto"])
            
        # 4. 違規事實大類 (Dropdown) 與小類細項
        category = violation_data.get("violation_category", "")
        subcategory = violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")
        
        print(f"  - 嘗試選擇違規大類: '{category}' ...")
        
        def do_select_category() -> bool:
            # 優先嘗試以大類選取
            ok = self._select_option_by_keyword(self.config["selectors"]["violation_category"], category)
            # 若失敗且有提供小類，嘗試以小類關鍵字在「大類下拉選單」中尋找
            # (例如：大類寫「交通違規檢舉專區」，小類為「闖紅燈」，此時能在選單中找到「不當駕駛行為(闖紅燈)」)
            if not ok and subcategory:
                kw = subcategory[:10]
                print(f"  - 大類精確匹配失敗，嘗試以小類關鍵字 '{kw}' 匹配大類選單...")
                ok = self._select_option_by_keyword(self.config["selectors"]["violation_category"], kw)
            return ok

        # 下拉大類選擇會觸發 AutoPostBack (頁面刷新以加載小類)
        try:
            with self.page.expect_navigation(timeout=5000):
                success = do_select_category()
                if not success:
                    raise ValueError("No matched category")
        except Exception:
            # 後備等待時間
            self.page.wait_for_timeout(2000)
            
        # 5. 違規事實小類細項 (Dropdown)
        if subcategory:
            clean_sub = subcategory[:15]
            print(f"  - 選擇違規小類細項 (比對關鍵字: '{clean_sub}'):")
            self._select_option_by_keyword(self.config["selectors"]["violation_subcategory"], clean_sub)
        
        # 6. 違規詳細地點
        self.page.fill(self.config["selectors"]["violation_location_address"], violation_data.get("violation_location", ""))
        
        # 7. 違規事實陳述/內容描述
        self.page.fill(self.config["selectors"]["violation_description"], violation_data.get("violation_description", ""))

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Kaohsiung] 沒有提供附加媒體檔案。")
            return
            
        print(f"[Kaohsiung] 正在上傳附加檔案 (限制最多 3 個): {media_paths}")
        
        # 高雄市真正的上傳策略是使用一個單一的可見 File 欄位與一個「上傳」按鈕
        # 逐一選擇並點擊上傳 (每次上傳皆會引起 ASP.NET 頁面 postback)
        file_input_selector = self.config["selectors"]["file_upload_input"]
        upload_btn_selector = self.config["selectors"]["file_upload_submit"]
        
        for idx, path in enumerate(media_paths[:3]):
            if os.path.exists(path):
                print(f"  - 正在選取第 {idx+1} 個檔案: {path}")
                self.page.set_input_files(file_input_selector, path)
                
                print(f"  - 點擊「上傳」按鈕並等待頁面更新...")
                try:
                    with self.page.expect_navigation(timeout=20000):
                        self.page.click(upload_btn_selector)
                    print(f"  - 第 {idx+1} 個檔案上傳完成。")
                except Exception as ue:
                    print(f"  - 檔案上傳發生超時或更新異常 (忽略並繼續): {ue}")
                    self.page.wait_for_timeout(3000)

    def handle_verification(self) -> None:
        print("[Kaohsiung] 觸發手動驗證 Hook...")
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        # 檢查是否已完成驗證並跳轉
        if "Mail.aspx" not in self.page.url:
            print("[Kaohsiung] 偵測到網頁已提前完成跳轉，無須重複點擊送出。")
            return self.wait_for_success()
            
        print("[Kaohsiung] 執行最後送出點擊...")
        self.page.click(self.config["selectors"]["submit_btn"])
        return self.wait_for_success()

    def wait_for_success(self) -> bool:
        """等待並判斷是否發送成功"""
        print("[Kaohsiung] 正在驗證是否檢舉成功...")
        try:
            self.page.wait_for_function("() => !window.location.href.includes('Mail.aspx')", timeout=10000)
            current_url = self.page.url
            print(f"[Kaohsiung] 已跳離填寫頁面，目前 URL: {current_url}")
            
            if "DisplayCaptcha" not in current_url:
                print("[Kaohsiung] 成功判定：已進入收取驗證信階段。")
                return True
        except Exception as e:
            print(f"[Kaohsiung 警告] 驗證成功狀態判定超時: {e}")
            
        return False

    # ==================== 輔助與格式化方法 ====================
    
    def _format_date(self, date_str: str) -> str:
        """將各種日期格式（如 20260706、2026/07/06 等）標準化為 YYYY-MM-DD"""
        date_str = date_str.replace("/", "-").replace(".", "-").strip()
        # 情況 A: 8碼純數字 (例如 20260706)
        if re.match(r"^\d{8}$", date_str):
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        # 情況 B: 已經是 YYYY-MM-DD 或 YYYY-M-D
        match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", date_str)
        if match:
            y, m, d = match.groups()
            return f"{y}-{int(m):02d}-{int(d):02d}"
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
            print(f"[Kaohsiung 警告] 無法由關鍵字選取下拉選單 [{selector}]: {e}")
        return False
