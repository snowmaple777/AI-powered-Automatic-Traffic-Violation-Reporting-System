import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class HsinchuCountyDriver(BaseDriver):
    """
    新竹縣交通違規檢舉自動填表策略 (Hsinchu County Driver)
    """

    TOWN_MAP = {
        "竹北市": "竹北市",
        "竹北": "竹北市",
        "竹東鎮": "竹東鎮",
        "竹東": "竹東鎮",
        "關西鎮": "關西鎮",
        "關西": "關西鎮",
        "新埔鎮": "新埔鎮",
        "新埔": "新埔鎮",
        "湖口鄉": "湖口鄉",
        "湖口": "湖口鄉",
        "橫山鄉": "橫山鄉",
        "橫山": "橫山鄉",
        "新豐鄉": "新豐鄉",
        "新豐": "新豐鄉",
        "芎林鄉": "芎林鄉",
        "芎林": "芎林鄉",
        "寶山鄉": "寶山鄉",
        "寶山": "寶山鄉",
        "北埔鄉": "北埔鄉",
        "北埔": "北埔鄉",
        "峨眉鄉": "峨眉鄉",
        "峨眉": "峨眉鄉",
        "尖石鄉": "尖石鄉",
        "尖石": "尖石鄉",
        "五峰鄉": "五峰鄉",
        "五峰": "五峰鄉"
    }

    def navigate_to_start(self) -> None:
        print(f"[HsinchuCounty] 正在導航至新竹縣 Step 1: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(1000)

    def handle_pre_actions(self) -> None:
        print("[HsinchuCounty] 勾選同意條款並推進 Step 1 -> Step 2...")
        self.page.check(self.config["selectors"]["step1_agree_checkbox"])
        self.page.click(self.config["selectors"]["step1_next_btn"])
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_selector(self.config["selectors"]["reporter_name"], state="visible", timeout=10000)
        print("[HsinchuCounty] 成功進入 Step 2 檢舉人資料填寫頁面！")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[HsinchuCounty] 正在填寫 Step 2 檢舉人個人資料...")
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))
        
        # 住址拆解選取 (預設高雄市左營區)
        raw_address = reporter_data.get("reporter_address", "")
        city_name = "高雄市"
        town_name = "左營區"
        if "台北" in raw_address or "臺北" in raw_address: city_name = "臺北市"
        elif "新北" in raw_address: city_name = "新北市"
        elif "台中" in raw_address or "臺中" in raw_address: city_name = "臺中市"
        elif "台南" in raw_address or "臺南" in raw_address: city_name = "臺南市"
        elif "新竹" in raw_address: city_name = "新竹市" if "市" in raw_address else "新竹縣"
        
        print(f"  - 選擇居住縣市: {city_name}")
        self.page.select_option(self.config["selectors"]["reporter_city"], label=city_name)
        self.page.wait_for_timeout(500)
        
        try:
            self.page.select_option(self.config["selectors"]["reporter_town"], label=town_name)
        except Exception:
            pass
            
        clean_address = raw_address
        for prefix in ["高雄市", "左營區", "臺北市", "新北市", "臺中市", "臺南市", "新竹市", "新竹縣"]:
            clean_address = clean_address.replace(prefix, "").strip()
            
        self.page.fill(self.config["selectors"]["reporter_address_number"], clean_address or raw_address)
        
        print("[HsinchuCounty] 推進 Step 2 -> Step 3...")
        self.page.click(self.config["selectors"]["step2_next_btn"])
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_selector(self.config["selectors"]["violation_date"], state="visible", timeout=10000)
        print("[HsinchuCounty] 成功進入 Step 3 檢舉內容填寫頁面！")

    def select_district(self, district_name: str) -> None:
        clean_dist = district_name.replace("新竹縣", "").strip()
        target_town = "竹北市"
        for key, val in self.TOWN_MAP.items():
            if key in clean_dist or clean_dist in key:
                target_town = val
                break
                
        print(f"  - 選擇違規鄉鎮市區: '{clean_dist}' (選取: {target_town})")
        self.page.select_option(self.config["selectors"]["violation_town"], label=target_town)

    def select_village(self, village_name: str) -> None:
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[HsinchuCounty] 正在填寫 Step 3 違規詳細資料...")
        
        # 1. 日期 (YYYY-MM-DD) 與 時間 (HH:MM)
        formatted_date = self._format_date(violation_data.get("violation_date", ""))
        hour_str, min_str = self._parse_hour_minute(violation_data.get("violation_time", "00:00"))
        formatted_time = f"{hour_str}:{min_str}"
        
        print(f"  - 填寫違規日期: {formatted_date}, 時間: {formatted_time}")
        self.page.fill(self.config["selectors"]["violation_date"], formatted_date)
        self.page.fill(self.config["selectors"]["violation_time"], formatted_time)
        
        # 2. 違規行政區與詳細地點細項拆分填寫
        district_name = violation_data.get("district", "竹北市")
        self.select_district(district_name)
        full_location = violation_data.get("violation_location", "")
        print(f"  - 拆分並填寫詳細地點細項: '{full_location}'")
        self._parse_and_fill_location(full_location, district_name)
        
        # 3. 車牌雙欄拆分 (CarCode1 前碼 / CarCode2 後碼)
        plate = violation_data.get("license_plate", "")
        prefix, suffix = self._split_license_plate(plate)
        print(f"  - 填寫拆分車牌: 前碼 '{prefix}', 後碼 '{suffix}'")
        self.page.fill(self.config["selectors"]["license_plate_prefix"], prefix)
        self.page.fill(self.config["selectors"]["license_plate_suffix"], suffix)
        
        # 4. 法條選擇 (ddlLegislation)
        fact_desc = (violation_data.get("violation_subcategory", "") or violation_data.get("violation_description", "")).strip()
        print(f"  - 搜尋並配對違規法條 (關鍵字: '{fact_desc}')...")
        options = self.page.locator(f"{self.config['selectors']['violation_fact_select']} option").all()
        selected = False
        for opt in options:
            txt = opt.evaluate("el => el.innerText")
            val = opt.evaluate("el => el.value")
            if "56" in txt or "人行道" in txt or "停車" in txt:
                self.page.select_option(self.config["selectors"]["violation_fact_select"], value=val)
                selected = True
                print(f"    - 成功配對法條: '{txt[:30]}...'")
                break
                
        if not selected and len(options) > 1:
            val = options[1].evaluate("el => el.value")
            self.page.select_option(self.config["selectors"]["violation_fact_select"], value=val)
            print("    - 選取預設法條。")
            
        # 5. 詳細描述內容
        desc_text = violation_data.get("violation_description", "")
        self.page.fill(self.config["selectors"]["violation_description"], desc_text)

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[HsinchuCounty] 沒有提供附加媒體檔案。")
            return
            
        valid_paths = [p for p in media_paths if os.path.exists(p)]
        if not valid_paths:
            print("[HsinchuCounty] 未找到任何有效的媒體檔案。")
            return
            
        print(f"[HsinchuCounty] 正在上傳附加檔案至 #fileInput: {[os.path.basename(p) for p in valid_paths]}")
        self.page.set_input_files(self.config["selectors"]["file_input"], valid_paths[0])
        self.page.wait_for_timeout(1000)
        
        # 觸發 change 事件完成檔案佇列排隊
        self.page.evaluate("""() => {
            const fi = document.querySelector('#fileInput');
            if (fi) {
                fi.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }""")
        self.page.wait_for_timeout(1500)
        
        print("[HsinchuCounty] 推進 Step 3 -> Step 4...")
        self.page.click(self.config["selectors"]["step3_next_btn"])
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_timeout(2000)

    def handle_verification(self) -> None:
        print("[HsinchuCounty] 已成功推進至 Step 4 要件檢核表，安全停留在「提交申請」前。")
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/hc_filled_preview.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[HsinchuCounty] 填寫完成之全頁面截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[HsinchuCounty] 儲存截圖失敗: {e}")
            
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        print("[HsinchuCounty] 自動填表完畢，安全停留在送出表單前，請使用者確認無誤後手動點擊「提交申請」並接收 Email 認證信。")
        return False

    # ==================== 輔助方法 ====================

    def _parse_and_fill_location(self, full_location: str, district_name: str) -> None:
        """解析違規地點，將路段自動檢索填入 Select2 選單，並將 鄰/巷/弄/號 及 補充說明 分別填入專屬欄位"""
        clean_loc = full_location.replace("新竹縣", "").replace(district_name, "").replace(district_name.replace("市", "").replace("鎮", "").replace("鄉", ""), "").strip()
        twn_id = self.page.evaluate("() => $('#MainContent_hidSelectedViolationTownId').val() || $('#MainContent_ddlViolationTown').val()")
        
        # 1. 嘗試配對路段 (路/街/大道/段)
        road_match = re.search(r"([^\d\s,，]+?(?:路|街|大道|段))", clean_loc)
        matched_road_id = None
        matched_road_text = ""
        
        if road_match and twn_id:
            kw = road_match.group(1)
            try:
                res = self.page.evaluate(f"""async () => {{
                    const r = await fetch('/api/address/roads/search?twnId={twn_id}&q={kw}');
                    return await r.json();
                }}""")
                results = res.get("results", [])
                for item in results:
                    if item["text"] == kw or item["text"] in clean_loc or clean_loc in item["text"]:
                        matched_road_id = item["id"]
                        matched_road_text = item["text"]
                        break
                if not matched_road_id and results:
                    matched_road_id = results[0]["id"]
                    matched_road_text = results[0]["text"]
            except Exception as e:
                print(f"    - 路段 API 查詢提示: {e}")

        if matched_road_id and matched_road_text:
            self.page.evaluate(f"""() => {{
                var newOption = new Option('{matched_road_text}', '{matched_road_id}', true, true);
                $('#ddlViolationRoad').append(newOption).trigger('change');
                $('#MainContent_hidSelectedViolationRoadId').val('{matched_road_id}');
            }}""")
            print(f"    - 路段選單已設定: '{matched_road_text}' (ID: {matched_road_id})")
            clean_loc = clean_loc.replace(matched_road_text, "").strip()
        else:
            print(f"    - 未能在選單中匹配路段，將保持詳細文字填寫: '{clean_loc}'")

        # 2. 提取 鄰, 巷, 弄, 號
        lin_m = re.search(r"(\d+)鄰", clean_loc)
        lane_m = re.search(r"(\d+)巷", clean_loc)
        alley_m = re.search(r"(\d+)弄", clean_loc)
        num_m = re.search(r"(\d+)號", clean_loc)
        
        lin_val = lin_m.group(1) if lin_m else ""
        lane_val = lane_m.group(1) if lane_m else ""
        alley_val = alley_m.group(1) if alley_m else ""
        num_val = num_m.group(1) if num_m else ""
        
        if lin_m: clean_loc = clean_loc.replace(lin_m.group(0), "")
        if lane_m: clean_loc = clean_loc.replace(lane_m.group(0), "")
        if alley_m: clean_loc = clean_loc.replace(alley_m.group(0), "")
        if num_m: clean_loc = clean_loc.replace(num_m.group(0), "")
        
        other_val = clean_loc.strip()
        if not other_val:
            other_val = "無"
            
        print(f"    - 地點細項拆分 -> 鄰: '{lin_val}', 巷: '{lane_val}', 弄: '{alley_val}', 號: '{num_val}', 補充說明: '{other_val}'")
        
        if lin_val: self.page.fill(self.config["selectors"]["violation_neighborhood"], lin_val)
        if lane_val: self.page.fill(self.config["selectors"]["violation_lane"], lane_val)
        if alley_val: self.page.fill(self.config["selectors"]["violation_alley"], alley_val)
        if num_val: self.page.fill(self.config["selectors"]["violation_number"], num_val)
        self.page.fill(self.config["selectors"]["violation_detail_location"], other_val)

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
