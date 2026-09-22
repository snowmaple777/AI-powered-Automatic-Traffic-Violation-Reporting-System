import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class HualienDriver(BaseDriver):
    """
    花蓮縣交通違規檢舉自動填表策略 (Hualien Driver)
    """

    FACT_MAP = [
        ("56-1-1", "人行道(非騎樓)停車"),
        ("56101", "人行道(非騎樓)停車"),
        ("56條", "人行道(非騎樓)停車"),
        ("人行道停車", "人行道(非騎樓)停車"),
        ("55101", "人行道(非騎樓)臨時停車"),
        ("55條", "人行道(非騎樓)臨時停車"),
        ("臨時停車", "人行道(非騎樓)臨時停車"),
        ("562", "併排停車"),
        ("並排停車", "併排停車"),
        ("併排停車", "併排停車"),
        ("53", "闖紅燈"),
        ("闖紅燈", "闖紅燈"),
        ("42", "方向燈"),
        ("方向燈", "方向燈"),
        ("60203", "標誌、標線"),
        ("標誌", "標誌、標線"),
        ("標線", "標誌、標線")
    ]

    def navigate_to_start(self) -> None:
        print(f"[Hualien] 正在導航至花蓮縣檢舉門戶: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(2000)

    def handle_pre_actions(self) -> None:
        # 從案件資料中判定行政區
        district_name = self.input_data.get("district", "花蓮市")
        clean_dist = district_name.replace("花蓮縣", "").strip()
        
        type_id = "108" # 預設花蓮市
        for key, tid in self.config.get("town_typeids", {}).items():
            if key in clean_dist or clean_dist in key:
                type_id = tid
                break
                
        print(f"[Hualien] 依據行政區 '{clean_dist}' 進入對應子表單 (typeid: {type_id})...")
        target_url = f"http://hlpb.twgov.mobi/order/iframviolation_list.php?dir=order2&menu=&typeid=2936&violationtype_id={type_id}"
        self.page.goto(target_url, wait_until="domcontentloaded")
        self.page.wait_for_selector(self.config["selectors"]["reporter_name"], state="visible", timeout=10000)
        print("[Hualien] 成功進入花蓮縣表單填寫頁面！")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Hualien] 正在填寫檢舉人聯絡資料...")
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))

    def select_district(self, district_name: str) -> None:
        pass

    def select_village(self, village_name: str) -> None:
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Hualien] 正在填寫違規案發詳細資料...")
        
        # 1. 車牌號碼雙欄拆分 (subject 前碼 / subject6 後碼)
        plate = violation_data.get("license_plate", "")
        prefix, suffix = self._split_license_plate(plate)
        print(f"  - 填寫拆分車牌: 前碼 '{prefix}', 後碼 '{suffix}'")
        self.page.fill(self.config["selectors"]["license_plate_prefix"], prefix)
        self.page.fill(self.config["selectors"]["license_plate_suffix"], suffix)
        
        # 2. 違規日期 (datet: YYYY-MM-DD)
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_date(raw_date)
        print(f"  - 填寫違規日期: {formatted_date}")
        self.page.fill(self.config["selectors"]["violation_date"], formatted_date)
        
        # 3. 違規時間 (時 timet / 分 tmi)
        raw_time = violation_data.get("violation_time", "00:00")
        hour_str, min_str = self._parse_hour_minute(raw_time)
        print(f"  - 選擇違規時間: {hour_str} 時 {min_str} 分")
        self.page.select_option(self.config["selectors"]["violation_hour"], value=hour_str)
        self.page.select_option(self.config["selectors"]["violation_minute"], value=min_str)
        
        # 4. 違規地點 (content)
        full_location = violation_data.get("violation_location", "")
        print(f"  - 填寫違規地點: '{full_location}'")
        self.page.fill(self.config["selectors"]["location_detail"], full_location)
        
        # 5. 違規法條及事實 (20191113100106 Radio 按鈕)
        fact_desc = (violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")).strip()
        target_keyword = "人行道(非騎樓)停車" # 預設
        for pattern, kw in self.FACT_MAP:
            if pattern in fact_desc or pattern in violation_data.get("violation_category", ""):
                target_keyword = kw
                break
                
        print(f"  - 搜尋並勾選違規事實 Radio 關鍵字: '{target_keyword}'...")
        radios = self.page.locator(self.config["selectors"]["violation_fact_radio"]).all()
        selected = False
        for radio in radios:
            val = radio.evaluate("el => el.value")
            if target_keyword in val:
                radio.check(force=True)
                selected = True
                print(f"    - 成功勾選匹配選項: '{val[:30]}...'")
                break
                
        if not selected and radios:
            radios[0].check(force=True)
            print("    - 未能精確比對，選取第一個 Radio 選項。")
            
        # 6. 其他違規事實敘述 (20221205203006)
        desc_text = violation_data.get("violation_description", "")
        self.page.fill(self.config["selectors"]["violation_fact_remark"], desc_text)

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Hualien] 沒有提供附加媒體檔案。")
            return
            
        valid_paths = [p for p in media_paths if os.path.exists(p)][:4]
        file_inputs = self.config["selectors"]["file_inputs"]
        
        print(f"[Hualien] 正在上傳附加檔案 (最多 4 個槽分流): {valid_paths}")
        for idx, path in enumerate(valid_paths):
            if idx < len(file_inputs):
                selector = file_inputs[idx]
                print(f"  - 檔案槽 [{idx+1}] 上傳: '{os.path.basename(path)}'")
                self.page.set_input_files(selector, path)
                self.page.wait_for_timeout(500)

    def handle_verification(self) -> None:
        print("[Hualien] 提示使用者：請手動計算圖形數學驗證碼（如 4+4=?）填入後，點擊「確認送出」按鈕。")
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/hl_filled_preview.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[Hualien] 填寫完成之全頁面截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[Hualien] 儲存截圖失敗: {e}")
            
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        print("[Hualien] 自動填表完畢，安全停留在送出表單前，請使用者確認無誤後手動提交。")
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
