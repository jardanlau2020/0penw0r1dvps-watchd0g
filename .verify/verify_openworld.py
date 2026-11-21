#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Openworld 续期脚本的精简验证夹（上游整份采用 + 本仓补丁）。

为什么精简：上一版 229 条断言是围绕 renew-kit 架构写的，代码整体换掉后那套
全作废；老板要的是「主路径能证明」，不是条数好看。这里只压五条主路径：

  [1] token 预检通过 / 被吊销时能定性
  [2] Clerk 换会话成功（__session → sessioncookie）
  [3] 检测到 VPS 实例
  [4] 未到窗口静默（不发 Telegram、exit 0）
  [5] 到窗口进验证码流程（调用上游 try_renew_captcha）

外加静态接线：patchright、上游五类 solver 在位、人工路径已删、workflow 接线。

用法：python .verify/verify_openworld.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APP = ROOT / "apprenew.py"
WF = ROOT / ".github" / "workflows" / "renew-openworld.yml"
REQ = ROOT / "requirements.txt"

PASS = 0
FAIL: list = []


def ok(label, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL.append(label)
        print(f"  ❌ {label}" + (f"  << {extra}" if extra else ""))


def eq(label, got, want):
    ok(label, got == want, f"got={got!r} want={want!r}")


# ── 依赖替身：patchright/playwright/numpy/cv2/requests 都不真装 ──────────────
class _Any:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _Any()

    def __getattr__(self, name):
        return _Any()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return True


class _FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeRequests(types.ModuleType):
    def __init__(self):
        super().__init__("requests")
        self.get_calls = []
        self.next_get = None

    def get(self, url, **kw):
        self.get_calls.append((url, kw))
        return self.next_get or _FakeResp(200, {"username": "jardanlau", "id": "1"})

    def post(self, *a, **k):
        return _FakeResp(200)


def install_stubs():
    pw = types.ModuleType("patchright")
    pw_sync = types.ModuleType("patchright.sync_api")
    pw_sync.sync_playwright = lambda *a, **k: _Any()
    pw.sync_api = pw_sync
    sys.modules["patchright"] = pw
    sys.modules["patchright.sync_api"] = pw_sync

    pl = types.ModuleType("playwright")
    pl_sync = types.ModuleType("playwright.sync_api")
    pl_sync.sync_playwright = lambda *a, **k: _Any()
    pl.sync_api = pl_sync
    sys.modules["playwright"] = pl
    sys.modules["playwright.sync_api"] = pl_sync

    np = types.ModuleType("numpy")
    np.ndarray = object
    np.float32 = float
    np.uint8 = int
    np.zeros = lambda *a, **k: _Any()
    np.array = lambda *a, **k: _Any()
    np.asarray = lambda *a, **k: _Any()
    sys.modules["numpy"] = np

    cv2 = types.ModuleType("cv2")
    for name in ("cvtColor", "matchTemplate", "Canny", "findContours", "threshold",
                 "resize", "GaussianBlur", "absdiff", "inRange", "morphologyEx",
                 "getStructuringElement", "imdecode", "imencode", "warpAffine",
                 "getRotationMatrix2D", "minMaxLoc", "matchShapes", "contourArea",
                 "boundingRect", "fillPoly", "copyMakeBorder"):
        setattr(cv2, name, lambda *a, **k: _Any())
    cv2.COLOR_BGR2GRAY = 6
    cv2.TM_CCOEFF_NORMED = 5
    cv2.RETR_EXTERNAL = 0
    cv2.CHAIN_APPROX_SIMPLE = 1
    cv2.THRESH_BINARY = 0
    cv2.THRESH_OTSU = 8
    cv2.INTER_LINEAR = 1
    cv2.MORPH_OPEN = 2
    sys.modules["cv2"] = cv2

    fake_requests = _FakeRequests()
    sys.modules["requests"] = fake_requests
    return fake_requests


def load_app():
    spec = importlib.util.spec_from_file_location("apprenew_verify", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 假页面 ──────────────────────────────────────────────────────────────────
class FakeLocator:
    def __init__(self, links=None, text=""):
        self._links = links or []
        self._text = text

    @property
    def first(self):
        return self

    def count(self):
        return len(self._links)

    def all(self):
        return self._links

    def is_visible(self, timeout=0):
        return True

    def inner_text(self, timeout=0):
        return self._text

    def text_content(self):
        return self._text

    def get_attribute(self, name):
        return None

    def click(self):
        return None

    def wait_for(self, **k):
        return None

    def screenshot(self, path=None, **k):
        return None


class FakeLink:
    def __init__(self, href):
        self._href = href

    def get_attribute(self, name):
        return self._href if name == "href" else None


class FakeRequestCtx:
    def __init__(self, resp=None):
        self.calls = []
        self._resp = resp or _FakeResp(200, {"ok": True})

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self._resp


class FakePage:
    def __init__(self, links=None, body=""):
        self.url = "https://openworld.eu.org/vps/e2ce269b-b14a-4beb-a851-b439b323828f"
        self.title_ = "vps"
        self._links = links or []
        self._body = body
        self.request = FakeRequestCtx()
        self.context = FakeContext()
        self.goto_calls = []

    def goto(self, url, **k):
        self.goto_calls.append(url)
        self.url = url
        return _FakeResp(200)

    def title(self):
        return self.title_

    def locator(self, sel):
        if "a[href*='/vps/']" in sel:
            return FakeLocator(self._links)
        return FakeLocator([], self._body)

    def wait_for_timeout(self, ms):
        return None

    def content(self):
        return "<html></html>"

    def evaluate(self, js):
        if "script[src]" in js:
            return [["script", "https://openworld.eu.org/static/js/base.js"]]
        if "captcha" in js and "sel" in js:
            return [{"sel": "[class*=captcha]", "tag": "DIV", "id": "captcha_x",
                     "cls": "captcha", "html": "<div id='captcha_x'></div>"}]
        return [["__owCaptcha", "object", ""]]

    def screenshot(self, path=None, **k):
        return None

    def wait_for_cloudflare(self, *a, **k):
        return None

    def on(self, event, cb):
        return None

    def wait_for_load_state(self, *a, **k):
        return None

    def wait_for_url(self, *a, **k):
        return None

    @property
    def keyboard(self):
        return types.SimpleNamespace(press=lambda *a, **k: None,
                                     type=lambda *a, **k: None)

    @property
    def mouse(self):
        return types.SimpleNamespace(move=lambda *a, **k: None,
                                     down=lambda *a, **k: None,
                                     up=lambda *a, **k: None,
                                     click=lambda *a, **k: None)


class FakeContext:
    def __init__(self, cookies=None):
        self._cookies = cookies or []

    def cookies(self, url=None):
        return self._cookies

    def add_cookies(self, cs):
        self._cookies.extend(cs)

    def clear_cookies(self):
        self._cookies = []


# ══════════════════════════════════════════════════════════════════ [1] token
def t_token(mod, fake_requests):
    print("\n=== [1] token 预检 ===")
    fake_requests.next_get = _FakeResp(200, {"username": "jardanlau", "id": "1244122025140813867"})
    alive, info = mod.discord_token_alive("dummy")
    ok("1.1 200 → 判活", alive is True, info)
    ok("1.2 说明里带用户名", "jardanlau" in info, info)
    ok("1.3 打的是 /users/@me",
       any("users/@me" in u for u, _ in fake_requests.get_calls))

    fake_requests.next_get = _FakeResp(401, {"message": "401: Unauthorized"}, "401: Unauthorized")
    alive, info = mod.discord_token_alive("dummy")
    ok("1.4 401 → 判死（不是构造问题）", alive is False, info)
    ok("1.5 说明带状态码", "401" in info, info)

    alive, info = mod.discord_token_alive("")
    ok("1.6 空 token 直接判死", alive is False, info)


# ═══════════════════════════════════════════════════════ [2] Clerk 换会话
def t_clerk(mod):
    print("\n=== [2] Clerk 换会话 ===")
    # 情形 a：登录页 JS 自己换出 sessioncookie
    page = FakePage()
    page.context = FakeContext([
        {"name": "__session", "value": "jwt"},
        {"name": "__client_uat", "value": "1794904722"},
    ])
    state = {"polled": 0}

    def cookies(url=None):
        state["polled"] += 1
        base = [{"name": "__session", "value": "jwt"},
                {"name": "__client_uat", "value": "1794904722"}]
        if state["polled"] >= 2:
            base.append({"name": "sessioncookie", "value": "fresh"})
        return base

    page.context.cookies = cookies
    got = mod.exchange_clerk_session(page, wait_seconds=1)
    ok("2.1 登录页自动换出 sessioncookie → True", got is True)

    # 情形 b：没有 Clerk 会话，但有现成 sessioncookie
    page2 = FakePage()
    page2.context = FakeContext([{"name": "sessioncookie", "value": "ok"}])
    ok("2.2 无 Clerk 会话但已有 sessioncookie → True",
       mod.exchange_clerk_session(page2, wait_seconds=1) is True)

    # 情形 c：什么都没有
    page3 = FakePage()
    page3.context = FakeContext([])
    ok("2.3 无任何会话 → False", mod.exchange_clerk_session(page3, wait_seconds=1) is False)

    # 情形 d：cookie 注入通用拆分（含带后缀的 Clerk cookie）
    page4 = FakePage()
    page4.context = FakeContext([])
    mod.login_with_cookies(
        page4,
        "__session=a; __session_95RAEI21=b; __client_uat=1; csrf_token=c; sessioncookie=d")
    names = [c["name"] for c in page4.context.cookies()
             if c.get("domain") == "openworld.eu.org"]
    ok("2.4 通用拆分收全部 5 条", len(names) == 5, names)
    ok("2.5 带后缀的 __session_95RAEI21 也在", "__session_95RAEI21" in names, names)
    ok("2.6 Clerk cookie 另注 clerk 子域",
       any(c.get("domain") == "clerk.openworld.eu.org"
           for c in page4.context.cookies()))


# ═══════════════════════════════════════════════════════ [3] 检测实例
def t_vps(mod):
    print("\n=== [3] 检测 VPS 实例 ===")
    links = [FakeLink("/vps/e2ce269b-b14a-4beb-a851-b439b323828f"),
             FakeLink("/vps/list"),
             FakeLink("/vps/e2ce269b-b14a-4beb-a851-b439b323828f")]
    page = FakePage(links=links)
    urls = mod.get_vps_urls(page)
    eq("3.1 只留详情页（去重、排除 /vps/list）", len(urls), 1)
    ok("3.2 是完整 URL", urls and urls[0].startswith("https://openworld.eu.org/vps/"), urls)

    empty = mod.get_vps_urls(FakePage(links=[]))
    eq("3.3 没有实例时返回空", empty, [])


# ═══════════════════════════════════════════ [4][5] main 主路径
def _run_main(mod, days, threshold=5):
    """跑一次 main()，把外部依赖全换成假的，返回 (code, tg 消息, captcha 调用次数)。"""
    sent, captcha_calls = [], []

    mod.RENEW_THRESHOLD_DAYS = threshold
    mod.DISCORD_TOKEN = "dummy"
    mod.OPENWORLD_COOKIES = ""
    mod.SKIP_DISCORD = False
    mod.DUMP_DOM = False
    mod.DRY_RUN = False
    mod.TG_BOT_TOKEN = "t"
    mod.TG_CHAT_ID = "c"

    page = FakePage(links=[FakeLink("/vps/e2ce269b-b14a-4beb-a851-b439b323828f")],
                    body=f"Renews in {days} days")

    class FakeBrowser:
        def close(self):
            pass

        def new_context(self, **k):
            return types.SimpleNamespace(
                add_init_script=lambda *a, **k: None,
                new_page=lambda: page,
            )

    class FakePW:
        def __enter__(self):
            return types.SimpleNamespace(
                chromium=types.SimpleNamespace(launch=lambda **k: FakeBrowser()))

        def __exit__(self, *a):
            return False

    mod.sync_playwright = lambda *a, **k: FakePW()
    mod.login = lambda p: True
    mod.get_vps_urls = lambda p: [
        "https://openworld.eu.org/vps/e2ce269b-b14a-4beb-a851-b439b323828f"]
    mod.wait_for_cloudflare = lambda *a, **k: None
    mod.save_screenshot = lambda *a, **k: None
    mod.send_telegram_message = lambda m: sent.append(m)
    mod.check_and_handle_vps_status = lambda p: "服务器状态：正常（Running）"

    def fake_captcha(p, initial_days=0, max_attempts=12):
        captcha_calls.append((initial_days, max_attempts))
        return True

    mod.try_renew_captcha = fake_captcha
    mod.read_fail_streak = lambda: 0
    mod.write_fail_streak = lambda n: None

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = mod.main()
    return code, sent, captcha_calls, buf.getvalue()


def t_main(mod):
    print("\n=== [4] 未到窗口静默 ===")
    code, sent, calls, out = _run_main(mod, days=6)
    eq("4.1 exit 0", code, 0)
    eq("4.2 不发 Telegram", sent, [])
    eq("4.3 不进验证码流程", calls, [])
    ok("4.4 日志写「静默」", "静默" in out, out[-200:])

    print("\n=== [5] 到窗口进验证码流程 ===")
    code, sent, calls, out = _run_main(mod, days=5)
    eq("5.1 exit 0（续期成功）", code, 0)
    eq("5.2 调了一次 try_renew_captcha", len(calls), 1)
    eq("5.3 传了 initial_days=5", calls[0][0] if calls else None, 5)
    eq("5.4 单轮给足 12 次重试", calls[0][1] if calls else None, 12)
    ok("5.5 续期成功有通知", any("续期成功" in m for m in sent), sent)


# ═══════════════════════════════════════════════════════ 静态接线
def t_static():
    print("\n=== [S] 静态接线 ===")
    src = APP.read_text(encoding="utf-8")
    wf = WF.read_text(encoding="utf-8")
    req = REQ.read_text(encoding="utf-8")

    ok("S1 用 patchright（带回退）",
       "from patchright.sync_api import sync_playwright" in src)
    for name in ("_install_ws_hook", "_solve_puzzle", "_solve_rotate", "_solve_odd",
                 "_solve_match", "_drag_slider", "_human_click", "_handle_one_stage",
                 "try_renew_captcha"):
        ok(f"S2 上游 solver 在位: {name}", f"def {name}(" in src)
    ok("S3 五类验证码都在 SUPPORTED_KINDS",
       all(k in src for k in ('"puzzle"', '"key"', '"rotate"', '"odd"', '"match"')))
    ok("S4 人工路径已删（capture_renew_captcha）", "def capture_renew_captcha" not in src)
    ok("S5 人工路径已删（request_manual_renewal）", "def request_manual_renewal" not in src)
    ok("S6 人工路径已删（send_telegram_photo）", "def send_telegram_photo" not in src)
    ok("S7 保留 token 预检", "def discord_token_alive(" in src)
    ok("S8 保留 Clerk 换会话", "def exchange_clerk_session(" in src)
    ok("S9 保留 SKIP_DISCORD", "SKIP_DISCORD" in src)
    ok("S10 有 dump_dom", "def dump_dom(" in src and "DUMP_DOM" in src)
    ok("S11 有跨轮熔断", "MAX_FAIL_STREAK" in src and "read_fail_streak" in src)
    ok("S12 不硬编码凭证",
       not re.search(r"(DISCORD_TOKEN|TG_BOT_TOKEN)\s*=\s*['\"][A-Za-z0-9._-]{8,}", src))

    ok("S13 workflow cron 仍是 01:00 UTC", "cron: '0 1 * * *'" in wf)
    ok("S14 workflow 有 dump_dom 入参", "dump_dom:" in wf)
    ok("S15 workflow 装 requirements.txt", "pip install -r requirements.txt" in wf)
    ok("S16 workflow 装 patchright chromium", "patchright install" in wf)
    ok("S17 workflow 传老板的 secret 名",
       all(k in wf for k in ("secrets.DISCORD_TOKEN", "secrets.OPENWORLD_COOKIES",
                             "secrets.TG_BOT_TOKEN", "secrets.TG_CHAT_ID")))
    ok("S18 workflow 不再用 renew-kit action", "renew-kit" not in wf)
    ok("S19 workflow 上传 screenshots/", "screenshots/" in wf)

    ok("S20 requirements 是上游那四个",
       all(p in req for p in ("patchright==", "requests==", "numpy==",
                              "opencv-python-headless==")))
    ok("S21 404/登录页判为会话失效（不是实例不存在）",
       'logged_out' in src and '"/404"' in src and '"/login"' in src)


def main():
    print("Openworld 续期脚本 · 精简验证夹")
    print(f"  应用: {APP}")
    fake_requests = install_stubs()
    mod = load_app()
    t_token(mod, fake_requests)
    t_clerk(mod)
    t_vps(mod)
    t_main(mod)
    t_static()

    print("\n" + "=" * 60)
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败 / 共 {PASS + len(FAIL)}")
        for f in FAIL:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部通过（{PASS} 项断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
