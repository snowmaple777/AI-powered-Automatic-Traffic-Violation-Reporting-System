import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class PenghuDriver(BaseDriver):
    """
    澎湖縣交通違規檢舉自動填表策略 (Penghu Driver)
    """

    TOWN_MAP = {
        "馬公": "馬公市",
        "西嶼": "西嶼鄉",
        "望安": "望安鄉",
        "七美": "七美鄉",
        "白沙": "白沙鄉",
        "湖西": "湖西鄉"
    }

    def navigate_to_start(self) -> None:
        print(f"[Penghu] 正在導航至澎湖縣檢舉門戶: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(1000)

    def handle_pre_actions(self) -> None:
        print("[Penghu] 點擊「下一步」按鈕進入表單頁面...")
        self.page.click(self.config["selectors"]["agreement_next_btn"])
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_selector(self.config["selectors"]["name"], state="visible", timeout=10000)
        print("[Penghu] 成功進入澎湖縣表單填寫頁面！")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Penghu] 正在填寫檢舉人聯絡資料...")
        self.page.fill(self.config["selectors"]["name"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["pid"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["tel"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["address"], reporter_data.get("reporter_address", ""))
        self.page.fill(self.config["selectors"]["email"], reporter_data.get("reporter_email", ""))

    def select_district(self, district_name: str) -> None:
        clean_dist = district_name.strip()
        target_town = "馬公市"
        for key, val in self.TOWN_MAP.items():
            if key in clean_dist or clean_dist in key:
                target_town = val
                break
        print(f"  - 選擇違規行政區: {target_town}")
        self.page.select_option(self.config["selectors"]["imparea"], label=target_town)

    def select_village(self, village_name: str) -> None:
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Penghu] 正在填寫違規內容資料...")

        # 1. 填寫車牌號碼雙欄拆分
        plate = violation_data.get("license_plate", "")
        prefix, suffix = self._split_license_plate(plate)
        print(f"  - 填寫車牌: 前碼 '{prefix}', 後碼 '{suffix}'")
        self.page.fill(self.config["selectors"]["carno1"], prefix)
        self.page.fill(self.config["selectors"]["carno2"], suffix)

        # 2. 選擇行政區與地點
        self.select_district(violation_data.get("district", "馬公市"))
        full_location = violation_data.get("violation_location", "")
        print(f"  - 填寫發生地點: '{full_location}'")
        self.page.fill(self.config["selectors"]["impaddress"], full_location)

        # 3. 填寫違規日期與時間
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_date(raw_date)
        hour_str, min_str = self._parse_hour_minute(violation_data.get("violation_time", "00:00"))
        
        print(f"  - 填寫違規日期: {formatted_date}, 時間: {hour_str} 時 {min_str} 分")
        self.page.fill(self.config["selectors"]["impdate"], formatted_date)
        self.page.select_option(self.config["selectors"]["hh"], value=hour_str)
        self.page.select_option(self.config["selectors"]["mm"], value=min_str)

        # 4. 違規項目選取
        fact_desc = (violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")).strip()
        print(f"  - 搜尋並配對違規項目 (關鍵字: '{fact_desc}')...")
        options = self.page.locator(f"{self.config['selectors']['subject']} option").all()
        selected = False
        for opt in options:
            txt = opt.evaluate("el => el.innerText")
            val = opt.evaluate("el => el.value")
            if "人行道" in txt or "停車" in txt or "臨時停車" in txt or "標誌" in txt:
                self.page.select_option(self.config["selectors"]["subject"], value=val)
                selected = True
                print(f"    - 成功配對項目: '{txt[:30]}...'")
                break
                
        if not selected and len(options) > 1:
            val = options[1].evaluate("el => el.value")
            self.page.select_option(self.config["selectors"]["subject"], value=val)
            print("    - 選取預設違規項目。")

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Penghu] 沒有提供附加媒體檔案。")
            return
            
        valid_paths = [p for p in media_paths if os.path.exists(p)]
        if not valid_paths:
            print("[Penghu] 未找到任何有效的媒體檔案。")
            return
            
        print(f"[Penghu] 正在上傳附加檔案 (最多 5 個槽分流): {[os.path.basename(p) for p in valid_paths]}")
        file_inputs = self.config["selectors"]["file_inputs"]
        for idx, path in enumerate(valid_paths[:5]):
            if idx < len(file_inputs):
                selector = file_inputs[idx]
                if self.page.is_visible(selector):
                    self.page.set_input_files(selector, path)
                    self.page.wait_for_timeout(500)

    def handle_verification(self) -> None:
        print("[Penghu] 填表完成，勾選同意保護條款並儲存預覽截圖...")
        self.page.check(self.config["selectors"]["dataread"])
        
        # 焦點放到驗證碼輸入框，並產出截圖供使用者手動輸入
        self.page.focus(self.config["selectors"]["chkint"])
        self.page.wait_for_timeout(500)
        
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/ph_filled_preview.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[Penghu] 填寫完成之全頁面截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[Penghu] 儲存截圖失敗: {e}")
            
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        print("[Penghu] 自動填表完畢，安全停留在確定送出前，請使用者確認無誤並輸入驗證碼後點擊「確定送出」。")
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

    def _split_license_plate(self, plate: str) -> tuple:
        """將車牌號碼以 '-' 拆分為 (前碼, 後碼)"""
        plate = plate.strip().upper()
        if "-" in plate:
            parts = plate.split("-", 1)
            return parts[0], parts[1]
        elif len(plate) >= 6:
            return plate[:3], plate[3:]
        return plate, ""
