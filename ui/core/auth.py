"""登入／註冊（本機帳號）。

- 帳號存在 ui/data/accounts.json，密碼用 PBKDF2-SHA256 加鹽雜湊，不存明碼。
- 登入成功發一組 token（存在 ui/data/sessions.json），前端放在瀏覽器裡。
- 每個請求依 token 找出使用者（server.py → store.set_user），草稿／分析／案件／檢舉人資料
  都只給本人看（見 store.py 的 owns()）。
- Google 登入為模擬：前端選一個帳號後呼叫 /api/auth/google，不會連到 Google。
- 註冊要驗證 Email：先存成「待驗證」（ui/data/pending_signups.json），寄出 6 位數驗證碼，
  輸入正確才真正建立帳號。目前是開發模式：驗證碼不寄信，印在伺服器視窗並回傳給前端顯示。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime

from .store import DATA_DIR

ACCOUNTS_PATH = os.path.join(DATA_DIR, "accounts.json")
SESSIONS_PATH = os.path.join(DATA_DIR, "sessions.json")
PENDING_PATH = os.path.join(DATA_DIR, "pending_signups.json")
CODE_TTL = 10 * 60        # 驗證碼有效 10 分鐘
RESEND_COOLDOWN = 60      # 60 秒內不能重寄
MAX_ATTEMPTS = 5          # 輸錯 5 次要重寄
DEV_MODE = True           # 開發模式：不寄信，驗證碼印在伺服器視窗
_lock = threading.Lock()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    pass


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000).hex()


def _public(email, acc):
    return {"email": email, "name": acc.get("name") or email.split("@")[0],
            "provider": acc.get("provider", "password"), "created": acc.get("created")}


def _new_session(email):
    token = secrets.token_urlsafe(32)
    sessions = _load(SESSIONS_PATH)
    sessions[token] = {"email": email, "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    _save(SESSIONS_PATH, sessions)
    return token


def _code_hash(code, salt):
    return hashlib.sha256((salt + code).encode()).hexdigest()


def _send_code(email, name, code):
    """開發模式：把驗證碼印在伺服器視窗（之後要真的寄信，改這裡接 SMTP 就好）。"""
    line = "=" * 46
    print(f"\n{line}\n  📧 [開發模式] Email 驗證碼\n  收件人：{name} <{email}>\n"
          f"  驗證碼：{code}    （{CODE_TTL // 60} 分鐘內有效）\n{line}\n", flush=True)


def _issue_code(pending, email):
    """產生新驗證碼並「寄出」，回傳給前端的待驗證資訊。"""
    p = pending[email]
    code = f"{secrets.randbelow(1_000_000):06d}"
    p["code_salt"] = secrets.token_hex(8)
    p["code_hash"] = _code_hash(code, p["code_salt"])
    p["expires"] = time.time() + CODE_TTL
    p["sent_at"] = time.time()
    p["attempts"] = 0
    _save(PENDING_PATH, pending)
    _send_code(email, p["name"], code)
    res = {"pending": True, "email": email, "expires_in": CODE_TTL, "resend_in": RESEND_COOLDOWN, "dev_mode": DEV_MODE}
    if DEV_MODE:
        res["dev_code"] = code
    return res


def register(name: str, email: str, password: str) -> dict:
    """第一步：存成待驗證並寄出驗證碼（帳號在 verify_email 才建立）。"""
    name, email = (name or "").strip(), (email or "").strip().lower()
    if not name:
        raise AuthError("請輸入姓名")
    if not EMAIL_RE.match(email):
        raise AuthError("Email 格式不正確")
    if len(password or "") < 6:
        raise AuthError("密碼至少要 6 個字元")
    with _lock:
        if email in _load(ACCOUNTS_PATH):
            raise AuthError("這個 Email 已經註冊過了，請直接登入")
        pending = _load(PENDING_PATH)
        salt = secrets.token_hex(16)
        pending[email] = {"name": name, "salt": salt, "hash": _hash(password, salt),
                          "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        return _issue_code(pending, email)


def verify_email(email: str, code: str) -> dict:
    """第二步：驗證碼正確 → 建立帳號並登入。"""
    email, code = (email or "").strip().lower(), re.sub(r"\D", "", code or "")
    with _lock:
        pending = _load(PENDING_PATH)
        p = pending.get(email)
        if not p:
            raise AuthError("找不到待驗證的註冊資料，請重新註冊")
        if time.time() > p.get("expires", 0):
            raise AuthError("驗證碼已過期，請按「重新寄送」")
        if p.get("attempts", 0) >= MAX_ATTEMPTS:
            raise AuthError("輸錯太多次了，請按「重新寄送」取得新的驗證碼")
        if len(code) != 6 or not hmac.compare_digest(_code_hash(code, p["code_salt"]), p["code_hash"]):
            p["attempts"] = p.get("attempts", 0) + 1
            _save(PENDING_PATH, pending)
            left = MAX_ATTEMPTS - p["attempts"]
            raise AuthError(f"驗證碼不正確，還可以再試 {left} 次" if left else "輸錯太多次了，請按「重新寄送」")
        accounts = _load(ACCOUNTS_PATH)
        accounts[email] = {"name": p["name"], "salt": p["salt"], "hash": p["hash"], "provider": "password",
                           "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "email_verified": True}
        _save(ACCOUNTS_PATH, accounts)
        pending.pop(email)
        _save(PENDING_PATH, pending)
        print(f"  ✅ {email} 已完成 Email 驗證，帳號建立", flush=True)
        return {"token": _new_session(email), "user": _public(email, accounts[email])}


def resend_code(email: str) -> dict:
    email = (email or "").strip().lower()
    with _lock:
        pending = _load(PENDING_PATH)
        p = pending.get(email)
        if not p:
            raise AuthError("找不到待驗證的註冊資料，請重新註冊")
        wait = int(p.get("sent_at", 0) + RESEND_COOLDOWN - time.time())
        if wait > 0:
            raise AuthError(f"請等 {wait} 秒後再重新寄送")
        return _issue_code(pending, email)


def login(email: str, password: str) -> dict:
    email = (email or "").strip().lower()
    with _lock:
        acc = _load(ACCOUNTS_PATH).get(email)
        if not acc:  # 註冊了但還沒驗證：密碼對的話直接帶去驗證
            pending = _load(PENDING_PATH)
            p = pending.get(email)
            if p and hmac.compare_digest(_hash(password or "", p["salt"]), p["hash"]):
                if time.time() - p.get("sent_at", 0) < RESEND_COOLDOWN and time.time() < p.get("expires", 0):
                    return {"pending": True, "email": email, "dev_mode": DEV_MODE, "expires_in": int(p["expires"] - time.time()),
                            "resend_in": int(p["sent_at"] + RESEND_COOLDOWN - time.time())}
                return _issue_code(pending, email)
        ok = bool(acc and acc.get("hash")) and hmac.compare_digest(_hash(password or "", acc["salt"]), acc["hash"])
        if not ok:
            raise AuthError("Email 或密碼錯誤")
        return {"token": _new_session(email), "user": _public(email, acc)}


def google_login(email: str, name: str) -> dict:
    """模擬 Google 登入：帳號不存在就自動建立。"""
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError("Email 格式不正確")
    with _lock:
        accounts = _load(ACCOUNTS_PATH)
        if email not in accounts:
            accounts[email] = {"name": (name or "").strip() or email.split("@")[0], "provider": "google",
                               "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            _save(ACCOUNTS_PATH, accounts)
        return {"token": _new_session(email), "user": _public(email, accounts[email])}


def me(token: str):
    if not token:
        return None
    sess = _load(SESSIONS_PATH).get(token)
    if not sess:
        return None
    acc = _load(ACCOUNTS_PATH).get(sess["email"])
    return _public(sess["email"], acc) if acc else None


def user_email(token: str):
    """token → 帳號 Email（帳號已刪除或 token 無效則回傳 None）。"""
    if not token:
        return None
    sess = _load(SESSIONS_PATH).get(token)
    if not sess or sess["email"] not in _load(ACCOUNTS_PATH):
        return None
    return sess["email"]


def logout(token: str):
    with _lock:
        sessions = _load(SESSIONS_PATH)
        if sessions.pop(token or "", None) is not None:
            _save(SESSIONS_PATH, sessions)
    return {"ok": True}


def reset_password(email: str):
    """忘記密碼（模擬）：只檢查格式，不真的寄信。"""
    if not EMAIL_RE.match((email or "").strip()):
        raise AuthError("請先輸入正確的 Email")
    return {"ok": True, "simulated": True}
