import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class KaohsiungDriver(BaseDriver):
    """
    高雄市交通違規檢舉填表策略 (Strategy Pattern)。
    對應網址: https://policemail.kcg.gov.tw/Statement.aspx
    """

    def navigate_to_start(self) -> None:
        print(f"[Kaohsiung] 正在導覽至起始聲明頁面: {self.config['start_url']}")
        self.page.goto(self.config["start_url"])
        self.page.wait_for_load_state("domcontentloaded")

    def handle_pre_actions(self) -> None:
        print("[Kaohsiung] 正在執行前置同意書勾選...")
        # 勾選四個條款
        for selector in self.config["selectors"]["agreement_checkboxes"]:
            self.page.check(selector)
            
        print("[Kaohsiung] 點擊「我要檢舉」按鈕...")
        self.page.click(self.config["selectors"]["agreement_submit"])
        
        # 等待網址跳轉至 Mail.aspx
        self.page.wait_for_url("**/Mail.aspx**", timeout=15000)
        print(f"[Kaohsiung] 成功進入資料填寫頁面: {self.page.url}")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Kaohsiung] 正在填寫檢舉人基本資料...")
        
        # 1. 姓名
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        
        # 2. 身份證字號
        reporter_id = reporter_data.get("reporter_id", "").upper()
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_id)
        
        # 3. 國籍 (預設本國籍)
        self.page.check(self.config["selectors"]["reporter_nationality_domestic"])
        
        # 4. 性別 (依身分證字號第二碼自動判定)
        if len(reporter_id) > 1:
            gender_code = reporter_id[1]
            if gender_code == '1':
                self.page.check(self.config["selectors"]["reporter_gender_male"])
            elif gender_code == '2':
                self.page.check(self.config["selectors"]["reporter_gender_female"])
                
        # 5. 地址
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))
        
        # 6. E-mail
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))
        
        # 7. 電話
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))

    def select_district(self, district_name: str) -> None:
        """選擇違規行政區 (處理兩個行政區 dropdown，支援模糊地名與分局剔除)"""
        if not district_name:
            return
            
        print(f"[Kaohsiung] 正在選擇違規行政區: {district_name}")

        # 行政區專用比對：自動去除「高雄市」、「分局」、「派出所」與結尾的區鄉鎮市，
        # 並要求完全相同，避免「東區」配到「東勢區」這類錯誤
        # 「違規行政區」 #ContentPlaceHolder1_ViolationArea
        self.select_native(self.config["selectors"]["violation_district"], district_name,
                           field_label="違規行政區", district=True, city="高雄市")

        # 「交通違規地點行政區」 #ContentPlaceHolder1_uscPlace_ddlArea
        self.select_native(self.config["selectors"]["violation_location_district"], district_name,
                           field_label="交通違規地點行政區", district=True, city="高雄市")

    def select_village(self, village_name: str) -> None:
        # 高雄市表單無此欄位，直接 pass
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Kaohsiung] 正在填寫違規內容與時間...")
        
        # 1. 格式化並填寫違規日期/時間 (相容 YYYYMMDD 等非標準格式)
        date_str = self._format_date(violation_data.get("violation_date", ""))
        time_str = violation_data.get("violation_time", "") # HH:MM
        datetime_val = f"{date_str} {time_str}".strip()
        
        self.page.fill(self.config["selectors"]["violation_date"], datetime_val)
        
        # 填寫並調整隱藏或輔助的 hours / minutes range 滑桿以防萬一
        time_parts = time_str.split(":")
        if len(time_parts) == 2:
            try:
                h_val = str(int(time_parts[0]))
                m_val = str(int(time_parts[1]))
                self.page.fill(self.config["selectors"]["time_hours"], h_val)
                self.page.fill(self.config["selectors"]["time_minutes"], m_val)
                print(f"  - 輔助滑桿設定成功：{h_val} 時 {m_val} 分")
            except Exception as te:
                print(f"  - 無法設定輔助滑桿 (忽略): {te}")
        
        # 2. 車牌號碼拆分 (高雄市拆分為兩欄：前/後)
        license_plate = violation_data.get("license_plate", "")
        parts = license_plate.split("-")
        if len(parts) == 2:
            self.page.fill(self.config["selectors"]["violation_plate_part1"], parts[0].strip())
            self.page.fill(self.config["selectors"]["violation_plate_part2"], parts[1].strip())
        else:
            self.page.fill(self.config["selectors"]["violation_plate_part1"], license_plate[:3])
            self.page.fill(self.config["selectors"]["violation_plate_part2"], license_plate[3:])
            
        # 3. 車種 (汽車/重機/輕機)
        car_type = violation_data.get("car_type", "汽車")
        if "重機" in car_type or "大型重機" in car_type:
            self.page.check(self.config["selectors"]["car_type_heavy_moto"])
        elif "輕機" in car_type or "機車" in car_type:
            self.page.check(self.config["selectors"]["car_type_light_moto"])
        else:
            self.page.check(self.config["selectors"]["car_type_auto"])
            
        # 4. 違規事實大類 (Dropdown) 與小類細項
        # 5. 違規事實大類 → 小類 (兩層連動選單)
        # 依 configs/kaohsiung.json 的 violation_map 查表，直接選取對應的完整選項文字（不做模糊比對）。
        # 大類選單是 AutoPostBack，選了之後整頁重新載入小類；select_cascade 會自動等待小類選項更新。
        self._select_violation_type(violation_data)
        
        # 6. 違規詳細地點
        self.page.fill(self.config["selectors"]["violation_location_address"], violation_data.get("violation_location", ""))
        
        # 7. 違規事實陳述/內容描述
        self.page.fill(self.config["selectors"]["violation_description"], violation_data.get("violation_description", ""))

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Kaohsiung] 沒有提供附加媒體檔案。")
            return
            
        print(f"[Kaohsiung] 正在上傳附加檔案 (限制最多 3 個): {media_paths}")
        
        # 高雄市真正的上傳策略是使用一個單一的可見 File 欄位與一個「上傳」按鈕
        # 逐一選擇並點擊上傳 (每次上傳皆會引起 ASP.NET 頁面 postback)
        file_input_selector = self.config["selectors"]["file_upload_input"]
        upload_btn_selector = self.config["selectors"]["file_upload_submit"]
        
        for idx, path in enumerate(media_paths[:3]):
            if os.path.exists(path):
                print(f"  - 正在選取第 {idx+1} 個檔案: {path}")
                self.page.set_input_files(file_input_selector, path)
                
                print(f"  - 點擊「上傳」按鈕並等待頁面更新...")
                try:
                    with self.page.expect_navigation(timeout=20000):
                        self.page.click(upload_btn_selector)
                    print(f"  - 第 {idx+1} 個檔案上傳完成。")
                except Exception as ue:
                    print(f"  - 檔案上傳發生超時或更新異常 (忽略並繼續): {ue}")
                    self.page.wait_for_timeout(3000)

    def handle_verification(self) -> None:
        print("[Kaohsiung] 觸發手動驗證 Hook...")
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        # 檢查是否已完成驗證並跳轉
        if "Mail.aspx" not in self.page.url:
            print("[Kaohsiung] 偵測到網頁已提前完成跳轉，無須重複點擊送出。")
            return self.wait_for_success()
            
        print("[Kaohsiung] 執行最後送出點擊...")
        self.page.click(self.config["selectors"]["submit_btn"])
        return self.wait_for_success()

    def wait_for_success(self) -> bool:
        """等待並判斷是否發送成功"""
        print("[Kaohsiung] 正在驗證是否檢舉成功...")
        try:
            self.page.wait_for_function("() => !window.location.href.includes('Mail.aspx')", timeout=10000)
            current_url = self.page.url
            print(f"[Kaohsiung] 已跳離填寫頁面，目前 URL: {current_url}")
            
            if "DisplayCaptcha" not in current_url:
                print("[Kaohsiung] 成功判定：已進入收取驗證信階段。")
                return True
        except Exception as e:
            print(f"[Kaohsiung 警告] 驗證成功狀態判定超時: {e}")
            
        return False

    # ==================== 輔助與格式化方法 ====================

    def _select_violation_type(self, violation_data: Dict[str, Any]) -> None:
        """依 violation_map 查表選取違規事實大類與小類"""
        cat_sel = self.config["selectors"]["violation_category"]
        sub_sel = self.config["selectors"]["violation_subcategory"]
        violation_map = {k: v for k, v in self.config.get("violation_map", {}).items() if not k.startswith("_")}

        # 名稱與別名 (aliases) 都對應到正式名稱；只忽略空白，文字必須完全相同
        lookup = {}
        for name, entry in violation_map.items():
            for n in [name] + list(entry.get("aliases", [])):
                lookup[re.sub(r"\s+", "", n)] = name

        # info.txt 的「違規事實」優先，其次「違規類別」
        candidates = [violation_data.get("violation_subcategory"), violation_data.get("violation_category")]
        key = next((lookup[re.sub(r"\s+", "", c)] for c in candidates
                    if c and re.sub(r"\s+", "", c) in lookup), None)

        if key is None:
            supported = "、".join(violation_map)
            detail = (f"info.txt 的違規事實/違規類別 ({[c for c in candidates if c]}) 不在支援清單中。"
                      f"支援的項目：{supported}")
            print(f"  - [違規事實] {detail}")
            self._record_unmatched(cat_sel, "違規事實大類", candidates, detail)
            self._record_unmatched(sub_sel, "違規事實小類", candidates, detail)
            return

        entry = violation_map[key]
        print(f"  - 違規項目「{key}」→ 大類「{entry['category']}」/ 小類「{entry['subcategory']}」")
        self.select_cascade(
            cat_sel, entry["category"],
            sub_sel, entry["subcategory"],
            parent_label="違規事實大類",
            child_label="違規事實小類",
            exact=True,
        )

    def _format_date(self, date_str: str) -> str:
        """將各種日期格式（如 20260706、2026/07/06 等）標準化為 YYYY-MM-DD"""
        date_str = date_str.replace("/", "-").replace(".", "-").strip()
        # 情況 A: 8碼純數字 (例如 20260706)
        if re.match(r"^\d{8}$", date_str):
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        # 情況 B: 已經是 YYYY-MM-DD 或 YYYY-M-D
        match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", date_str)
        if match:
            y, m, d = match.groups()
            return f"{y}-{int(m):02d}-{int(d):02d}"
        return date_str
