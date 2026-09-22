import os
import json
import re
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.path.join(BASE_DIR, "reporter_profile.json")

class ProfileSetupApp:
    def __init__(self, root):
        self.root = root
        self.root.title("檢舉人個人資料設定")
        self.root.geometry("520x340")
        # 允許使用者調整視窗大小，以應對不同 DPI 縮放問題
        self.root.resizable(True, True)
        
        # 設置視窗主題與樣式
        self.style = ttk.Style()
        self.style.theme_use("clam")
        
        # 設定顏色
        self.bg_color = "#f4f6f9"
        self.primary_color = "#0056b3"
        self.text_color = "#333333"
        
        self.root.configure(bg=self.bg_color)
        
        self.create_widgets()
        self.load_existing_profile()

    def create_widgets(self):
        # 標題
        title_label = tk.Label(
            self.root, 
            text="檢舉人個人資料設定", 
            font=("Microsoft JhengHei", 14, "bold"),
            bg=self.bg_color,
            fg=self.primary_color
        )
        title_label.pack(pady=(15, 10))
        
        # 容器框 (使用 Grid 排版)
        form_frame = tk.Frame(self.root, bg=self.bg_color)
        form_frame.pack(padx=25, pady=5, fill="both", expand=True)
        
        # 欄位定義
        self.fields = [
            ("姓名 (中文真實姓名):", "reporter_name"),
            ("身分證字號 (實名制檢核):", "reporter_id"),
            ("聯絡電話 (行動電話):", "reporter_phone"),
            ("電子信箱 (收受驗證信):", "reporter_email"),
            ("聯絡地址 (文書郵寄地址):", "reporter_address"),
        ]
        
        self.entries = {}
        
        # 使用兩列 Grid 排版，節省垂直空間，避免按鈕被切掉
        for i, (label_text, field_name) in enumerate(self.fields):
            lbl = tk.Label(
                form_frame, 
                text=label_text, 
                font=("Microsoft JhengHei", 9, "bold"),
                bg=self.bg_color,
                fg=self.text_color,
                anchor="e"
            )
            lbl.grid(row=i, column=0, sticky="e", padx=(0, 10), pady=8)
            
            entry = ttk.Entry(form_frame, font=("Microsoft JhengHei", 10))
            entry.grid(row=i, column=1, sticky="ew", pady=8)
            self.entries[field_name] = entry
            
        form_frame.columnconfigure(1, weight=1)
        
        # 按鈕容器
        btn_frame = tk.Frame(self.root, bg=self.bg_color)
        btn_frame.pack(pady=(10, 15), fill="x")
        
        # 儲存按鈕
        self.save_btn = ttk.Button(
            btn_frame, 
            text="儲存設定", 
            command=self.save_profile
        )
        self.save_btn.pack(side="right", padx=(10, 25))
        
        # 取消按鈕
        self.cancel_btn = ttk.Button(
            btn_frame, 
            text="取消", 
            command=self.root.destroy
        )
        self.cancel_btn.pack(side="right")

    def load_existing_profile(self):
        """
        若檔案存在，則載入現有 Profile 設定
        """
        if os.path.exists(PROFILE_PATH):
            try:
                with open(PROFILE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for key, entry in self.entries.items():
                        if key in data:
                            entry.insert(0, data[key])
            except Exception as e:
                print(f"[GUI] 讀取舊設定失敗: {e}")

    def save_profile(self):
        """
        驗證資料並儲存至 JSON 檔案
        """
        profile_data = {}
        
        # 1. 取得並清理資料
        for key, entry in self.entries.items():
            profile_data[key] = entry.get().strip()
            
        # 2. 驗證資料格式
        # 2.1 檢查是否留空
        if not all(profile_data.values()):
            messagebox.showerror("錯誤", "所有欄位皆為必填，不可留空！")
            return
            
        # 2.2 檢查身分證字號格式
        id_pattern = re.compile(r"^[A-Z][1-2]\d{8}$")
        if not id_pattern.match(profile_data["reporter_id"].upper()):
            messagebox.showerror("錯誤", "身分證字號格式錯誤！\n必須為 1 碼大寫英文 + 1 碼性別代號(1或2) + 8 碼數字。")
            return
            
        # 2.3 檢查電話號碼
        phone_pattern = re.compile(r"^09\d{8}$")
        if not phone_pattern.match(profile_data["reporter_phone"]):
            messagebox.showerror("錯誤", "電話號碼格式錯誤！\n必須為 10 碼行動電話數字 (例: 0912345678)。")
            return
            
        # 2.4 檢查信箱格式
        email_pattern = re.compile(r"^[\w\.-]+@[\w\.-]+\.\w+$")
        if not email_pattern.match(profile_data["reporter_email"]):
            messagebox.showerror("錯誤", "電子信箱格式錯誤！")
            return
            
        # 將身分證統一轉為大寫存檔
        profile_data["reporter_id"] = profile_data["reporter_id"].upper()

        # 3. 寫入 JSON
        try:
            with open(PROFILE_PATH, "w", encoding="utf-8") as f:
                json.dump(profile_data, f, ensure_ascii=False, indent=2)
            messagebox.showinfo("成功", "個人設定資料已成功儲存！")
            self.root.destroy()
        except Exception as e:
            messagebox.showerror("儲存失敗", f"無法寫入設定檔，錯誤訊息:\n{e}")

def run_gui():
    """
    啟動 Tkinter GUI 設定視窗的主函數
    """
    root = tk.Tk()
    app = ProfileSetupApp(root)
    root.mainloop()

if __name__ == "__main__":
    run_gui()
