import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class NewTaipeiDriver(BaseDriver):
    """
    新北市交通違規檢舉自動填表策略 (New Taipei Driver)
    """

    def navigate_to_start(self) -> None:
        print(f"[NewTaipei] 正在導航至新北市檢舉首頁: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(1000)

    def handle_pre_actions(self) -> None:
        print("[NewTaipei] 勾選同意宣告並點擊下一步進入表單...")
        self.page.check(self.config["selectors"]["agreement_checkbox"])
        self.page.click(self.config["selectors"]["agreement_submit"])
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_selector(self.config["selectors"]["violation_type"], state="visible", timeout=10000)
        print("[NewTaipei] 成功進入填表頁面！")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[NewTaipei] 正在填寫檢舉人資料...")
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        self.page.check(self.config["selectors"]["foreigner_radio_taiwan"])
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))
        
        # 註冊 Dialog 處理器，因為發送驗證信會彈出 alert
        def handle_dialog(d):
            print(f"[Dialog 訊息] {d.message}")
            d.dismiss()
        self.page.on("dialog", handle_dialog)
        
        print("[NewTaipei] 點擊「認證信箱」發送驗證信...")
        self.page.click(self.config["selectors"]["send_email_btn"])
        self.page.wait_for_timeout(1500)

    def select_district(self, district_name: str) -> None:
        print("[NewTaipei] 正在選取行政區與對接街道...")
        
        # 1. 選擇是否為新北市
        self.page.select_option(self.config["selectors"]["is_city_area"], label="新北市")
        self.page.wait_for_timeout(500)
        
        # 2. 去尾行政區並選取
        clean_dist = district_name.strip()
        if not clean_dist.endswith("區"):
            clean_dist += "區"
            
        print(f"  - 選擇行政區: '{clean_dist}'")
        self.page.select_option(self.config["selectors"]["district"], label=clean_dist)
        self.page.wait_for_timeout(1000)  # 等待街道 AJAX 載入

    def select_village(self, village_name: str) -> None:
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[NewTaipei] 正在填寫違規內容資訊...")

        # 1. 選擇違規類型 (汽車/機車)
        car_type = violation_data.get("car_type", "汽車")
        target_type = "汽車"
        if "機車" in car_type or "機" in car_type:
            target_type = "機車"
        print(f"  - 選擇違規車種類型: {target_type}")
        self.page.select_option(self.config["selectors"]["violation_type"], label=target_type)
        self.page.wait_for_timeout(500)

        # 2. 選擇車牌種類為 一般車牌 (1)
        self.page.select_option(self.config["selectors"]["plate_type"], value="1")
        
        # 3. 填寫車牌前後欄
        plate = violation_data.get("license_plate", "")
        prefix, suffix = self._split_license_plate(plate)
        print(f"  - 填寫車牌: 前碼 '{prefix}', 後碼 '{suffix}'")
        self.page.fill(self.config["selectors"]["license_prefix"], prefix)
        self.page.fill(self.config["selectors"]["license_suffix"], suffix)

        # 4. 移除唯讀屬性並寫入違規日期 (YYYY-MM-DD)
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_date_dashes(raw_date)
        print(f"  - 填寫違規日期: {formatted_date}")
        self.page.evaluate(f"document.getElementById('eventsData_vio_date').removeAttribute('readonly')")
        self.page.fill(self.config["selectors"]["case_date"], formatted_date)

        # 5. 時/分選單
        raw_time = violation_data.get("violation_time", "00:00")
        hour_str, min_str = self._parse_hour_minute(raw_time)
        print(f"  - 選擇違規時間: {hour_str} 時 {min_str} 分")
        self.page.select_option(self.config["selectors"]["case_hour"], value=hour_str)
        self.page.select_option(self.config["selectors"]["case_minute"], value=min_str)

        # 6. 地點街道對應與細部拆分
        full_location = violation_data.get("violation_location", "")
        district_name = violation_data.get("district", "")
        self._fill_road_and_details(full_location, district_name)

        # 7. 違規法規項目模糊匹配
        self._select_violation_law(violation_data)

        # 8. 事實敘述備註
        desc_text = violation_data.get("violation_description", "")
        self.page.fill(self.config["selectors"]["violation_remark"], desc_text)

    def _fill_road_and_details(self, full_location: str, district_name: str) -> None:
        clean_loc = full_location.replace("新北市", "").replace(district_name, "").strip()
        
        # 1. 搜尋匹配街道 (在 dropdown 中模糊匹配路段名稱)
        road_opts = self.page.locator(f"{self.config['selectors']['road']} option").all()
        matched_road_val = None
        matched_road_label = ""
        
        # 找尋最長匹配的路名
        max_len = 0
        for opt in road_opts:
            val = opt.evaluate("el => el.value")
            label = opt.evaluate("el => el.innerText").strip()
            if label and val:
                # 模糊匹配，例：路段出現在地址中
                clean_lbl = label.replace("１段", "1段").replace("２段", "2段").replace("３段", "3段").replace("４段", "4段")
                if clean_lbl in clean_loc or label in clean_loc:
                    if len(label) > max_len:
                        max_len = len(label)
                        matched_road_val = val
                        matched_road_label = label
                        
        if matched_road_val:
            print(f"  - 配對成功路段: '{matched_road_label}'")
            self.page.select_option(self.config["selectors"]["road"], value=matched_road_val)
            clean_loc = clean_loc.replace(matched_road_label.strip(), "").replace(matched_road_label.replace("１","1").replace("２","2").replace("３","3").strip(), "").strip()
        else:
            print("  - 未能在下拉選單中匹配到街道，選取「其他」路段。")
            self.page.select_option(self.config["selectors"]["road"], label="其他")
            
        # 2. 地點補充備註
        if not clean_loc:
            clean_loc = "前"
        print(f"  - 填寫補充詳細地點: '{clean_loc}'")
        self.page.fill(self.config["selectors"]["addr_remark"], clean_loc)

    def _select_violation_law(self, violation_data: Dict[str, Any]) -> None:
        subcat = (violation_data.get("violation_subcategory", "") or "") + " " + (violation_data.get("violation_description", "") or "")
        subcat = subcat.strip()
        
        # 關鍵字對應
        target_val = None
        if "身障" in subcat or "身心障礙" in subcat:
            target_val = "VC69" # 於身心障礙專用停車位違規停車
        elif "併排" in subcat:
            target_val = "VC70" # 併排停車
        elif "不依順行" in subcat:
            target_val = "VC81" # 不依順行方向臨時停車
        elif "人行道" in subcat and "臨時停車" in subcat:
            target_val = "VC84" # 人行道、行人穿越道違規臨時停車
        elif "人行道" in subcat and "停車" in subcat:
            target_val = "VC85" # 人行道、行人穿越道違規停車
        elif "闖紅燈" in subcat:
            target_val = "VC58" # 闖紅燈、紅燈右轉
        elif "行駛人行道" in subcat or ("行駛" in subcat and "人行道" in subcat):
            target_val = "VC48" # 駕車行駛人行道
            
        if target_val:
            print(f"  - 匹配成功違規法規項目代碼: {target_val}")
            self.page.select_option(self.config["selectors"]["violation_law"], value=target_val)
        else:
            # 遍歷選項模糊搜尋
            options = self.page.locator(f"{self.config['selectors']['violation_law']} option").all()
            selected = False
            for opt in options:
                txt = opt.evaluate("el => el.innerText")
                val = opt.evaluate("el => el.value")
                if "停車" in txt or "臨時停車" in txt:
                    self.page.select_option(self.config["selectors"]["violation_law"], value=val)
                    selected = True
                    print(f"  - 模糊匹配預設法規項目: '{txt.strip()}'")
                    break
            if not selected and len(options) > 1:
                val = options[1].evaluate("el => el.value")
                self.page.select_option(self.config["selectors"]["violation_law"], value=val)
                print("  - 選取預設法規項目。")

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[NewTaipei] 沒有提供媒體檔案。")
            return
            
        valid_paths = [p for p in media_paths if os.path.exists(p)]
        if not valid_paths:
            print("[NewTaipei] 未找到任何有效的媒體檔案。")
            return
            
        print(f"[NewTaipei] 正在動態新增檔案槽並上傳: {[os.path.basename(p) for p in valid_paths]}")
        
        # 依據檔案數量，點擊「新增檔案」按鈕動態新增輸入框
        for idx in range(1, len(valid_paths)):
            self.page.click(self.config["selectors"]["add_file_btn"])
            self.page.wait_for_timeout(300)
            
        # 分流填入上傳槽
        file_inputs = self.page.locator(self.config["selectors"]["file_input"]).all()
        for idx, path in enumerate(valid_paths):
            if idx < len(file_inputs):
                file_inputs[idx].set_input_files(path)
                self.page.wait_for_timeout(500)

    def handle_verification(self) -> None:
        print("[NewTaipei] 正在擷取驗證碼圖片...")
        self.page.wait_for_selector(self.config["selectors"]["login_captcha_img"], state="visible", timeout=5000)
        
        screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/npt_captcha_crop.png"
        self.page.locator(self.config["selectors"]["login_captcha_img"]).screenshot(path=screenshot_path)
        print(f"[NewTaipei] 驗證碼圖片已儲存: {screenshot_path}")
        
        # 定位焦點
        self.page.focus(self.config["selectors"]["login_captcha"])
        self.page.wait_for_timeout(500)
        
        print("\n" + "="*80)
        print(" 【人機協作圖形與信箱驗證】")
        print(" 1. 請至您的信箱點選剛發送的驗證郵件連結進行 Email OTP 驗證。")
        print(" 2. 在開啟的瀏覽器中填入圖形驗證碼並點選「送出」提交。")
        print("="*80 + "\n")
        
        # 暫停
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        print("[NewTaipei] 填表完成，安全聚焦送出按鈕並儲存預覽截圖...")
        self.page.focus(self.config["selectors"]["submit_btn"])
        self.page.wait_for_timeout(1000)
        
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/npt_filled_preview.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[NewTaipei] 預覽截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[NewTaipei] 截圖失敗: {e}")
            
        return False

    # ==================== 輔助方法 ====================

    def _format_date_dashes(self, date_str: str) -> str:
        """將西元日期轉換為 YYYY-MM-DD"""
        date_str = date_str.replace("/", "-").replace(".", "-").strip()
        if re.match(r"^\d{8}$", date_str):
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", date_str)
        if match:
            y, m, d = match.groups()
            return f"{y}-{int(m):02d}-{int(d):02d}"
        return date_str

    def _parse_hour_minute(self, time_str: str) -> tuple:
        time_str = time_str.strip()
        h, m = 0, 0
        if ":" in time_str:
            parts = time_str.split(":")
            h, m = int(parts[0]), int(parts[1])
        elif len(time_str) >= 4 and time_str.isdigit():
            h, m = int(time_str[:2]), int(time_str[2:4])
        return f"{h:02d}", f"{m:02d}"

    def _split_license_plate(self, plate: str) -> tuple:
        plate = plate.strip().upper()
        if "-" in plate:
            parts = plate.split("-", 1)
            return parts[0], parts[1]
        elif len(plate) >= 6:
            return plate[:3], plate[3:]
        return plate, ""
