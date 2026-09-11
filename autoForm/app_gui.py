import os
import sys
import json
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 解決 Windows 終端機 UnicodeEncodeError
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# 載入 Playwright 與 Drivers
from playwright.sync_api import sync_playwright
from drivers.kaohsiung_driver import KaohsiungDriver
from drivers.tainan_driver import TainanDriver
from drivers.pingtung_driver import PingtungDriver
from drivers.yunlin_driver import YunlinDriver
from drivers.chiayi_driver import ChiayiDriver
from drivers.chiayi_city_driver import ChiayiCityDriver
from drivers.taichung_driver import TaichungDriver
from drivers.changhua_driver import ChanghuaDriver
from drivers.nantou_driver import NantouDriver
from drivers.yilan_driver import YilanDriver
from drivers.hualien_driver import HualienDriver
from drivers.taitung_driver import TaitungDriver
from drivers.hsinchu_county_driver import HsinchuCountyDriver
from drivers.hsinchu_city_driver import HsinchuCityDriver
from drivers.penghu_driver import PenghuDriver
from drivers.keelung_driver import KeelungDriver
from drivers.taoyuan_driver import TaoyuanDriver
from drivers.new_taipei_driver import NewTaipeiDriver
from drivers.taipei_driver import TaipeiDriver
from drivers.miaoli_driver import MiaoliDriver
from plugins.human_verification_helper import HumanVerificationHelperPlugin

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.path.join(BASE_DIR, "reporter_profile.json")

class AppConsoleGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("全台交通檢舉自動填表整合控制台")
        self.root.geometry("640x580")
        self.root.resizable(True, True)
        
        # 樣式設定
        self.style = ttk.Style()
        self.style.theme_use("clam")
        self.bg_color = "#f4f6f9"
        self.primary_color = "#0056b3"
        self.root.configure(bg=self.bg_color)
        
        # 內部狀態
        self.case_path = ""
        self.case_data = {}
        self.media_files = []
        
        self.create_widgets()
        self.load_reporter_profile()

    def create_widgets(self):
        # 標題
        title_label = tk.Label(
            self.root, 
            text="交通違規檢舉自動化系統", 
            font=("Microsoft JhengHei", 14, "bold"),
            bg=self.bg_color,
            fg=self.primary_color
        )
        title_label.pack(pady=10)

        # ==================== 1. 案件選擇區 ====================
        case_frame = ttk.LabelFrame(self.root, text=" 選擇案件資料夾 ")
        case_frame.pack(padx=20, pady=5, fill="x")
        
        self.case_path_var = tk.StringVar(value="請選擇待申報案件資料夾...")
        path_label = ttk.Entry(case_frame, textvariable=self.case_path_var, font=("Microsoft JhengHei", 9), state="readonly")
        path_label.pack(side="left", padx=10, pady=8, fill="x", expand=True)
        
        self.browse_btn = ttk.Button(case_frame, text="瀏覽案件...", command=self.browse_case_folder)
        self.browse_btn.pack(side="right", padx=10, pady=8)

        # ==================== 2. 案件內容預覽區 ====================
        preview_frame = ttk.LabelFrame(self.root, text=" 案件資料預覽 ")
        preview_frame.pack(padx=20, pady=5, fill="both", expand=True)
        
        # Grid 排版
        grid_frame = tk.Frame(preview_frame, bg=self.bg_color)
        grid_frame.pack(padx=10, pady=8, fill="both", expand=True)
        grid_frame.columnconfigure(1, weight=1)
        
        preview_fields = [
            ("違規車牌：", "license_plate"),
            ("違規時間：", "violation_datetime"),
            ("違規地點：", "violation_location"),
            ("違規行政區：", "district"),
            ("違規類別：", "category"),
            ("佐證媒體：", "media"),
            ("違規敘述：", "description")
        ]
        
        self.preview_labels = {}
        for idx, (label_text, field_name) in enumerate(preview_fields):
            lbl = tk.Label(grid_frame, text=label_text, font=("Microsoft JhengHei", 9, "bold"), bg=self.bg_color, anchor="e")
            lbl.grid(row=idx, column=0, sticky="ne", padx=(0, 5), pady=4)
            
            # 使用 Text 控制項以支援多行長文字換行
            if field_name in ["description", "media", "violation_location", "category"]:
                val_txt = tk.Text(grid_frame, font=("Microsoft JhengHei", 9), bg=self.bg_color, bd=0, height=2, wrap="char")
                val_txt.grid(row=idx, column=1, sticky="ew", pady=4)
                val_txt.configure(state="disabled")
                self.preview_labels[field_name] = val_txt
            else:
                val_lbl = tk.Label(grid_frame, text="-", font=("Microsoft JhengHei", 9), bg=self.bg_color, anchor="w")
                val_lbl.grid(row=idx, column=1, sticky="w", pady=4)
                self.preview_labels[field_name] = val_lbl

        # ==================== 3. 檢舉人資料區 ====================
        profile_frame = ttk.LabelFrame(self.root, text=" 檢舉人設定 ")
        profile_frame.pack(padx=20, pady=5, fill="x")
        
        self.profile_info_var = tk.StringVar(value="尚未載入個人資料...")
        info_label = tk.Label(profile_frame, textvariable=self.profile_info_var, font=("Microsoft JhengHei", 9), fg="#555555")
        info_label.pack(side="left", padx=10, pady=8)
        
        edit_profile_btn = ttk.Button(profile_frame, text="修改資料", command=self.edit_profile)
        edit_profile_btn.pack(side="right", padx=10, pady=8)

        # ==================== 4. 執行控制區 ====================
        control_frame = tk.Frame(self.root, bg=self.bg_color)
        control_frame.pack(padx=20, pady=10, fill="x")
        
        city_label = tk.Label(control_frame, text="目標申報縣市：", font=("Microsoft JhengHei", 9, "bold"), bg=self.bg_color)
        city_label.pack(side="left", padx=(10, 5))
        
        # 縣市清單，目前高雄市完工
        self.city_combobox = ttk.Combobox(
            control_frame, 
            values=["高雄市", "台北市", "新北市", "桃園市", "苗栗縣", "台中市", "台南市", "屏東縣", "雲林縣", "嘉義縣", "嘉義市", "彰化縣", "南投縣", "宜蘭縣", "花蓮縣", "台東縣", "新竹縣", "新竹市", "澎湖縣", "基隆市"], 
            state="readonly", 
            width=10
        )
        self.city_combobox.set("高雄市")
        self.city_combobox.pack(side="left", padx=5)
        
        self.run_btn = ttk.Button(control_frame, text="啟動自動填表", command=self.start_fill_process)
        self.run_btn.pack(side="right", padx=10)

    def load_reporter_profile(self):
        """讀取檢舉人設定檔"""
        if os.path.exists(PROFILE_PATH):
            try:
                with open(PROFILE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    name = data.get("reporter_name", "未設定")
                    id_num = data.get("reporter_id", "未設定")
                    self.profile_info_var.set(f"目前檢舉人：{name} ({id_num})")
            except Exception as e:
                self.profile_info_var.set(f"讀取個人資料失敗: {e}")
        else:
            self.profile_info_var.set("找不到個人設定檔，請先建立！")

    def edit_profile(self):
        """開啟 setup_profile.py 修改個人設定"""
        try:
            import setup_profile
            # 建立子視窗
            sub_win = tk.Toplevel(self.root)
            # 將 control 移交至 setup_profile
            app = setup_profile.ProfileSetupApp(sub_win)
            sub_win.focus_set()
            # 阻塞主視窗直至設定視窗關閉，之後重新讀取設定檔
            self.root.wait_window(sub_win)
            self.load_reporter_profile()
        except Exception as e:
            messagebox.showerror("錯誤", f"無法啟動個人資料設定介面: {e}")

    def browse_case_folder(self):
        """瀏覽並選取案件資料夾"""
        initial_dir = os.path.join(BASE_DIR, "cases")
        if not os.path.exists(initial_dir):
            initial_dir = BASE_DIR
            
        selected = filedialog.askdirectory(title="選擇待檢舉案件資料夾", initialdir=initial_dir)
        if not selected:
            return
            
        self.case_path = os.path.abspath(selected)
        self.case_path_var.set(self.case_path)
        self.parse_case_data()

    def parse_case_data(self):
        """解析選取資料夾內部的 info.txt 與照片"""
        info_file = os.path.join(self.case_path, "info.txt")
        if not os.path.exists(info_file):
            messagebox.showerror("解析錯誤", f"選取的資料夾中找不到案件資訊檔案 [info.txt]！")
            return
            
        # 1. 解析 info.txt
        self.case_data = {}
        try:
            with open(info_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    self.case_data[k.strip()] = v.strip()
        except Exception as e:
            messagebox.showerror("讀取失敗", f"無法讀取 info.txt 內容:\n{e}")
            return
            
        # 2. 獲取照片檔案列表
        self.media_files = []
        try:
            for file in os.listdir(self.case_path):
                if file.lower().endswith((".jpg", ".jpeg", ".png", ".mp4")):
                    self.media_files.append(os.path.join(self.case_path, file))
        except Exception:
            pass
            
        # 3. 更新 UI 預覽標籤
        self.update_preview_ui()
        
        # 4. 根據「違規行政區」或「交通違規地點」自動推薦縣市
        location_str = self.case_data.get("交通違規地點", "") + self.case_data.get("違規行政區", "") + self.case_data.get("交通違規地點行政區", "")
        if "高雄" in location_str or "苓雅" in location_str or "三民" in location_str or "左營" in location_str:
            self.city_combobox.set("高雄市")
        elif "台南" in location_str or "永康" in location_str or "安平" in location_str or "東區" in location_str or "中西區" in location_str:
            self.city_combobox.set("台南市")
        elif "屏東" in location_str or "潮州" in location_str or "恆春" in location_str:
            self.city_combobox.set("屏東縣")
        elif "雲林" in location_str or "斗六" in location_str or "虎尾" in location_str or "西螺" in location_str:
            self.city_combobox.set("雲林縣")
        elif "台中" in location_str or "臺中" in location_str or "西屯" in location_str or "北屯" in location_str or "大里" in location_str:
            self.city_combobox.set("台中市")
        elif "彰化" in location_str or "員林" in location_str or "鹿港" in location_str or "和美" in location_str:
            self.city_combobox.set("彰化縣")
        elif "南投" in location_str or "草屯" in location_str or "埔里" in location_str or "竹山" in location_str:
            self.city_combobox.set("南投縣")
        elif "宜蘭" in location_str or "羅東" in location_str or "礁溪" in location_str or "頭城" in location_str or "蘇澳" in location_str:
            self.city_combobox.set("宜蘭縣")
        elif "花蓮" in location_str or "吉安" in location_str or "壽豐" in location_str or "新城" in location_str or "玉里" in location_str:
            self.city_combobox.set("花蓮縣")
        elif "台東" in location_str or "臺東" in location_str or "東河" in location_str or "成功" in location_str or "關山" in location_str or "池上" in location_str or "卑南" in location_str or "太麻里" in location_str or "綠島" in location_str or "蘭嶼" in location_str:
            self.city_combobox.set("台東縣")
        elif "新竹縣" in location_str or "竹北" in location_str or "竹東" in location_str or "關西" in location_str or "新埔" in location_str or "湖口" in location_str or "新豐" in location_str or "芎林" in location_str:
            self.city_combobox.set("新竹縣")
        elif "新竹市" in location_str or "香山" in location_str or "清大" in location_str or "交大" in location_str or "城隍廟" in location_str or "光復路" in location_str:
            self.city_combobox.set("新竹市")
        elif "澎湖" in location_str or "馬公" in location_str or "西嶼" in location_str or "白沙" in location_str or "湖西" in location_str or "七美" in location_str or "望安" in location_str:
            self.city_combobox.set("澎湖縣")
        elif "基隆" in location_str or "中正區" in location_str or "信義區" in location_str or "仁愛區" in location_str or "七堵" in location_str or "暖暖" in location_str or "廟口" in location_str:
            self.city_combobox.set("基隆市")
        elif "嘉義市" in location_str or "東區" in location_str or "西區" in location_str:
            self.city_combobox.set("嘉義市")
        elif "嘉義" in location_str or "民雄" in location_str or "朴子" in location_str or "太保" in location_str:
            self.city_combobox.set("嘉義縣")
        elif "台北" in location_str or "信義" in location_str:
            self.city_combobox.set("台北市")
        elif "新北" in location_str or "板橋" in location_str:
            self.city_combobox.set("新北市")
        elif "苗栗" in location_str or "頭份" in location_str or "竹南" in location_str:
            self.city_combobox.set("苗栗縣")

    def update_preview_ui(self):
        """更新預覽標籤中的內容"""
        # 更新單行 Label
        self.preview_labels["license_plate"].config(text=self.case_data.get("車牌號碼", "-"))
        
        date_str = self.case_data.get("違規日期", "")
        time_str = self.case_data.get("違規時間", "")
        self.preview_labels["violation_datetime"].config(text=f"{date_str} {time_str}".strip() or "-")
        
        self.preview_labels["district"].config(text=self.case_data.get("違規行政區") or self.case_data.get("交通違規地點行政區") or "-")
        
        # 更新多行 Text 欄位
        self._set_text_widget_content(self.preview_labels["violation_location"], self.case_data.get("交通違規地點", "-"))
        self._set_text_widget_content(self.preview_labels["category"], self.case_data.get("違規類別") or self.case_data.get("違規事實") or "-")
        self._set_text_widget_content(self.preview_labels["description"], self.case_data.get("違規事實敘述", "-"))
        
        # 佐證媒體
        media_names = [os.path.basename(m) for m in self.media_files]
        self._set_text_widget_content(self.preview_labels["media"], ", ".join(media_names) if media_names else "無附加影像檔案")

    def _set_text_widget_content(self, text_widget, text_val):
        """小工具：更新 Text 控制項內容"""
        text_widget.configure(state="normal")
        text_widget.delete("1.0", tk.END)
        text_widget.insert("1.0", text_val)
        text_widget.configure(state="disabled")

    def start_fill_process(self):
        """按下啟動按鈕"""
        # 1. 檢查檔案夾與設定檔
        if not self.case_path or not self.case_data:
            messagebox.showerror("錯誤", "請先點選「瀏覽案件...」選擇待申報案件！")
            return
            
        if not os.path.exists(PROFILE_PATH):
            messagebox.showerror("錯誤", "找不到檢舉人個人設定檔！請先點選「修改資料」建立設定。")
            return

        # 2. 鎖定按鈕避免重覆點擊
        self.run_btn.config(state="disabled")
        self.browse_btn.config(state="disabled")
        
        # 3. 開啟背景執行緒執行填表工作流
        threading.Thread(target=self.run_playwright_workflow, daemon=True).start()

    def run_playwright_workflow(self):
        """背景執行緒：啟動 Playwright 填表"""
        try:
            # 載入個人資料
            with open(PROFILE_PATH, "r", encoding="utf-8") as f:
                profile_data = json.load(f)
                
            # 整合中介欄位資料
            input_data = {
                # 檢舉人
                "reporter_name": profile_data.get("reporter_name"),
                "reporter_id": profile_data.get("reporter_id"),
                "reporter_phone": profile_data.get("reporter_phone"),
                "reporter_email": profile_data.get("reporter_email"),
                "reporter_address": profile_data.get("reporter_address"),
                
                # 案件
                "violation_date": self.case_data.get("違規日期"),
                "violation_time": self.case_data.get("違規時間"),
                "district": self.case_data.get("違規行政區") or self.case_data.get("交通違規地點行政區"),
                "violation_location": self.case_data.get("交通違規地點"),
                "license_plate": self.case_data.get("車牌號碼"),
                "car_type": self.case_data.get("違規車種類型"),
                "violation_category": self.case_data.get("違規類別"),
                "violation_subcategory": self.case_data.get("違規事實"),
                "violation_description": self.case_data.get("違規事實敘述"),
            }
            
            target_city = self.city_combobox.get()
            city_code = "kaohsiung"
            driver_class = KaohsiungDriver
            
            if target_city == "高雄市":
                city_code = "kaohsiung"
                driver_class = KaohsiungDriver
            elif target_city == "台南市":
                city_code = "tainan"
                driver_class = TainanDriver
            elif target_city == "屏東縣":
                city_code = "pingtung"
                driver_class = PingtungDriver
            elif target_city == "雲林縣":
                city_code = "yunlin"
                driver_class = YunlinDriver
            elif target_city == "嘉義縣":
                city_code = "chiayi"
                driver_class = ChiayiDriver
            elif target_city == "嘉義市":
                city_code = "chiayi_city"
                driver_class = ChiayiCityDriver
            elif target_city == "台中市":
                city_code = "taichung"
                driver_class = TaichungDriver
            elif target_city == "彰化縣":
                city_code = "changhua"
                driver_class = ChanghuaDriver
            elif target_city == "南投縣":
                city_code = "nantou"
                driver_class = NantouDriver
            elif target_city == "宜蘭縣":
                city_code = "yilan"
                driver_class = YilanDriver
            elif target_city == "花蓮縣":
                city_code = "hualien"
                driver_class = HualienDriver
            elif target_city == "台東縣":
                city_code = "taitung"
                driver_class = TaitungDriver
            elif target_city == "新竹縣":
                city_code = "hsinchu_county"
                driver_class = HsinchuCountyDriver
            elif target_city == "新竹市":
                city_code = "hsinchu_city"
                driver_class = HsinchuCityDriver
            elif target_city == "澎湖縣":
                city_code = "penghu"
                driver_class = PenghuDriver
            elif target_city == "基隆市":
                city_code = "keelung"
                driver_class = KeelungDriver
            elif target_city == "桃園市":
                city_code = "taoyuan"
                driver_class = TaoyuanDriver
            elif target_city == "新北市":
                city_code = "new_taipei"
                driver_class = NewTaipeiDriver
            elif target_city == "台北市":
                city_code = "taipei"
                driver_class = TaipeiDriver
            elif target_city == "苗栗縣":
                city_code = "miaoli"
                driver_class = MiaoliDriver
            else:
                # 降級提示
                messagebox.showwarning(
                    "尚未實作", 
                    f"目前尚未實作「{target_city}」的自動填表驅動策略。\n系統將自動降級並以「高雄市」進行表單填寫展示。"
                )
                city_code = "kaohsiung"
                driver_class = KaohsiungDriver
                
            config_path = os.path.join(BASE_DIR, "configs", f"{city_code}.json")
            
            print(f"[GUI] 啟動 Playwright 實例，策略代碼: {city_code.upper()}...")
            with sync_playwright() as p:
                # 啟動有頭模式 (必須為 False，這樣使用者才能看到畫面並輸入驗證碼)
                browser = p.chromium.launch(headless=False)
                context = browser.new_context()
                page = context.new_page()
                
                # 實例化對應縣市策略
                driver = driver_class(
                    city_code=city_code,
                    config_path=config_path,
                    context=context,
                    page=page
                )
                
                # 註冊人機協作手動認證插件
                verify_plugin = HumanVerificationHelperPlugin(mode="auto")
                driver.register_plugin(verify_plugin)
                
                # 執行生命週期工作流
                try:
                    success = driver.run_workflow(input_data, self.media_files)
                    if success:
                        messagebox.showinfo("填表完成", "表單自動填寫已結束！\n視窗將保持開啟，請確認無誤並手動送出。當您關閉瀏覽器後，程式才會結束。")
                    else:
                        messagebox.showwarning("填表結束", "自動填表流程已退出或中斷，請手動確認網頁最終狀態。當您關閉瀏覽器後，程式才會結束。")
                except Exception as err:
                    messagebox.showerror("填表中斷", f"執行過程中遭遇錯誤:\n{err}\n\n瀏覽器將保持開啟以供檢查。")
                    
                # 保持瀏覽器開啟，直到使用者關閉網頁
                try:
                    if not page.is_closed():
                        page.wait_for_event("close", timeout=0)
                except Exception:
                    pass
                    
                try:
                    browser.close()
                except Exception:
                    pass
                
        except Exception as err:
            messagebox.showerror("啟動中斷", f"瀏覽器啟動過程中遭遇錯誤:\n{err}")
        finally:
            # 恢復 UI 按鈕狀態
            self.run_btn.config(state="normal")
            self.browse_btn.config(state="normal")

def run_gui():
    root = tk.Tk()
    app = AppConsoleGUI(root)
    root.mainloop()

if __name__ == "__main__":
    run_gui()
