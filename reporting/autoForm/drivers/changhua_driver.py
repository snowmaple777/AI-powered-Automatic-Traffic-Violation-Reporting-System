import os
import re
from typing import Dict, Any, List
from drivers.base_driver import BaseDriver

class ChanghuaDriver(BaseDriver):
    """
    彰化縣交通違規檢舉自動填表策略 (Changhua Driver)
    """

    def navigate_to_start(self) -> None:
        print(f"[Changhua] 正在導航至彰化縣首頁: {self.config['start_url']}")
        self.page.goto(self.config["start_url"], wait_until="domcontentloaded")
        self.page.wait_for_timeout(2000)

    def handle_pre_actions(self) -> None:
        print("[Changhua] 點擊「我已詳細閱讀並同意遵守」按鈕...")
        self.page.click(self.config["selectors"]["agreement_link"])
        self.page.wait_for_selector(self.config["selectors"]["reporter_name"], state="visible", timeout=10000)
        print("[Changhua] 成功進入檢舉表單頁面")

    def fill_reporter_info(self, reporter_data: Dict[str, Any]) -> None:
        print("[Changhua] 正在填寫檢舉人聯絡資料...")
        self.page.fill(self.config["selectors"]["reporter_name"], reporter_data.get("reporter_name", ""))
        self.page.fill(self.config["selectors"]["reporter_id"], reporter_data.get("reporter_id", "").upper())
        self.page.fill(self.config["selectors"]["reporter_phone"], reporter_data.get("reporter_phone", ""))
        self.page.fill(self.config["selectors"]["reporter_address"], reporter_data.get("reporter_address", ""))
        self.page.fill(self.config["selectors"]["reporter_email"], reporter_data.get("reporter_email", ""))

    def select_district(self, district_name: str) -> None:
        if not district_name:
            return
        clean_dist = district_name.replace("彰化縣", "").strip()
        # 去除結尾的 鄉/鎮/市/區 以便進行模糊比對
        clean_dist_base = re.sub(r"[鄉鎮市區]$", "", clean_dist)
        print(f"  - 選擇行政區: {clean_dist} (基準字: {clean_dist_base})")
        try:
            # 等待行政區選單附著於 DOM 完成，即使 display: none 也能相容
            self.page.wait_for_selector(self.config["selectors"]["location_town"], state="attached", timeout=5000)
            select_el = self.page.locator(self.config["selectors"]["location_town"])
            
            # 獲取所有選項的文字與值
            opts = select_el.evaluate("""el => {
                return Array.from(el.options).map(o => ({text: o.text, value: o.value}));
            }""")
            
            target_val = None
            for opt in opts:
                text = opt["text"].strip()
                val = opt["value"].strip()
                if clean_dist_base in text or clean_dist_base in val:
                    target_val = val
                    break
                    
            if target_val is not None:
                select_el.select_option(value=target_val)
                print(f"    - 行政區選取成功 (Value: {target_val})")
            else:
                select_el.select_option(label=clean_dist)
                print(f"    - 行政區直接以標籤選取: {clean_dist}")
        except Exception as e:
            print(f"[Changhua 警告] 選擇行政區時發生錯誤: {e}")

    def select_village(self, village_name: str) -> None:
        # 彰化無獨立村里下拉，直接 pass
        pass

    def fill_violation_details(self, violation_data: Dict[str, Any]) -> None:
        print("[Changhua] 正在填寫違規案發詳細資料...")
        
        # 1. 主題 (自動組裝)
        plate = violation_data.get("license_plate", "")
        subject_str = f"檢舉 {plate} 交通違規"
        self.page.fill(self.config["selectors"]["subject"], subject_str)
        
        # 2. 車牌號碼
        self.page.fill(self.config["selectors"]["license_plate"], plate)
        
        # 3. 違規發生地點
        self.page.fill(self.config["selectors"]["location_detail"], violation_data.get("violation_location", ""))
        
        # 4. 行政區 (若有行政區，在此處確認選取)
        self.select_district(violation_data.get("district", ""))
        
        # 5. 違規日期 (HTML5 type=date)
        raw_date = violation_data.get("violation_date", "")
        formatted_date = self._format_date_hyphens(raw_date)
        self.page.fill(self.config["selectors"]["violation_date"], formatted_date)
        
        # 6. 違規時間 (HTML5 type=time)
        raw_time = violation_data.get("violation_time", "")
        formatted_time = raw_time if ":" in raw_time else f"{raw_time[:2]}:{raw_time[2:]}"
        self.page.fill(self.config["selectors"]["violation_time"], formatted_time)
        
        # 7. 勾選違規項目 Checkbox (點選對應 label 以防 pointer-events 攔截)
        # 違規項目 Checkbox：讀出每個項目的標籤文字，依共用違規目錄模糊比對；比對不到就標紅框
        checkboxes = self.page.locator("input[name='ViolationItems']").all()
        items = []
        for cb in checkboxes:
            cb_id = cb.get_attribute("id") or ""
            if not cb_id:
                continue
            lbl = self.page.locator(f"label[for='{cb_id}']")
            if lbl.count() > 0:
                items.append((cb_id, lbl.evaluate("el => el.innerText").strip()))
        result = self.match_violation(items, violation_data, field_label="違規項目")
        if result:
            # 點選與其 ID 綁定的 label，避開 invisible checkbox 的 click 限制
            self.page.locator(f"label[for='{result.value}']").click()
        else:
            self.violation_unmatched("input[name='ViolationItems']", violation_data, field_label="違規項目")
            
        # 8. 違規事實說明
        self.page.fill(self.config["selectors"]["violation_description"], violation_data.get("violation_description", ""))
        
        # 9. 勾選個人資料聲明同意書 (點選 label 以防攔截)
        print("  - 勾選個人資料聲明同意...")
        agree_id = self.page.locator(self.config["selectors"]["agreement_checkbox"]).get_attribute("id")
        self.page.locator(f"label[for='{agree_id}']").click()

    def upload_media(self, media_paths: List[str]) -> None:
        if not media_paths:
            print("[Changhua] 沒有提供附加媒體檔案。")
            return
            
        print(f"[Changhua] 正在上傳附加檔案 (最多 5 個槽分流): {media_paths}")
        
        # 取得設定檔中的 file input 槽名
        file_inputs = self.config["selectors"]["file_inputs"]
        valid_paths = [p for p in media_paths if os.path.exists(p)][:5]
        
        for idx, path in enumerate(valid_paths):
            if idx < len(file_inputs):
                selector = file_inputs[idx]
                print(f"  - 檔案槽 [{idx+1}] 上傳: '{os.path.basename(path)}'")
                self.page.set_input_files(selector, path)
                self.page.wait_for_timeout(500)

    def handle_verification(self) -> None:
        print("[Changhua] 提示使用者：請手動輸入圖片驗證碼並確認送出。")
        # 截圖供驗證
        try:
            screenshot_path = "C:/Users/HP/.gemini/antigravity/brain/e10a979a-58b5-40f7-8ffc-1e195403f74c/scratch/ch_filled_preview.png"
            self.page.screenshot(path=screenshot_path, full_page=True)
            print(f"[Changhua] 填寫完成之全頁面截圖已儲存: {screenshot_path}")
        except Exception as e:
            print(f"[Changhua] 儲存填表截圖失敗: {e}")
        # 觸發人機驗證 Hook
        self.trigger_hook("on_verification")

    def submit(self) -> bool:
        print("[Changhua] 自動填表完畢，安全停留在送出表單前，請使用者確認無誤後手動提交。")
        return False

    # ==================== 輔助方法 ====================

    def _format_date_hyphens(self, date_str: str) -> str:
        """將各種日期格式（如 20260706、2026/07/06）格式化為 YYYY-MM-DD"""
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
