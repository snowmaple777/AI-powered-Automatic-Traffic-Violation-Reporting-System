import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class NantouDriver(BaseDriver):
    """
    南投縣交通違規檢舉自動填表策略 (Nantou Driver)
    """

    DISTRICT_MAP = {
        "南投市": "540",
        "南投": "540",
        "中興新村": "5401",
        "中寮鄉": "541",
        "中寮": "541",
        "草屯鎮": "542",
        "草屯": "542",
        "國姓鄉": "544",
        "國姓": "544",
        "埔里鎮": "545",
        "埔里": "545",
        "仁愛鄉": "546",
        "仁愛": "546",
        "名間鄉": "551",
        "名間": "551",
        "集集鎮": "552",
        "集集": "552",
        "水里鄉": "553",
        "水里": "553",
        "魚池鄉": "555",
        "魚池": "555",
        "信義鄉": "556",
        "信義": "556",
        "竹山鎮": "557",
        "竹山": "557",
        "鹿谷鄉": "558",
        "鹿谷": "558"
    }

    FACT_MAP = [
        ("56-1-1", "56101"),
        ("56101", "56101"),
        ("56條", "56101"),
        ("人行道停車", "56101"),
        ("55101", "55101"),
        ("55條", "55101"),
        ("臨時停車", "55101"),
        ("562", "562"),
        ("並排停車", "562"),
        ("併排停車", "562"),
        ("53", "53"),
        ("闖紅燈", "53"),
        ("42", "42"),
        ("方向燈", "42"),
        ("60203", "60203"),
        ("標誌", "60203"),
        ("標線", "60203")
    ]

    def navigate_to_start(self) -> None:
        print(f"[Nantou] 正在導航至南投縣首頁: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(2000)

    def handle_pre_actions(self) -> None:
        print("[Nantou] 執行 Step 1 宣告同意跳轉 (點擊下一步)...")
        self.page.click(self.config["selectors"]["step1_btn"])
        self.page.wait_for_timeout(2000)
        
        print("[Nantou] 執行 Step 2 個資宣告跳轉 (點擊我已閱讀並同意個資事項)...")
        self.page.click(self.config["selectors"]["step2_btn"])
        
        # 等待主要表單頁面載入完成
        self.page.wait_for_selector(self.config["selectors"]["reporter_name"], state="visible", timeout=10000)
        print("[Nantou] 成功進入主要填表頁面 (Step 3)")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Nantou] 正在填寫檢舉人聯絡資料...")
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))

    def select_district(self, district_name: str) -> None:
        if not district_name:
            return
        clean_dist = district_name.replace("南投縣", "").strip()
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
            print(f"[Nantou 警告] 無向對應行政區代碼 '{clean_dist}'，嘗試以標籤選取...")
            try:
                self.page.select_option(self.config["selectors"]["location_district"], label=clean_dist)
            except Exception as e:
                print(f"[Nantou 錯誤] 行政區選取失敗: {e}")

    def select_village(self, village_name: str) -> None:
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Nantou] 正在填寫違規案發詳細資料...")
        
        # 1. 車種 (小客車 / 普通重型機車等)
        car_type_raw = violation_data.get("car_type", "小客車")
        target_class = "小客車"
        if "機車" in car_type_raw:
            target_class = "普通重型機車" if "重" in car_type_raw else "輕型機車"
        elif "貨車" in car_type_raw:
            target_class = "小貨車"
        elif "大客車" in car_type_raw:
            target_class = "大客車"
            
        print(f"  - 選擇車種: {target_class}")
        try:
            self.page.select_option(self.config["selectors"]["car_class"], label=target_class)
        except Exception:
            self.page.select_option(self.config["selectors"]["car_class"], value="小客車")
            
        # 2. 牌照號碼
        plate = violation_data.get("license_plate", "")
        self.page.fill(self.config["selectors"]["license_plate"], plate)
        
        # 3. 行政區選取 (Loc1)
        self.select_district(violation_data.get("district", ""))
        
        # 4. 街道路名選取 (Loc2 模糊匹配)
        full_location = violation_data.get("violation_location", "")
        print(f"  - 正在解析並選取街道路名 (完整地點: '{full_location}')...")
        street_select = self.page.locator(self.config["selectors"]["location_street"])
        street_opts = street_select.evaluate("""el => {
            return Array.from(el.options).map(o => ({text: o.text, value: o.value}));
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
            print("    - 未能從地點中自動比對出精確街道路名，保留選單第一個預設選項。")
            
        # 5. 詳細地點
        self.page.fill(self.config["selectors"]["location_detail"], full_location)
        
        # 6. 違規日期 (txtDate: YYYY/MM/DD)
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_date_slashes(raw_date)
        print(f"  - 填寫違規日期: {formatted_date}")
        self.page.fill(self.config["selectors"]["violation_date"], formatted_date)
        
        # 7. 違規時間 (時 mcarhour / 分 mcarmitu)
        raw_time = violation_data.get("violation_time", "00:00")
        hour_str, min_str = self._parse_hour_minute(raw_time)
        print(f"  - 填寫違規時間: {hour_str} 時 {min_str} 分")
        self.page.select_option(self.config["selectors"]["violation_hour"], value=hour_str)
        self.page.select_option(self.config["selectors"]["violation_minute"], value=min_str)
        
        # 8. 法條樣態 (rlid)
        fact_desc = (violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")).strip()
        target_rlid = "56101" # 預設人行道停車
        for pattern, rlid_val in self.FACT_MAP:
            if pattern in fact_desc or pattern in violation_data.get("violation_category", ""):
                target_rlid = rlid_val
                break
                
        print(f"  - 選擇法條樣態 (rlid): {target_rlid}")
        self.page.select_option(self.config["selectors"]["violation_fact_select"], value=target_rlid)
        self.page.wait_for_timeout(500)
        
        # 9. 違規事實文字敘述 (mcarblack)
        fact_text = violation_data.get("violation_description", "")
        if len(fact_text) < 3:
            fact_text = f"車輛 {plate} 違規停放，違反道路交通管理處罰條例。"
        self.page.fill(self.config["selectors"]["violation_fact_desc"], fact_text)

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Nantou] 沒有提供附加媒體檔案。")
            return
            
        valid_paths = [p for p in media_paths if os.path.exists(p)]
        if not valid_paths:
            print("[Nantou] 未找到任何有效的媒體檔案。")
            return
            
        print(f"[Nantou] 正在上傳附加檔案至 FileUpload1: '{os.path.basename(valid_paths[0])}'")
        self.page.set_input_files(self.config["selectors"]["file_input"], valid_paths[0])

    def handle_verification(self) -> None:
        print("[Nantou] 提示使用者：請手動輸入 4 碼圖片驗證碼並確認點擊「資料上傳」按鈕。")
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/nt_filled_preview.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[Nantou] 填寫完成之全頁面截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[Nantou] 儲存截圖失敗: {e}")
            
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        print("[Nantou] 自動填表完畢，安全停留在送出表單前，請使用者確認無誤後手動提交。")
        return False

    # ==================== 輔助方法 ====================

    def _format_date_slashes(self, date_str: str) -> str:
        """將日期格式化為 YYYY/MM/DD"""
        date_str = date_str.replace("-", "/").replace(".", "/").strip()
        if re.match(r"^\d{8}$", date_str):
            return f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
        match = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", date_str)
        if match:
            y, m, d = match.groups()
            return f"{y}/{int(m):02d}/{int(d):02d}"
        return date_str

    def _parse_hour_minute(self, time_str: str) -> tuple:
        """解析時間字串，傳回 (hour_str, minute_str) 純整數字串 (0..23, 0..59)"""
        time_str = time_str.strip()
        h, m = 0, 0
        if ":" in time_str:
            parts = time_str.split(":")
            h = int(parts[0])
            m = int(parts[1])
        elif len(time_str) >= 4 and time_str.isdigit():
            h = int(time_str[:2])
            m = int(time_str[2:4])
        return str(h), str(m)
