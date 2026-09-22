import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class KeelungDriver(BaseDriver):
    """
    基隆市交通違規檢舉自動填表策略 (Keelung Driver)
    """

    TOWN_MAP = {
        "仁愛": "仁愛區",
        "信義": "信義區",
        "中正": "中正區",
        "中山": "中山區",
        "安樂": "安樂區",
        "暖暖": "暖暖區",
        "七堵": "七堵區"
    }

    def navigate_to_start(self) -> None:
        print(f"[Keelung] 正在導航至基隆市檢舉門戶: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(1000)

    def handle_pre_actions(self) -> None:
        print("[Keelung] 勾選同意宣告條款並進入填表頁面...")
        self.page.check(self.config["selectors"]["agreement_checkbox"])
        self.page.click(self.config["selectors"]["agreement_submit"])
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_selector(self.config["selectors"]["reporter_name"], state="visible", timeout=10000)
        print("[Keelung] 成功進入基隆市表單填寫頁面！")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Keelung] 正在填寫檢舉人個人資料...")
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))
        
        email_val = reporter_data.get("reporter_email", "")
        self.page.fill(self.config["selectors"]["reporter_email"], email_val)
        
        # 點擊「認證信箱」發送認證信至: {email_val}...
        print(f"  - 點擊「認證信箱」發送認證信至: {email_val}...")
        self.page.click(self.config["selectors"]["email_verify_btn"])
        # 等待 3 秒以確保 ASP.NET UpdatePanel 局部重新整理完成
        self.page.wait_for_timeout(3000)

    def select_district(self, district_name: str) -> None:
        clean_dist = district_name.strip()
        target_town = "中正區"
        for key, val in self.TOWN_MAP.items():
            if key in clean_dist or clean_dist in key:
                target_town = val
                break
        print(f"  - 選擇行政區: {target_town}")
        self.page.select_option(self.config["selectors"]["district"], label=target_town)
        self.page.wait_for_timeout(1000)  # 等待路段下拉選單重新載入

    def select_village(self, village_name: str) -> None:
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Keelung] 正在填寫違規案發詳細資料...")

        # 1. 填寫完整車牌與車種類型
        plate = violation_data.get("license_plate", "")
        print(f"  - 填寫車牌號碼: '{plate}'")
        self.page.fill(self.config["selectors"]["license_plate"], plate.upper())
        
        car_type = violation_data.get("car_type", "汽車")
        type_val = "1" # 汽車
        if "大型重機" in car_type:
            type_val = "6"
        elif "重機" in car_type or "重型機車" in car_type:
            type_val = "3"
        elif "輕機" in car_type or "綠牌" in car_type:
            type_val = "4"
        elif "拖車" in car_type:
            type_val = "2"
        print(f"  - 選擇車種代碼: {type_val} ({car_type})")
        self.page.select_option(self.config["selectors"]["car_type"], value=type_val)

        # 2. 違規日期 (移除 readonly 屬性後直接寫入 YYYY-MM-DD)
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_date(raw_date)
        print(f"  - 填寫違規日期: {formatted_date}")
        self.page.evaluate(f"document.querySelector('{self.config['selectors']['violation_date']}').removeAttribute('readonly')")
        self.page.fill(self.config["selectors"]["violation_date"], formatted_date)

        # 3. 違規時間 (小時 / 分鐘下拉選取，去零匹配)
        raw_time = violation_data.get("violation_time", "00:00")
        hour_str, min_str = self._parse_hour_minute(raw_time)
        # 基隆市選單的值是無前導零的整數 (e.g. "9" 而不是 "09")
        h_val = str(int(hour_str))
        m_val = str(int(min_str))
        print(f"  - 選擇違規時間: {h_val} 時 {m_val} 分")
        self.page.select_option(self.config["selectors"]["violation_hour"], value=h_val)
        self.page.select_option(self.config["selectors"]["violation_minute"], value=m_val)

        # 4. 選擇行政區並動態比對路段
        district_name = violation_data.get("district", "中正區")
        self.select_district(district_name)
        
        full_location = violation_data.get("violation_location", "")
        print(f"  - 拆分違規地點細項: '{full_location}'")
        self._parse_and_fill_location(full_location, district_name)

        # 5. 違規項目選取
        fact_desc = (violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")).strip()
        print(f"  - 搜尋並配對違規項目 (關鍵字: '{fact_desc}')...")
        options = self.page.locator(f"{self.config['selectors']['violation_fact_select']} option").all()
        selected = False
        for opt in options:
            txt = opt.evaluate("el => el.innerText")
            val = opt.evaluate("el => el.value")
            if "55" in txt or "56" in txt or "人行道" in txt or "停車" in txt or "臨時停車" in txt:
                self.page.select_option(self.config["selectors"]["violation_fact_select"], value=val)
                selected = True
                print(f"    - 成功配對違規項目: '{txt[:30]}...'")
                break
                
        if not selected and len(options) > 1:
            val = options[1].evaluate("el => el.value")
            self.page.select_option(self.config["selectors"]["violation_fact_select"], value=val)
            print("    - 選取預設違規項目。")

        # 6. 備註描述填寫
        desc_text = violation_data.get("violation_description", "")
        self.page.fill(self.config["selectors"]["violation_remark"], desc_text)

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Keelung] 沒有提供附加媒體檔案。")
            return
            
        valid_paths = [p for p in media_paths if os.path.exists(p)]
        if not valid_paths:
            print("[Keelung] 未找到任何有效的媒體檔案。")
            return
            
        print(f"[Keelung] 正在上傳附加檔案 (最多 4 個槽分流): {[os.path.basename(p) for p in valid_paths]}")
        file_inputs = self.config["selectors"]["file_inputs"]
        for idx, path in enumerate(valid_paths[:4]):
            if idx < len(file_inputs):
                selector = file_inputs[idx]
                if self.page.is_visible(selector):
                    self.page.set_input_files(selector, path)
                    self.page.wait_for_timeout(500)

    def handle_verification(self) -> None:
        print("[Keelung] 填表完成，引導使用者手動輸入認證碼...")
        # 焦點放到認證碼輸入框，並產出截圖供使用者手動輸入
        self.page.focus(self.config["selectors"]["chkcode"])
        self.page.wait_for_timeout(500)
        
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/kl_filled_preview.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[Keelung] 填寫完成之全頁面截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[Keelung] 儲存截圖失敗: {e}")
            
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        print("[Keelung] 自動填表完畢，安全停留在提交前，請使用者輸入認證碼後手動點擊「上傳」提交表單。")
        return False

    # ==================== 輔助方法 ====================

    def _parse_and_fill_location(self, full_location: str, district_name: str) -> None:
        """解析違規地點，將路段自動比對下拉選單，並將 鄰/巷/弄/號/樓 及 補充說明 分別填入專屬欄位"""
        clean_loc = full_location.replace("基隆市", "").replace(district_name, "").strip()
        
        # 1. 比對路段選單 (#OccurAddr1_5)
        options = self.page.locator(f"{self.config['selectors']['road']} option").all()
        matched_road_val = None
        matched_road_text = ""
        
        for opt in options:
            txt = opt.evaluate("el => el.innerText").strip()
            val = opt.evaluate("el => el.value")
            if val and txt in clean_loc:
                if len(txt) > len(matched_road_text):  # 取長度最長者避免混淆
                    matched_road_val = val
                    matched_road_text = txt
                    
        if matched_road_val:
            self.page.select_option(self.config["selectors"]["road"], value=matched_road_val)
            print(f"    - 路段選單已配對: '{matched_road_text}' (Value: {matched_road_val})")
            clean_loc = clean_loc.replace(matched_road_text, "").strip()
        else:
            print("    - 未能在下拉選單中找到匹配路段，保持詳細文字填寫。")

        # 2. 提取 鄰, 巷, 弄, 之弄, 號, 樓/之號
        lin_m = re.search(r"(\d+)鄰", clean_loc)
        lane_m = re.search(r"(\d+)巷", clean_loc)
        alley_m = re.search(r"(\d+)弄", clean_loc)
        suballey_m = re.search(r"之(\d+)弄", clean_loc)
        num_m = re.search(r"(\d+)號", clean_loc)
        floor_m = re.search(r"(?:之|樓|F|f)(\d+)(?:樓|F|f|之號)?", clean_loc)
        
        lin_val = lin_m.group(1) if lin_m else ""
        lane_val = lane_m.group(1) if lane_m else ""
        alley_val = alley_m.group(1) if alley_m else ""
        suballey_val = suballey_m.group(1) if suballey_m else ""
        num_val = num_m.group(1) if num_m else ""
        floor_val = floor_m.group(1) if floor_m else ""
        
        if lin_m: clean_loc = clean_loc.replace(lin_m.group(0), "")
        if lane_m: clean_loc = clean_loc.replace(lane_m.group(0), "")
        if alley_m: clean_loc = clean_loc.replace(alley_m.group(0), "")
        if suballey_m: clean_loc = clean_loc.replace(suballey_m.group(0), "")
        if num_m: clean_loc = clean_loc.replace(num_m.group(0), "")
        if floor_m: clean_loc = clean_loc.replace(floor_m.group(0), "")
        
        other_val = clean_loc.strip()
        if not other_val:
            other_val = "前"
            
        print(f"    - 地點細項拆分 -> 鄰: '{lin_val}', 巷: '{lane_val}', 弄: '{alley_val}', 之弄: '{suballey_val}', 號: '{num_val}', 之號/樓: '{floor_val}', 說明: '{other_val}'")
        
        if lin_val: self.page.fill(self.config["selectors"]["addr_neighborhood"], lin_val)
        if lane_val: self.page.fill(self.config["selectors"]["addr_lane"], lane_val)
        if alley_val: self.page.fill(self.config["selectors"]["addr_alley"], alley_val)
        if suballey_val: self.page.fill(self.config["selectors"]["addr_suballey"], suballey_val)
        if num_val: self.page.fill(self.config["selectors"]["addr_number"], num_val)
        if floor_val: self.page.fill(self.config["selectors"]["addr_subnumber"], floor_val)
        self.page.fill(self.config["selectors"]["addr_cross1"], other_val)

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
