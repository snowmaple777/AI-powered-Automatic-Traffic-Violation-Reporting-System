import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class HsinchuCityDriver(BaseDriver):
    """
    新竹市交通違規檢舉自動填表策略 (Hsinchu City Driver)
    """

    TOWN_MAP = {
        "東區": "東區",
        "北區": "北區",
        "香山區": "香山區",
        "香山": "香山區"
    }

    def navigate_to_start(self) -> None:
        print(f"[HsinchuCity] 正在導航至新竹市檢舉首頁: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(1000)

    def handle_pre_actions(self) -> None:
        print("[HsinchuCity] 勾選條款並進入新竹市檢舉表單頁面...")
        self.page.check(self.config["selectors"]["agree_checkbox"])
        self.page.click(self.config["selectors"]["agree_submit"])
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_selector(self.config["selectors"]["email"], state="visible", timeout=10000)
        print("[HsinchuCity] 成功進入新竹市檢舉表單填寫頁面！")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[HsinchuCity] 正在填寫檢舉人聯絡資料...")
        
        # 1. 填寫 Email 並觸發驗證
        email_val = reporter_data.get("reporter_email", "")
        self.page.fill(self.config["selectors"]["email"], email_val)
        print(f"  - 填寫 Email: {email_val}，點擊「驗證」解鎖預覽按鈕...")
        self.page.click(self.config["selectors"]["email_verify_btn"])
        self.page.wait_for_timeout(1500)  # 等待綠色勾勾出現

        # 2. 填寫姓名、身分證、電話、住址
        self.page.fill(self.config["selectors"]["name"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["id_number"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["contact_address"], reporter_data.get("reporter_address", ""))
        self.page.fill(self.config["selectors"]["phone"], reporter_data.get("reporter_phone", ""))

    def select_district(self, district_name: str) -> None:
        clean_dist = district_name.strip()
        target_town = "東區"
        for key, val in self.TOWN_MAP.items():
            if key in clean_dist:
                target_town = val
                break
        print(f"  - 選擇行政區: {target_town}")
        self.page.select_option(self.config["selectors"]["area_district"], label=target_town)
        self.page.wait_for_timeout(1000)  # 等待路段下拉選單重新載入

    def select_village(self, village_name: str) -> None:
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[HsinchuCity] 正在填寫違規內容資料...")

        # 1. 填寫違規時間
        formatted_date = self._format_date(violation_data.get("violation_date", ""))
        hour_str, min_str = self._parse_hour_minute(violation_data.get("violation_time", "00:00"))
        
        print(f"  - 選擇違規日期: {formatted_date}, 時間: {hour_str} 時 {min_str} 分")
        date_selector = self.config["selectors"]["violated_date"]
        options = self.page.locator(f"{date_selector} option").all()
        valid_values = [opt.evaluate("el => el.value") for opt in options if opt.evaluate("el => el.value")]
        
        if formatted_date in valid_values:
            self.page.select_option(date_selector, value=formatted_date)
        elif valid_values:
            fallback_date = valid_values[-1]  # 取得選單中最早（或最新）的有效日期
            print(f"  [HsinchuCity 提示] 違規日期 {formatted_date} 超出 7 天限制，使用自動退回值: {fallback_date}")
            self.page.select_option(date_selector, value=fallback_date)
            
        self.page.select_option(self.config["selectors"]["violated_hour"], value=hour_str)
        self.page.select_option(self.config["selectors"]["violated_min"], value=min_str)

        # 2. 選擇車種類型與車牌填寫
        car_type = violation_data.get("car_type", "汽車")
        type_label = "汽車"
        if "重" in car_type or "大型" in car_type:
            type_label = "重型機車(含白紅黃牌)"
        elif "輕" in car_type or "綠牌" in car_type:
            type_label = "輕型機車(綠牌)"
            
        print(f"  - 選擇車種類型: {type_label}")
        self.page.select_option(self.config["selectors"]["car_type"], label=type_label)
        
        plate = violation_data.get("license_plate", "")
        prefix, suffix = self._split_license_plate(plate)
        print(f"  - 填寫車牌號碼: 前碼 '{prefix}', 後碼 '{suffix}'")
        self.page.fill(self.config["selectors"]["first_car_number"], prefix)
        self.page.fill(self.config["selectors"]["last_car_number"], suffix)

        # 3. 填寫違規地點與路段比對
        district_name = violation_data.get("district", "東區")
        self.select_district(district_name)
        
        full_location = violation_data.get("violation_location", "")
        print(f"  - 拆分違規地點細項: '{full_location}'")
        self._parse_and_fill_location(full_location, district_name)

        # 4. 違規事實法條選取
        fact_desc = (violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")).strip()
        print(f"  - 搜尋並配對違規法條 (關鍵字: '{fact_desc}')...")
        options = self.page.locator(f"{self.config['selectors']['illegality_code']} option").all()
        selected = False
        for opt in options:
            txt = opt.evaluate("el => el.innerText")
            val = opt.evaluate("el => el.value")
            if "55" in txt or "56" in txt or "人行道" in txt or "停車" in txt:
                self.page.select_option(self.config["selectors"]["illegality_code"], value=val)
                selected = True
                print(f"    - 成功配對法條: '{txt[:30]}...'")
                break
                
        if not selected and len(options) > 1:
            val = options[1].evaluate("el => el.value")
            self.page.select_option(self.config["selectors"]["illegality_code"], value=val)
            print("    - 選取預設法條。")

        # 5. 違規說明 (case_illegality_details，若可見時填寫)
        desc_text = violation_data.get("violation_description", "")
        try:
            if self.page.is_visible(self.config["selectors"]["illegality_details"], timeout=1000):
                self.page.fill(self.config["selectors"]["illegality_details"], desc_text)
        except Exception:
            pass

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[HsinchuCity] 沒有提供附加媒體檔案。")
            return
            
        valid_paths = [p for p in media_paths if os.path.exists(p)]
        if not valid_paths:
            print("[HsinchuCity] 未找到任何有效的媒體檔案。")
            return
            
        print(f"[HsinchuCity] 正在上傳附加檔案 (支援多達 5 個槽分流): {[os.path.basename(p) for p in valid_paths]}")
        for idx, path in enumerate(valid_paths[:5]):
            selector = f"#file_input_{idx}"
            if self.page.is_visible(selector):
                self.page.set_input_files(selector, path)
                self.page.wait_for_timeout(500)

    def handle_verification(self) -> None:
        print("[HsinchuCity] 填表完成，勾選聲明條款並儲存預覽截圖...")
        self.page.check(self.config["selectors"]["read_statement"], force=True)
        self.page.wait_for_timeout(500)
        
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/hcc_filled_preview.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[HsinchuCity] 填寫完成之全頁面截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[HsinchuCity] 儲存截圖失敗: {e}")
            
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        print("[HsinchuCity] 自動填表完畢，安全停留在預覽提交前，請確認無誤後點擊「預覽」手動提交。")
        return False

    # ==================== 輔助方法 ====================

    def _parse_and_fill_location(self, full_location: str, district_name: str) -> None:
        """解析違規地點，將路段自動比對下拉選單，並將 鄰/巷/弄/號 及 補充說明 分別填入專屬欄位"""
        clean_loc = full_location.replace("新竹市", "").replace(district_name, "").replace(district_name.replace("區", ""), "").strip()
        
        # 1. 動態配對路段選單 (#case_area_code)
        options = self.page.locator(f"{self.config['selectors']['area_code']} option").all()
        matched_road_val = None
        matched_road_text = ""
        
        # 尋找匹配度最高的路段
        for opt in options:
            txt = opt.evaluate("el => el.innerText").strip()
            val = opt.evaluate("el => el.value")
            if val and txt in clean_loc:
                if len(txt) > len(matched_road_text):  # 避免 "光復路" 與 "光復路一段" 搞混，取字數最長者
                    matched_road_val = val
                    matched_road_text = txt
                    
        if matched_road_val:
            self.page.select_option(self.config["selectors"]["area_code"], value=matched_road_val)
            print(f"    - 路段選單已配對: '{matched_road_text}' (Value: {matched_road_val})")
            clean_loc = clean_loc.replace(matched_road_text, "").strip()
        else:
            print("    - 未能在下拉選單中找到匹配路段，保持詳細文字填寫。")

        # 2. 提取 段, 巷, 弄, 號, 樓
        section_m = re.search(r"(\d+)段", clean_loc)
        lane_m = re.search(r"(\d+)巷", clean_loc)
        alley_m = re.search(r"(\d+)弄", clean_loc)
        num_m = re.search(r"(\d+)號", clean_loc)
        floor_m = re.search(r"(\d+)(?:樓|F|f)", clean_loc)
        
        sec_val = section_m.group(1) if section_m else ""
        lane_val = lane_m.group(1) if lane_m else ""
        alley_val = alley_m.group(1) if alley_m else ""
        num_val = num_m.group(1) if num_m else ""
        floor_val = floor_m.group(1) if floor_m else ""
        
        if section_m: clean_loc = clean_loc.replace(section_m.group(0), "")
        if lane_m: clean_loc = clean_loc.replace(lane_m.group(0), "")
        if alley_m: clean_loc = clean_loc.replace(alley_m.group(0), "")
        if num_m: clean_loc = clean_loc.replace(num_m.group(0), "")
        if floor_m: clean_loc = clean_loc.replace(floor_m.group(0), "")
        
        detail_val = clean_loc.strip()
        if not detail_val:
            detail_val = "前"
            
        print(f"    - 地點細項拆分 -> 段: '{sec_val}', 巷: '{lane_val}', 弄: '{alley_val}', 號: '{num_val}', 樓: '{floor_val}', 補充說明: '{detail_val}'")
        
        if sec_val: self.page.fill(self.config["selectors"]["addr1"], sec_val)
        if lane_val: self.page.fill(self.config["selectors"]["addr2"], lane_val)
        if alley_val: self.page.fill(self.config["selectors"]["addr3"], alley_val)
        if num_val: self.page.fill(self.config["selectors"]["addr4"], num_val)
        if floor_val: self.page.fill(self.config["selectors"]["addr5"], floor_val)
        self.page.fill(self.config["selectors"]["addr_detail"], detail_val)

    def _format_date(self, date_str: str) -> str:
        """將日期格式化為 YYYY-MM-DD"""
        date_str = date_str.replace("/", "-").replace(".", "-").strip()
        if re.match(r"^\d{8}$", date_str):
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", date_str)
        if match:
            y, m, d = match.groups()
            return f"{y}-{int(m):02d}-{int(d):02d}"
        return date_str

    def _parse_hour_minute(self, time_str: str) -> tuple:
        """解析時間字串，傳回 (hour_str, minute_str) 補零兩位字串 (00..23, 00..59)"""
        time_str = time_str.strip()
        h, m = 0, 0
        if ":" in time_str:
            parts = time_str.split(":")
            h, m = int(parts[0]), int(parts[1])
        elif len(time_str) >= 4 and time_str.isdigit():
            h, m = int(time_str[:2]), int(time_str[2:4])
        return f"{h:02d}", f"{m:02d}"

    def _split_license_plate(self, plate: str) -> tuple:
        """將車牌號碼以 '-' 拆分為 (前碼, 後碼)"""
        plate = plate.strip().upper()
        if "-" in plate:
            parts = plate.split("-", 1)
            return parts[0], parts[1]
        elif len(plate) >= 6:
            return plate[:3], plate[3:]
        return plate, ""
