import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class TaitungDriver(BaseDriver):
    """
    台東縣交通違規檢舉自動填表策略 (Taitung Driver)
    """

    DISTRICT_MAP = {
        "台東市": "臺東市",
        "臺東市": "臺東市",
        "綠島鄉": "綠島鄉",
        "綠島": "綠島鄉",
        "蘭嶼鄉": "蘭嶼鄉",
        "蘭嶼": "蘭嶼鄉",
        "延平鄉": "延平鄉",
        "延平": "延平鄉",
        "卑南鄉": "卑南鄉",
        "卑南": "卑南鄉",
        "鹿野鄉": "鹿野鄉",
        "鹿野": "鹿野鄉",
        "關山鎮": "關山鎮",
        "關山": "關山鎮",
        "海端鄉": "海端鄉",
        "海端": "海端鄉",
        "池上鄉": "池上鄉",
        "池上": "池上鄉",
        "東河鄉": "東河鄉",
        "東河": "東河鄉",
        "成功鎮": "成功鎮",
        "成功": "成功鎮",
        "長濱鄉": "長濱鄉",
        "長濱": "長濱鄉",
        "太麻里鄉": "太麻里鄉",
        "太麻里": "太麻里鄉",
        "金峰鄉": "金峰鄉",
        "金峰": "金峰鄉",
        "大武鄉": "大武鄉",
        "大武": "大武鄉",
        "達仁鄉": "達仁鄉",
        "達仁": "達仁鄉"
    }

    def navigate_to_start(self) -> None:
        print(f"[Taitung] 正在導航至台東縣檢舉條款頁面: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(2000)

    def handle_pre_actions(self) -> None:
        print("[Taitung] 處理免責宣告條款，進入表單頁面...")
        form_url = self.config.get("form_url", "https://www.ttcpb.gov.tw/chinese/home.jsp?serno=201108090002&&contlink=ap/mail1_1.jsp")
        self.page.goto(form_url, wait_until="domcontentloaded")
        self.page.wait_for_selector(self.config["selectors"]["reporter_name"], state="visible", timeout=10000)
        print("[Taitung] 成功進入台東縣表單頁面！")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Taitung] 正在填寫檢舉人聯絡資料...")
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))

    def select_district(self, district_name: str) -> None:
        clean_dist = district_name.replace("台東縣", "").replace("臺東縣", "").strip()
        target_label = "臺東市"
        for key, label in self.DISTRICT_MAP.items():
            if key in clean_dist or clean_dist in key:
                target_label = label
                break
                
        print(f"  - 選擇行政區: '{clean_dist}' (比對選單標籤: {target_label})")
        self.page.select_option(self.config["selectors"]["location_district"], label=target_label)

    def select_village(self, village_name: str) -> None:
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Taitung] 正在填寫違規案發詳細資料...")
        
        # 1. 違規車牌 (car)
        plate = violation_data.get("license_plate", "")
        print(f"  - 填寫違規車牌: {plate}")
        self.page.fill(self.config["selectors"]["license_plate"], plate)
        
        # 2. 行政區選取 (area)
        self.select_district(violation_data.get("district", "臺東市"))
        
        # 3. 詳細地點 (oad)
        full_location = violation_data.get("violation_location", "")
        print(f"  - 填寫詳細地點: '{full_location}'")
        self.page.fill(self.config["selectors"]["location_detail"], full_location)
        
        # 4. 違規日期 (odate_view: YYYY-MM-DD)
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_date(raw_date)
        print(f"  - 填寫違規日期: {formatted_date}")
        self.page.fill(self.config["selectors"]["violation_date"], formatted_date)
        
        # 5. 違規時間 (時 hh / 分 mm)
        raw_time = violation_data.get("violation_time", "00:00")
        hour_str, min_str = self._parse_hour_minute(raw_time)
        print(f"  - 選擇違規時間: {hour_str} 時 {min_str} 分")
        self.page.select_option(self.config["selectors"]["violation_hour"], value=hour_str)
        self.page.select_option(self.config["selectors"]["violation_minute"], value=min_str)
        
        # 6. 違規事項下拉選單 (subject)
        fact_desc = (violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")).strip()
        print(f"  - 搜尋並配對違規事項關鍵字 (依據敘述: '{fact_desc}')...")
        options = self.page.locator(f"{self.config['selectors']['violation_fact_select']} option").all()
        selected = False
        for opt in options:
            txt = opt.evaluate("el => el.innerText")
            val = opt.evaluate("el => el.value")
            if "人行道" in txt or "56條" in fact_desc:
                if "在人行道" in txt or "人行道" in txt:
                    self.page.select_option(self.config["selectors"]["violation_fact_select"], value=val)
                    selected = True
                    print(f"    - 成功選取違規事項: '{txt[:30]}...'")
                    break
                    
        if not selected and len(options) > 1:
            val = options[1].evaluate("el => el.value")
            self.page.select_option(self.config["selectors"]["violation_fact_select"], value=val)
            print("    - 選取預設違規事項選單項。")
            
        # 7. 補充事實描述 (subjectother，僅在選擇「其他」而顯示時才填寫)
        remark_selector = self.config["selectors"]["violation_remark"]
        try:
            if self.page.is_visible(remark_selector, timeout=1000):
                self.page.fill(remark_selector, fact_desc)
        except Exception:
            pass
        
        # 8. 詳細內容 (content1)
        full_desc = violation_data.get("violation_description", "")
        self.page.fill(self.config["selectors"]["violation_content"], full_desc)

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Taitung] 沒有提供附加媒體檔案。")
            return
            
        valid_paths = [p for p in media_paths if os.path.exists(p)][:3]
        file_inputs = self.config["selectors"]["file_inputs"]
        
        print(f"[Taitung] 正在上傳附加檔案 (最多 3 個槽分流): {valid_paths}")
        for idx, path in enumerate(valid_paths):
            if idx < len(file_inputs):
                selector = file_inputs[idx]
                print(f"  - 檔案槽 [{idx+1}] 上傳: '{os.path.basename(path)}'")
                self.page.set_input_files(selector, path)
                self.page.wait_for_timeout(500)

    def handle_verification(self) -> None:
        print("[Taitung] 提示使用者：請手動輸入圖形驗證碼（chkint）填入後，點擊「確定送出」按鈕。")
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/tt_filled_preview.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[Taitung] 填寫完成之全頁面截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[Taitung] 儲存截圖失敗: {e}")
            
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        print("[Taitung] 自動填表完畢，安全停留在送出表單前，請使用者確認無誤後手動提交。")
        return False

    # ==================== 輔助方法 ====================

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
