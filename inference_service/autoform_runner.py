"""執行組員的 autoForm（reporting/autoForm/main.py），組員的程式不用改。

用法（推論服務會自動呼叫，一般不用手動跑）：
  python autoform_runner.py <autoForm 資料夾> [--test] -- --city kaohsiung --case-dir <案件> [--profile <檔案>]

--test（測試模式）：照常開瀏覽器、填完整張表單、上傳佐證，
但「不做送出前的人機驗證、不按送出鈕」，停在送出前讓你檢查。
（驗證碼要求在填表「之前」的縣市，例如要先登入或收信驗證，仍會請你先完成驗證才能繼續填。）
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

TEST_MARK = "[測試模式]"


def _all_subclasses(cls):
    out = set()
    for sub in cls.__subclasses__():
        out.add(sub)
        out |= _all_subclasses(sub)
    return out


def _patch_test_mode():
    from drivers.base_driver import BaseDriver

    def fake_submit(self):
        print(f"{TEST_MARK} 表單已填好，依測試模式略過「送出」，沒有任何資料送到警察局。", flush=True)
        print(f"{TEST_MARK} 請在瀏覽器檢查填好的內容，看完直接關閉瀏覽器即可。", flush=True)
        return True

    for cls in {BaseDriver} | _all_subclasses(BaseDriver):
        if "submit" in cls.__dict__:
            cls.submit = fake_submit
        if "upload_media" in cls.__dict__:  # 記下「表單已經填完、上傳完」
            def upload_media(self, media, _orig=cls.__dict__["upload_media"]):
                result = _orig(self, media)
                self._af_filled = True
                return result
            cls.upload_media = upload_media
        if "handle_verification" in cls.__dict__:
            # 只略過「填完表單之後、送出之前」的驗證；
            # 填表前就要的驗證（先登入、收信驗證才看得到表單）照常請使用者完成。
            def handle_verification(self, _orig=cls.__dict__["handle_verification"]):
                if not getattr(self, "_af_filled", False):
                    return _orig(self)
                print(f"{TEST_MARK} 略過送出前的人機驗證。", flush=True)
            cls.handle_verification = handle_verification


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        sys.exit(2)
    autoform_dir = os.path.abspath(argv[0])
    rest = argv[1:]
    test = "--test" in rest[: rest.index("--")] if "--" in rest else "--test" in rest
    main_args = rest[rest.index("--") + 1:] if "--" in rest else [a for a in rest if a != "--test"]

    sys.path.insert(0, autoform_dir)
    os.chdir(autoform_dir)
    sys.argv = [os.path.join(autoform_dir, "main.py")] + main_args
    import main as autoform_main  # noqa: E402  (組員的 main.py)

    if test:
        _patch_test_mode()
        print(f"{TEST_MARK} 已啟用：會填好表單，但不會送出。", flush=True)
    autoform_main.main()


if __name__ == "__main__":
    main()
