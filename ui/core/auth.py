"""登入／註冊（本機帳號）。

- 帳號存在 ui/data/accounts.json，密碼用 PBKDF2-SHA256 加鹽雜湊，不存明碼。
- 登入成功發一組 token（存在 ui/data/sessions.json），前端放在瀏覽器裡。
- 目前只是「登入畫面」：所有帳號共用同一份檢舉資料，API 也不檢查 token。
  之後要分帳號資料時，再依 token 找出使用者、把資料夾分開即可。
- Google 登入為模擬：前端選一個帳號後呼叫 /api/auth/google，不會連到 Google。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime

from .store import DATA_DIR

ACCOUNTS_PATH = os.path.join(DATA_DIR, "accounts.json")
SESSIONS_PATH = os.path.join(DATA_DIR, "sessions.json")
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


def register(name: str, email: str, password: str) -> dict:
    name, email = (name or "").strip(), (email or "").strip().lower()
    if not name:
        raise AuthError("請輸入姓名")
    if not EMAIL_RE.match(email):
        raise AuthError("Email 格式不正確")
    if len(password or "") < 6:
        raise AuthError("密碼至少要 6 個字元")
    with _lock:
        accounts = _load(ACCOUNTS_PATH)
        if email in accounts:
            raise AuthError("這個 Email 已經註冊過了，請直接登入")
        salt = secrets.token_hex(16)
        accounts[email] = {"name": name, "salt": salt, "hash": _hash(password, salt), "provider": "password",
                           "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        _save(ACCOUNTS_PATH, accounts)
        return {"token": _new_session(email), "user": _public(email, accounts[email])}


def login(email: str, password: str) -> dict:
    email = (email or "").strip().lower()
    with _lock:
        acc = _load(ACCOUNTS_PATH).get(email)
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
