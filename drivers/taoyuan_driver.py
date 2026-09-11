import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class TaoyuanDriver(BaseDriver):
    """
    桃園市交通違規檢舉自動填表策略 (Taoyuan Driver)
    """

    DISTRICT_MAP = {
        "桃園": "4",
        "中壢": "5",
        "大溪": "6",
        "楊梅": "7",
        "蘆竹": "8",
        "大園": "9",
        "龜山": "10",
        "八德": "11",
        "龍潭": "12",
        "平鎮": "13",
        "新屋": "14",
        "觀音": "15",
        "復興": "16"
    }

    def run_workflow(self, input_data: Dict[str, Any], media_files: List[str]) -> bool:
        """
        覆寫主流程以對接桃園市的兩階段頁面設計 (D0101 登入驗證碼 -> D0102 案件填寫)。
        """
        try:
            self.input_data = input_data
            
            # 1. 導覽至首頁同意條款
            self.navigate_to_start()
            self.handle_pre_actions()
            
            # 2. 填寫第一階段：檢舉人基本資料 (D0101)
            self.fill_reporter_info(input_data)
            
            # 3. 處理驗證碼並引導使用者在 D0101 登入
            self.handle_verification()
            
            # 4. 進入第二階段：檢舉案件詳情 (D0102)
            self.select_district(input_data.get("district", ""))
            self.select_village(input_data.get("village", ""))
            self.fill_violation_details(input_data)
            self.upload_media(media_files)
            
            # 5. 安全停留在送出按鈕前
            return self.submit()
            
        except Exception as e:
            print(f"[TAOYUAN Driver 錯誤] 填表流程中斷: {e}")
            raise e

    def navigate_to_start(self) -> None:
        print(f"[Taoyuan] 正在導航至桃園市檢舉首頁: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(1000)

    def handle_pre_actions(self) -> None:
        print("[Taoyuan] 勾選同意宣告並點擊下一步進入登入頁面...")
        self.page.check(self.config["selectors"]["agreement_checkbox"])
        self.page.click(self.config["selectors"]["agreement_submit"])
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_selector(self.config["selectors"]["reporter_name"], state="visible", timeout=10000)
        print("[Taoyuan] 成功進入第一階段：檢舉人登入頁面 (D0101)！")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Taoyuan] 正在填寫檢舉人聯絡資料...")
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))

    def handle_verification(self) -> None:
        # 如果目前在 D0101，則需要進行圖形驗證碼截圖與人機協作登入
        if "D0101" in self.page.url:
            print("[Taoyuan] 正在擷取圖形驗證碼圖片...")
            self.page.wait_for_selector(self.config["selectors"]["login_canvas"], state="visible", timeout=5000)
            
            # 對 Canvas 驗證碼進行截圖
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/ty_captcha_crop.png"
            self.page.locator(self.config["selectors"]["login_canvas"]).screenshot(path=screenshot_path)
            print(f"[Taoyuan] 驗證碼圖片已儲存: {screenshot_path}")
            
            # 將焦點放到驗證碼輸入框
            self.page.focus(self.config["selectors"]["login_captcha"])
            self.page.wait_for_timeout(500)
            
            print("\n" + "="*80)
            print(" 【人機協作圖形驗證碼】")
            print(" 請在開啟的瀏覽器中輸入圖形驗證碼並點擊「確認送出」登入。")
            print("="*80 + "\n")
            
            # 觸發驗證碼協作 hook
            self.trigger_hook("on_verification")
            
            # 等待跳轉至 D0102
            print("[Taoyuan] 等待網頁跳轉至 D0102 案件填寫頁面...")
            self.page.wait_for_selector(self.config["selectors"]["case_date"], state="visible", timeout=60000)
            print("[Taoyuan] 成功登入，已進入第二階段：案件填寫頁面 (D0102)！")

    def select_district(self, district_name: str) -> None:
        print("[Taoyuan] 正在選取违規行政區...")
        # 1. 選擇桃園市
        self.page.select_option(self.config["selectors"]["city"], label="桃園市")
        self.page.wait_for_timeout(500)
        
        # 2. 匹配行政區 value
        clean_dist = district_name.strip()
        target_val = "4"  # 預設桃園區
        for key, val in self.DISTRICT_MAP.items():
            if key in clean_dist or clean_dist in key:
                target_val = val
                break
                
        print(f"  - 選擇行政區 value: {target_val} ({district_name})")
        self.page.select_option(self.config["selectors"]["district"], value=target_val)
        self.page.wait_for_timeout(1000)  # 等待路段選單初始化

    def select_village(self, village_name: str) -> None:
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Taoyuan] 正在填寫違規內容資料...")

        # 1. 違規日期 (格式必須為 yyyy/MM/dd)
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_date_slashes(raw_date)
        print(f"  - 填寫違規日期: {formatted_date}")
        self.page.fill(self.config["selectors"]["case_date"], formatted_date)

        # 2. 違規時間 (格式為 HH:mm)
        raw_time = violation_data.get("violation_time", "00:00")
        hour_str, min_str = self._parse_hour_minute(raw_time)
        formatted_time = f"{hour_str}:{min_str}"
        print(f"  - 填寫違規時間: {formatted_time}")
        self.page.fill(self.config["selectors"]["case_time"], formatted_time)

        # 3. 填寫車牌號碼雙欄拆分
        plate = violation_data.get("license_plate", "")
        prefix, suffix = self._split_license_plate(plate)
        print(f"  - 填寫車牌: 前碼 '{prefix}', 後碼 '{suffix}'")
        self.page.fill(self.config["selectors"]["license_prefix"], prefix)
        self.page.fill(self.config["selectors"]["license_suffix"], suffix)

        # 4. 選擇違規車種
        car_type = violation_data.get("car_type", "汽車")
        target_type = "汽車"
        if "大型重機" in car_type or "大型重型機車" in car_type:
            target_type = "大型重型機車"
        elif "重機" in car_type or "重型機車" in car_type:
            target_type = "重型機車"
        elif "輕機" in car_type or "輕型機車" in car_type:
            target_type = "輕型機車"
            
        print(f"  - 選擇違規車種: {target_type}")
        self.page.select_option(self.config["selectors"]["car_type"], label=target_type)

        # 5. 填寫路段 Selectize
        full_location = violation_data.get("violation_location", "")
        district_name = violation_data.get("district", "桃園區")
        self._fill_road_and_details(full_location, district_name)

        # 6. 違規項目選取 (區分動態違規與靜態違規)
        self._select_violation_law(violation_data)

        # 7. 事實備註敘述
        desc_text = violation_data.get("violation_description", "")
        if desc_text and self.page.is_visible(self.config["selectors"]["case_description"]):
            self.page.fill(self.config["selectors"]["case_description"], desc_text)

    def _fill_road_and_details(self, full_location: str, district_name: str) -> None:
        clean_loc = full_location.replace("桃園市", "").replace(district_name, "").strip()
        
        # 1. 擷取路段名 (路/街/大道/段)
        road_match = re.search(r"([^\d\s,，]+?(?:路|街|大道|段))", clean_loc)
        if road_match:
            road_name = road_match.group(1)
            print(f"  - 在 Selectize 搜尋路段: '{road_name}'...")
            self.page.fill(self.config["selectors"]["road_input"], road_name)
            self.page.wait_for_timeout(1000)
            
            # 點選下拉選項
            try:
                self.page.click(self.config["selectors"]["road_dropdown_active"], timeout=3000)
                print("    - 成功選擇下拉路段項目。")
                clean_loc = clean_loc.replace(road_name, "").strip()
            except Exception:
                print("    - 未能在下拉選單中成功點選，將以詳細文字輸入。")
                
        # 2. 提取 街, 巷, 弄, 衖, 號
        jie_m = re.search(r"(\d+)街", clean_loc)
        lane_m = re.search(r"(\d+)巷", clean_loc)
        alley_m = re.search(r"(\d+)弄", clean_loc)
        sublane_m = re.search(r"(\d+)衖", clean_loc)
        num_m = re.search(r"(\d+)號", clean_loc)
        
        jie_val = jie_m.group(1) if jie_m else ""
        lane_val = lane_m.group(1) if lane_m else ""
        alley_val = alley_m.group(1) if alley_m else ""
        sublane_val = sublane_m.group(1) if sublane_m else ""
        num_val = num_m.group(1) if num_m else ""
        
        if jie_m: clean_loc = clean_loc.replace(jie_m.group(0), "")
        if lane_m: clean_loc = clean_loc.replace(lane_m.group(0), "")
        if alley_m: clean_loc = clean_loc.replace(alley_m.group(0), "")
        if sublane_m: clean_loc = clean_loc.replace(sublane_m.group(0), "")
        if num_m: clean_loc = clean_loc.replace(num_m.group(0), "")
        
        other_val = clean_loc.strip()
        if not other_val:
            other_val = "前"
            
        print(f"    - 地點細項拆分 -> 街/段: '{jie_val}', 巷: '{lane_val}', 弄: '{alley_val}', 衖: '{sublane_val}', 號: '{num_val}', 備註: '{other_val}'")
        
        if jie_val: self.page.fill(self.config["selectors"]["addr_street"], jie_val)
        if lane_val: self.page.fill(self.config["selectors"]["addr_lane"], lane_val)
        if alley_val: self.page.fill(self.config["selectors"]["addr_alley"], alley_val)
        if sublane_val: self.page.fill(self.config["selectors"]["addr_sublane"], sublane_val)
        if num_val: self.page.fill(self.config["selectors"]["addr_number"], num_val)
        
        # 僅當地址備註欄位可見時填寫，否則忽略（或附加於案情描述）
        if other_val and self.page.is_visible(self.config["selectors"]["addr_remark"]):
            self.page.fill(self.config["selectors"]["addr_remark"], other_val)

    def _select_violation_law(self, violation_data: Dict[str, Any]) -> None:
        category = violation_data.get("violation_category", "")
        subcat = violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")
        
        # 判斷是靜態還是動態違規 (靜態如停車、臨時停車、排隊)
        is_static = "停車" in category or "停車" in subcat or "臨時停車" in subcat or "併排" in subcat or "人行道" in subcat
        
        if is_static:
            print("  - 違規類型判定為: [靜態違規]")
            self.page.select_option(self.config["selectors"]["violation_type"], label="靜態違規")
            self.page.wait_for_timeout(1000)
            law_selector = self.config["selectors"]["fact_static"]
        else:
            print("  - 違規類型判定為: [動態違規]")
            self.page.select_option(self.config["selectors"]["violation_type"], label="動態違規")
            self.page.wait_for_timeout(1000)
            law_selector = self.config["selectors"]["fact_dynamic"]
            
        # 搜尋並匹配法條
        fact_desc = subcat.strip()
        options = self.page.locator(f"{law_selector} option").all()
        selected = False
        for opt in options:
            txt = opt.evaluate("el => el.innerText")
            val = opt.evaluate("el => el.value")
            if "56" in txt or "55" in txt or "人行道" in txt or "停車" in txt or "臨時停車" in txt:
                self.page.select_option(law_selector, value=val)
                selected = True
                print(f"    - 成功配對法條: '{txt[:30]}...'")
                break
                
        if not selected and len(options) > 1:
            val = options[1].evaluate("el => el.value")
            self.page.select_option(law_selector, value=val)
            print("    - 選取預設法條。")

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Taoyuan] 沒有提供附加媒體檔案。")
            return
            
        valid_paths = [p for p in media_paths if os.path.exists(p)]
        if not valid_paths:
            print("[Taoyuan] 未找到任何有效的媒體檔案。")
            return
            
        print(f"[Taoyuan] 正在上傳附加檔案 (最多 5 個槽分流): {[os.path.basename(p) for p in valid_paths]}")
        file_inputs = self.config["selectors"]["file_inputs"]
        for idx, path in enumerate(valid_paths[:5]):
            if idx < len(file_inputs):
                selector = file_inputs[idx]
                if self.page.is_visible(selector):
                    self.page.set_input_files(selector, path)
                    self.page.wait_for_timeout(500)

    def submit(self) -> bool:
        print("[Taoyuan] 填表完成，勾選誠實聲明，儲存預覽截圖並安全停留...")
        self.page.check(self.config["selectors"]["agreement_checkbox_final"])
        
        # 焦點定位到送出按鈕
        self.page.focus(self.config["selectors"]["submit_btn"])
        self.page.wait_for_timeout(500)
        
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/ty_filled_preview.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[Taoyuan] 填寫完成之全頁面截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[Taoyuan] 儲存截圖失敗: {e}")
            
        return False

    # ==================== 輔助方法 ====================

    def _format_date_slashes(self, date_str: str) -> str:
        """將日期格式化為 yyyy/MM/dd 格式"""
        date_str = date_str.replace("-", "/").replace(".", "/").strip()
        if re.match(r"^\d{8}$", date_str):
            return f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
        match = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", date_str)
        if match:
            y, m, d = match.groups()
            return f"{y}/{int(m):02d}/{int(d):02d}"
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
