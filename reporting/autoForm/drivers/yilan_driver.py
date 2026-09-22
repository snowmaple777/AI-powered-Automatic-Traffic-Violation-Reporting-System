import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class YilanDriver(BaseDriver):
    """
    宜蘭縣交通違規檢舉自動填表策略 (Yilan Driver)
    """

    DISTRICT_MAP = {
        "宜蘭市": "260",
        "宜蘭": "260",
        "頭城鎮": "261",
        "頭城": "261",
        "礁溪鄉": "262",
        "礁溪": "262",
        "壯圍鄉": "263",
        "壯圍": "263",
        "員山鄉": "264",
        "員山": "264",
        "羅東鎮": "265",
        "羅東": "265",
        "三星鄉": "266",
        "三星": "266",
        "大同鄉": "267",
        "大同": "267",
        "五結鄉": "268",
        "五結": "268",
        "冬山鄉": "269",
        "冬山": "269",
        "蘇澳鎮": "270",
        "蘇澳": "270",
        "南澳鄉": "272",
        "南澳": "272"
    }

    FACT_MAP = [
        ("56-1-1", "102"),
        ("56101", "102"),
        ("56條", "102"),
        ("人行道停車", "102"),
        ("55101", "91"),
        ("55條", "91"),
        ("臨時停車", "91"),
        ("562", "93"),
        ("並排停車", "93"),
        ("併排停車", "93"),
        ("53", "86"),
        ("闖紅燈", "86"),
        ("42", "66"),
        ("方向燈", "66"),
        ("60203", "88"),
        ("標誌", "88"),
        ("標線", "88")
    ]

    def navigate_to_start(self) -> None:
        print(f"[Yilan] 正在導航至宜蘭縣首頁: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(2000)

    def handle_pre_actions(self) -> None:
        # 宜蘭縣為單頁式表單，無需前置同意跳轉
        pass

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Yilan] 正在填寫檢舉人聯絡資料...")
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())

    def select_district(self, district_name: str) -> None:
        if not district_name:
            return
        clean_dist = district_name.replace("宜蘭縣", "").strip()
        dist_code = None
        for name_key, code_val in self.DISTRICT_MAP.items():
            if name_key in clean_dist or clean_dist in name_key:
                dist_code = code_val
                break
                
        print(f"  - 選擇行政區: '{clean_dist}' (選單代碼: {dist_code})")
        if dist_code:
            self.page.select_option(self.config["selectors"]["location_district"], value=dist_code)
            self.page.wait_for_timeout(1500)
        else:
            print(f"[Yilan 警告] 無法配對行政區代碼 '{clean_dist}'，嘗試以標籤選取...")
            try:
                self.page.select_option(self.config["selectors"]["location_district"], label=clean_dist)
            except Exception as e:
                print(f"[Yilan 錯誤] 行政區選取失敗: {e}")

    def select_village(self, village_name: str) -> None:
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Yilan] 正在填寫違規案發詳細資料...")
        
        # 1. 違規日期與時間 (解鎖 readonly 後填入 YYYY/MM/DD HH:MM)
        raw_date = violation_data.get("violation_date", "")
        raw_time = violation_data.get("violation_time", "00:00")
        formatted_datetime = self._format_datetime_str(raw_date, raw_time)
        
        print(f"  - 解鎖 readonly 並寫入違規日期時間: '{formatted_datetime}'")
        self.page.evaluate("() => document.getElementById('datepicker1').removeAttribute('readonly')")
        self.page.fill(self.config["selectors"]["violation_date"], formatted_datetime)
        
        # 2. 行政區選取 (cakeslt1)
        self.select_district(violation_data.get("district", ""))
        
        # 3. 街道路名選取 (cakeslt2 模糊匹配)
        full_location = violation_data.get("violation_location", "")
        print(f"  - 正在解析並選取街道路名 (完整地點: '{full_location}')...")
        street_select = self.page.locator(self.config["selectors"]["location_street"])
        street_opts = street_select.evaluate("""el => {
            return Array.from(el.options).map(o => ({text: o.text.trim(), value: o.value}));
        }""")
        
        target_street_val = None
        target_street_text = ""
        for opt in street_opts:
            st_name = opt["text"].strip()
            if st_name and len(st_name) >= 2 and st_name in full_location:
                target_street_val = opt["value"]
                target_street_text = st_name
                break
                
        if target_street_val:
            street_select.select_option(value=target_street_val)
            print(f"    - 已選取匹配街道路名: '{target_street_text}'")
        else:
            print("    - 未能從地點中自動比對出精確街道路名，保留選單預設選項。")
            
        # 4. 詳細地點
        self.page.fill(self.config["selectors"]["location_detail"], full_location)
        
        # 5. 車牌號碼雙欄拆分 (carcode 前碼 / carcode12 後碼)
        plate = violation_data.get("license_plate", "")
        prefix, suffix = self._split_license_plate(plate)
        print(f"  - 填寫拆分車牌: 前碼 '{prefix}', 後碼 '{suffix}'")
        self.page.fill(self.config["selectors"]["license_plate_prefix"], prefix)
        self.page.fill(self.config["selectors"]["license_plate_suffix"], suffix)
        
        # 6. 違規事實法條選單 (legislation44)
        fact_desc = (violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")).strip()
        target_fact_val = "102" # 預設人行道停車
        for pattern, fact_val in self.FACT_MAP:
            if pattern in fact_desc or pattern in violation_data.get("violation_category", ""):
                target_fact_val = fact_val
                break
                
        print(f"  - 選擇違規事實法條 (legislation44): {target_fact_val}")
        self.page.select_option(self.config["selectors"]["violation_fact_select"], value=target_fact_val)
        
        # 7. 補檔註記說明 (content22)
        desc_text = violation_data.get("violation_description", "")
        self.page.fill(self.config["selectors"]["violation_fact_desc"], desc_text)

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Yilan] 沒有提供附加媒體檔案。")
            return
            
        valid_paths = [p for p in media_paths if os.path.exists(p)][:4]
        file_inputs = self.config["selectors"]["file_inputs"]
        
        print(f"[Yilan] 正在上傳附加檔案 (最多 4 個槽分流): {valid_paths}")
        for idx, path in enumerate(valid_paths):
            if idx < len(file_inputs):
                selector = file_inputs[idx]
                print(f"  - 檔案槽 [{idx+1}] 上傳: '{os.path.basename(path)}'")
                self.page.set_input_files(selector, path)
                self.page.wait_for_timeout(500)

    def handle_verification(self) -> None:
        print("[Yilan] 提示使用者：請手動輸入 4 碼圖片驗證碼並確認點擊「確認送出」按鈕。")
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/il_filled_preview.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[Yilan] 填寫完成之全頁面截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[Yilan] 儲存截圖失敗: {e}")
            
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        print("[Yilan] 自動填表完畢，安全停留在送出表單前，請使用者確認無誤後手動提交。")
        return False

    # ==================== 輔助方法 ====================

    def _format_datetime_str(self, date_str: str, time_str: str) -> str:
        """將日期與時間格式化為 YYYY/MM/DD HH:MM"""
        date_str = date_str.replace("-", "/").replace(".", "/").strip()
        if re.match(r"^\d{8}$", date_str):
            date_str = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
        match_d = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", date_str)
        if match_d:
            y, m, d = match_d.groups()
            date_str = f"{y}/{int(m):02d}/{int(d):02d}"
            
        time_str = time_str.strip()
        h, m = 0, 0
        if ":" in time_str:
            parts = time_str.split(":")
            h, m = int(parts[0]), int(parts[1])
        elif len(time_str) >= 4 and time_str.isdigit():
            h, m = int(time_str[:2]), int(time_str[2:4])
        time_formatted = f"{h:02d}:{m:02d}"
        
        return f"{date_str} {time_formatted}"

    def _split_license_plate(self, plate: str) -> tuple:
        """將車牌號碼以 '-' 拆分為 (前碼, 後碼)"""
        plate = plate.strip().upper()
        if "-" in plate:
            parts = plate.split("-", 1)
            return parts[0], parts[1]
        elif len(plate) >= 6:
            return plate[:3], plate[3:]
        return plate, ""
