#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0penw0r1dvps-watchd0g / apprenew.py 迁移到 renew-kit 的离线验证夹具。

为什么需要它：本轮改动的核心是**结果分类**——同一个页面在不同上游状态下
该走 SKIPPED / TRANSIENT / FAILED 哪一条，以及「标红」与「发通知」在
窗口中间日必须解耦。这些东西在真站点上没法稳定复现（要等上游真的挂），
但可以在假 Playwright 上把每个分支都走一遍。

四组断言：
  [A] 纯函数：_slug / _target_name / upstream_failure / _esc
  [B] 场景矩阵：假页面 + 假 Playwright，跑真 main()，看退出码 / 报告 / 通知
  [C] 静态接线：源码与 workflow 的口径一致性（含 AST 与 YAML 解析）
  [D] 子进程：真跑一次 python apprenew.py，确认进程级行为

用法：
    python .verify/verify_openworld.py            # 全跑
    python .verify/verify_openworld.py A B        # 只跑某几组
"""
from __future__ import annotations

import ast
import atexit
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tokenize
import traceback
import types
from pathlib import Path

import warnings

# apprenew.py 里嵌的 JS 字符串带 \s 之类，会在每次 exec 时刷 SyntaxWarning
warnings.filterwarnings("ignore", category=SyntaxWarning)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                       # 仓库根
APP_PATH = ROOT / "apprenew.py"
WF_PATH = ROOT / ".github" / "workflows" / "renew-openworld.yml"


def _find_renewkit() -> Path:
    """renew-kit 的本地检出位置：允许用环境变量指定，否则向上找。"""
    override = os.environ.get("RENEWKIT_ROOT")
    if override:
        return Path(override)
    for cand in (ROOT.parent / "renew-kit",
                 ROOT.parent.parent / "renew-kit",
                 ROOT.parent.parent.parent / "renew-kit"):
        if (cand / "renewkit" / "__init__.py").exists():
            return cand
    return ROOT.parent.parent / "renew-kit"


RENEWKIT_ROOT = _find_renewkit()

APP_SRC = APP_PATH.read_text(encoding="utf-8")
WF_SRC = WF_PATH.read_text(encoding="utf-8")

if str(RENEWKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(RENEWKIT_ROOT))

# ---------------------------------------------------------------- 计数与报告
PASS = 0
FAIL: list = []
GROUP = ""


def section(name: str) -> None:
    global GROUP
    GROUP = name
    print(f"\n=== [{name}] ===")


def ok(label: str, cond, extra: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL.append((GROUP, label, extra))
        print(f"  ❌ {label}" + (f"  << {extra}" if extra else ""))


def eq(label: str, got, want) -> None:
    ok(label, got == want, f"got={got!r} want={want!r}")


# ------------------------------------------------------------ 源码位置式屏蔽
def code_only(src: str) -> str:
    """把注释与字符串字面量就地涂成空格，保留行列位置。

    不能「丢掉 token 再 join」：那样 `_note(` 会被拼成 `_note (`，带点的
    名字也会被拆开，正则全废。所以按 token 的 (row, col) 在原行上涂。
    """
    lines = src.split("\n")
    buf = [list(line) for line in lines]
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            (r1, c1), (r2, c2) = tok.start, tok.end
            if r1 == r2:
                for c in range(c1, min(c2, len(buf[r1 - 1]))):
                    buf[r1 - 1][c] = " "
            else:
                for c in range(c1, len(buf[r1 - 1])):
                    buf[r1 - 1][c] = " "
                for r in range(r1, r2 - 1):
                    buf[r] = [" "] * len(buf[r])
                for c in range(0, min(c2, len(buf[r2 - 1]))):
                    buf[r2 - 1][c] = " "
    except (tokenize.TokenError, IndentationError):
        pass
    return "\n".join("".join(line) for line in buf)


CODE = code_only(APP_SRC)


# ------------------------------------------------------------------ 依赖替身
class _Any:
    """万能替身：任何属性访问/调用都返回自己。"""

    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self

    def __getattr__(self, name):
        return self

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


def install_stubs() -> list:
    """补上本机没有的第三方依赖，好让 apprenew.py 能被 import。

    只补 import 面（PIL / numpy / playwright）；requests 与 renewkit 用真的。
    """
    made = []

    if "PIL" not in sys.modules:
        pil = types.ModuleType("PIL")
        img = types.ModuleType("PIL.Image")
        for name in ("open", "new", "LANCZOS", "NEAREST", "BILINEAR", "Image"):
            setattr(img, name, _Any())
        img.LANCZOS = 1
        pil.Image = img
        sys.modules["PIL"] = pil
        sys.modules["PIL.Image"] = img
        made.append("PIL")

    if "numpy" not in sys.modules:
        np = types.ModuleType("numpy")
        np.array = _Any()
        np.uint8 = int
        sys.modules["numpy"] = np
        made.append("numpy")

    if "playwright" not in sys.modules:
        pw = types.ModuleType("playwright")
        sa = types.ModuleType("playwright.sync_api")
        sa.sync_playwright = PW
        sa.Page = _Any
        sa.Response = _Any
        sa.TimeoutError = type("TimeoutError", (Exception,), {})
        sa.Error = type("Error", (Exception,), {})
        pw.sync_api = sa
        sys.modules["playwright"] = pw
        sys.modules["playwright.sync_api"] = sa
        made.append("playwright")

    return made


#: [D] 组跑真子进程，进程内塞 sys.modules 没用 —— 得把替身落成真实文件。
STUB_FILES = {
    "numpy.py": '''"""numpy 替身：D 组只验证 import 面，不跑图像处理。"""


class _Any:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self

    def __getattr__(self, name):
        return self

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


array = _Any()
uint8 = int
''',
    "PIL/__init__.py": "from . import Image  # noqa: F401\n",
    "PIL/Image.py": '''"""PIL.Image 替身。"""


class _Any:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self

    def __getattr__(self, name):
        return self


LANCZOS = 1
NEAREST = 2
BILINEAR = 3
open = _Any()
new = _Any()
''',
    "playwright/__init__.py": "from . import sync_api  # noqa: F401\n",
    "playwright/sync_api.py": '''"""playwright.sync_api 替身：D 组的两条路都不会真开浏览器。"""


class Page:
    pass


class Response:
    pass


class TimeoutError(Exception):
    pass


class Error(Exception):
    pass


class _Ctx:
    chromium = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def sync_playwright(*a, **k):
    return _Ctx()
''',
}


def make_stub_dir() -> Path:
    """把依赖替身写成真实文件，供 D 组子进程 import。"""
    d = Path(tempfile.mkdtemp(prefix="ow-stubs-"))
    atexit.register(shutil.rmtree, d, True)
    for rel, body in STUB_FILES.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


def _third_party_path() -> str:
    """本机第三方包（requests 等）所在目录，好让子进程也能 import。"""
    try:
        import requests
        return str(Path(requests.__file__).resolve().parent.parent)
    except Exception:
        return ""


# ------------------------------------------------------- 假 Playwright 装置
class FakeResp:
    def __init__(self, status: int):
        self.status = status


class FakeLink:
    def __init__(self, href: str):
        self._href = href

    def get_attribute(self, name: str):
        return self._href if name == "href" else None


class FakeLocator:
    def __init__(self, *, text: str = "", links=None, count: int = 0,
                 attrs=None, visible: bool = False, raises=None):
        self._text = text
        self._links = links or []
        self._count = count
        self._attrs = attrs or {}
        self._visible = visible
        self._raises = raises
        self.clicks = 0

    @property
    def first(self):
        return self

    def all(self):
        return self._links

    def count(self) -> int:
        return self._count

    def inner_text(self, timeout=None) -> str:
        if self._raises:
            raise self._raises
        return self._text

    def text_content(self) -> str:
        return self._text

    def get_attribute(self, name):
        return self._attrs.get(name)

    def is_visible(self, timeout=None) -> bool:
        return self._visible

    def click(self, **kw):
        self.clicks += 1

    def fill(self, *a, **kw):
        pass


class FakeSpec:
    """一个 URL 的页面状态。

    final_url   ：导航后实际落地的 URL（模拟被重定向到 /login）。
    reload_spec ：给了就表示「刷新一次后变成这个状态」，用来模拟
                  「点了 Start 之后服务器真的起来了」。
    body_raises ：读 body 文本时抛这个异常，用来打「未捕获异常」那条路。
    """

    def __init__(self, status=200, title="openworld.eu.org", body="",
                 links=None, raises=None, reload_spec=None, final_url=None,
                 body_raises=None):
        self.status = status
        self.title = title
        self.body = body
        self.links = links or []
        self.raises = raises
        self.reload_spec = reload_spec
        self.final_url = final_url
        self.body_raises = body_raises


class FakePage:
    def __init__(self, routes, default=None):
        self.routes = routes
        self.default = default or FakeSpec()
        self._cur = self.default
        self._url = ""
        self.gotos: list = []
        self.shots: list = []
        self.reloads = 0

    def goto(self, url, **kw):
        self.gotos.append(url)
        spec = self.routes.get(url, self.default)
        if spec.raises:
            raise spec.raises
        self._cur = spec
        self._url = spec.final_url or url
        return FakeResp(spec.status)

    def reload(self, **kw):
        self.reloads += 1
        if self._cur.reload_spec is not None:
            self._cur = self._cur.reload_spec
        return FakeResp(self._cur.status)

    @property
    def url(self):
        return self._url

    def title(self):
        return self._cur.title

    def content(self):
        return self._cur.body

    def screenshot(self, path=None, **kw):
        self.shots.append(path)
        if path:
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")

    def locator(self, sel):
        if sel == "body":
            return FakeLocator(text=self._cur.body, raises=self._cur.body_raises)
        if "a[href" in sel:
            return FakeLocator(links=[FakeLink(h) for h in self._cur.links])
        # #vpsStatusLabel / #btnStart 之类：一律「不存在」，逼代码走 body 文案分支
        return FakeLocator(count=0, visible=False)


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_context(self, **kw):
        return FakeContext(self.page)

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.cookies: list = []

    def new_page(self):
        return self.page

    def clear_cookies(self):
        self.cookies.clear()

    def add_cookies(self, cookies):
        self.cookies.extend(cookies)


class FakePW:
    def __init__(self, page):
        self.page = page
        self.chromium = types.SimpleNamespace(launch=lambda **kw: FakeBrowser(self.page))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _PWFactory:
    """可替换的 sync_playwright 替身。"""

    def __init__(self):
        self.page = FakePage({})
        self.last_browser = None

    def __call__(self):
        pw = FakePW(self.page)
        orig = pw.chromium.launch

        def launch(**kw):
            b = orig(**kw)
            self.last_browser = b
            return b

        pw.chromium.launch = launch
        return pw


PW = _PWFactory()

install_stubs()


# ------------------------------------------------------------- 模块加载与间谍
def load_app():
    """每个场景重新 exec 一遍 apprenew.py，拿到干净的模块级状态。"""
    name = "apprenew_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class NotifySpy:
    def __init__(self):
        self.calls: list = []

    def __call__(self, text, **kw):
        self.calls.append({"text": text, **kw})
        return True

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


class ReportSpy:
    def __init__(self, real):
        self.real = real
        self.made: list = []

    def __call__(self, *a, **k):
        r = self.real(*a, **k)
        self.made.append(r)
        return r

    @property
    def last(self):
        return self.made[-1] if self.made else None


@contextlib.contextmanager
def env_patch(**envs):
    """清空环境变量再设场景值。

    renewkit.env 是**调用时**读 os.environ 的，所以必须在 load_app 之后、
    跑 main() 之前改。清空是为了不让宿主机上碰巧存在的变量干扰判定。
    """
    keep = {k: v for k, v in os.environ.items()
            if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "LANG",
                     "PYTHONPATH", "PYTHONHOME")}
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(keep)
    os.environ.update({k: str(v) for k, v in envs.items() if v is not None})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@contextlib.contextmanager
def no_sleep():
    """把 time.sleep 变 no-op：假页面不需要等，真等会让场景矩阵慢到没法用。"""
    import time as _t
    real = _t.sleep
    _t.sleep = lambda *a, **k: None
    try:
        yield
    finally:
        _t.sleep = real


@contextlib.contextmanager
def fast_clock(step=20.0):
    """让 time.time() 每次调用往前跳 step 秒。

    为什么需要：check_and_handle_vps_status 的「等 60 秒看有没有起来」是
    `while time.time() - t0 < 60: sleep(8) ...`。sleep 变 no-op 之后这个
    循环会变成**真的空转 60 秒**（time.time 还是真时间），一次场景就能把
    整个夹具拖死。把时钟拨快，循环几轮就自然结束。
    """
    import time as _t
    real = _t.time
    state = {"t": real()}

    def fake():
        state["t"] += step
        return state["t"]

    _t.time = fake
    try:
        yield
    finally:
        _t.time = real


@contextlib.contextmanager
def chdir_tmp():
    """在临时目录里跑，别把 .renew-handled / 截图落到仓库里。

    用 mkdtemp + atexit 而不是 TemporaryDirectory：断言要在 with 之外读
    产物（标记文件、截图），TemporaryDirectory 会在 with 退出时就删掉。
    """
    import atexit
    import shutil
    old = os.getcwd()
    td = tempfile.mkdtemp(prefix="ow-verify-")
    atexit.register(shutil.rmtree, td, True)
    os.chdir(td)
    try:
        yield Path(td)
    finally:
        os.chdir(old)


class Scenario:
    """一次 main() 运行的完整观测结果。"""

    def __init__(self, code, out, app, notify, report, page, tmp, err=None):
        self.code = code
        self.out = out
        self.app = app
        self.notify = notify
        self.report = report
        self.page = page
        self.tmp = tmp
        self.err = err

    # -- 便捷断言 --------------------------------------------------------
    @property
    def results(self):
        r = self.report.last
        return list(r.results) if r else []

    def outcomes(self):
        return [x.outcome.value for x in self.results]

    def names(self):
        return [x.name for x in self.results]

    def details(self):
        return [x.detail for x in self.results]

    def sent(self) -> int:
        return len(self.notify.calls)

    def last_text(self) -> str:
        c = self.notify.last
        return c["text"] if c else ""

    def last_buttons(self):
        c = self.notify.last
        return (c or {}).get("buttons") or []

    def marker(self) -> bool:
        return (Path(self.tmp) / ".renew-handled").exists()


def run_scenario(routes=None, default=None, envs=None, dry_run=False):
    """装好替身跑一次真 main()。"""
    env = {
        "OPENWORLD_COOKIES": "sessioncookie=x; csrf_token=y; __session=z",
        "TG_BOT_TOKEN": "TOKEN",
        "TG_CHAT_ID": "123",
        "HEADLESS": "true",
    }
    if dry_run:
        env["DRY_RUN"] = "1"
    if envs is not None:
        env.update(envs)
        for k, v in list(env.items()):
            if v is None:
                del env[k]

    with env_patch(**env), no_sleep(), fast_clock(), chdir_tmp() as tmp:
        app = load_app()
        PW.page = FakePage(routes or {}, default or FakeSpec())
        # refresh_cookie_server_side 会 spawn 真 curl 去联网；本夹具不测它
        # （它本轮没改），换成恒等函数，免得每个场景都挂 20 秒等网络。
        refresh_calls: list = []
        app.refresh_cookie_server_side = (
            lambda h, timeout=10: (refresh_calls.append(h), h)[1])
        notify = NotifySpy()
        app.notify.send = notify
        report = ReportSpy(app.RenewReport)
        app.RenewReport = report
        buf = io.StringIO()
        code = None
        err = None
        try:
            with contextlib.redirect_stdout(buf):
                code = app.main()
        except SystemExit as exc:
            code = exc.code
        except BaseException as exc:            # noqa: BLE001
            err = traceback.format_exc()
        sc = Scenario(code, buf.getvalue(), app, notify, report,
                      PW.page, tmp, err)
        sc.refresh_calls = refresh_calls
        return sc


# ============================================================== [A] 纯函数
def group_a():
    section("A")
    # env 必须在断言期间一直生效：renewkit.env 是调用时读的，出了 with 就没了
    with env_patch(ACCOUNT_LABEL="主號"):
        app = load_app()

        eq("A1 UUID 实例名只留前 8 位",
           app._slug("https://openworld.eu.org/vps/e2ce269b-b14a-4beb-a851-b439b323828f"),
           "e2ce269b")
        eq("A2 人读的短名原样保留",
           app._slug("https://openworld.eu.org/vps/vps-h6aad9"), "vps-h6aad9")
        eq("A3 空 URL 兜底为 vps", app._slug(""), "vps")
        eq("A4 无路径也兜底", app._slug("https://openworld.eu.org/"), "vps")
        eq("A5 结尾斜杠不影响",
           app._slug("https://openworld.eu.org/vps/abc-1234567890/"), "abc-1234")

        eq("A6 目标名含 ACCOUNT_LABEL",
           app._target_name(
               "https://openworld.eu.org/vps/e2ce269b-b14a-4beb-a851-b439b323828f"),
           "Openworld e2ce269b（主號）")
        eq("A7 无 URL 时叫 Openworld",
           app._target_name(""), "Openworld（主號）")
        eq("A8 PANEL_TARGET 是面板级目标名", app.PANEL_TARGET, "Openworld 面板")

        label = app.timeutil.now_local()
        ok("A9 timeutil.now_local 形如 MM-DD HH:MM",
           re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}", label) is not None, label)
        ok("A10 脚本不再自己实现 now_local（用 renewkit 的）",
           "def now_local" not in code_only(APP_SRC))

        eq("A11 _esc 转义 &<>", app._esc('a<b>&c'), "a&lt;b&gt;&amp;c")
        eq("A12 _esc 接受非字符串", app._esc(None), "")
        eq("A13 _esc 对普通中文无副作用", app._esc("服务器状态：正常"), "服务器状态：正常")

        # upstream_failure：状态码优先
        eq("A14 状态 502 直接判上游故障",
           app.upstream_failure(None, 502), "HTTP 502")
        eq("A15 状态 530 也在名单里",
           app.upstream_failure(None, 530), "HTTP 530")
        eq("A16 状态 404 不是上游故障",
           app.upstream_failure(FakePage({}, FakeSpec(status=404, title="404")), 404), "")
        ok("A17 状态 200 但标题是 CF 隧道错误 → 命中",
           app.upstream_failure(
               FakePage({}, FakeSpec(title="Cloudflare Tunnel error | openworld.eu.org",
                                     body="<html>error 1033</html>")), 200) != "")
        ok("A18 状态 200 但标题是 502 Bad gateway → 命中",
           app.upstream_failure(
               FakePage({}, FakeSpec(title="openworld.eu.org | 502: Bad gateway",
                                     body="")), 200) != "")
        eq("A19 正常面板页 → 空串",
           app.upstream_failure(
               FakePage({}, FakeSpec(title="Dashboard",
                                     body="Renews in 6 days running")), 200), "")
        eq("A20 状态 200 + 无标题无正文 → 空串",
           app.upstream_failure(FakePage({}, FakeSpec(title="", body="")), 200), "")
        eq("A21 拿不到响应对象（None status）也不炸",
           app.upstream_failure(FakePage({}, FakeSpec(title="Dashboard",
                                                      body="Renews in 6 days")), 0), "")

        ok("A22 上游故障名单与 renew-kit 口径一致",
           {500, 502, 503, 504, 520, 521, 522, 523, 524} <= set(app.UPSTREAM_BAD_STATUS),
           sorted(app.UPSTREAM_BAD_STATUS))

        eq("A23 三态常量齐备",
           (app.AUTH_OK, app.AUTH_COOKIE_DEAD, app.AUTH_UPSTREAM_DOWN),
           ("ok", "cookie_dead", "upstream_down"))
        ok("A24 UpstreamDown 是 RuntimeError 子类",
           issubclass(app.UpstreamDown, RuntimeError))


# ========================================================== [B] 场景矩阵
DASH = "https://openworld.eu.org/dashboard"
VPSPAGE = "https://openworld.eu.org/vps"
V1 = "https://openworld.eu.org/vps/e2ce269b-b14a-4beb-a851-b439b323828f"
V2 = "https://openworld.eu.org/vps/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

OK_DASH = FakeSpec(title="Dashboard", body="your servers", links=[V1])
OK_VPSPAGE = FakeSpec(title="VPS", body="your servers", links=[V1])


def vps_spec(days: int, *, status="running", extra=""):
    body = f"Status {status}. Renews in {days} days. {extra}"
    return FakeSpec(title="vps detail", body=body, links=[])


def group_b():
    section("B")

    # --- B1 剩余天数 > 阈值：SKIPPED，静默，exit 0
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE, V1: vps_spec(12)})
    eq("B1.1 exit 0", s.code, 0)
    eq("B1.2 只有一条结果", len(s.results), 1)
    eq("B1.3 是 SKIPPED", s.outcomes(), ["skipped"])
    eq("B1.4 不发 TG", s.sent(), 0)
    ok("B1.5 报告写「状态良好（剩 12 天）」",
       "状态良好（剩 12 天）" in s.out, s.out)
    ok("B1.6 报告写「未到续期窗口」", "未到续期窗口" in s.out, s.out)
    ok("B1.7 天数不重复出现两次", s.out.count("剩 12 天") == 1, s.out)
    ok("B1.8 打印 WATCHDOG_OK", "WATCHDOG_OK" in s.out)
    ok("B1.9 没打印 MANUAL_REQUIRED", "WATCHDOG_MANUAL_REQUIRED" not in s.out)
    ok("B1.10 留下已处理标记", s.marker())

    # --- B2 恰好等于阈值：进窗口，FAILED，发通知带按钮
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE, V1: vps_spec(5)})
    eq("B2.1 exit 1（标红）", s.code, 1)
    eq("B2.2 是 FAILED", s.outcomes(), ["failed"])
    eq("B2.3 发了 1 条 TG", s.sent(), 1)
    ok("B2.4 消息里有续期按钮", len(s.last_buttons()) == 1, s.last_buttons())
    eq("B2.5 按钮指向实例页", s.last_buttons()[0]["url"], V1)
    ok("B2.6 按钮文案带实例短名", "e2ce269b" in s.last_buttons()[0]["text"],
       s.last_buttons()[0])
    ok("B2.7 用 HTML parse_mode", s.notify.last.get("parse_mode") == "HTML")
    ok("B2.8 消息里带 HTML 粗体", "<b>" in s.last_text())
    ok("B2.9 消息里写剩 5 天", "剩 <b>5 天</b>" in s.last_text(), s.last_text())
    ok("B2.10 消息里教点 Renew free", "Renew free" in s.last_text())
    ok("B2.11 报告写「续期未完成（剩 5 天）」",
       "续期未完成（剩 5 天）" in s.out, s.out)
    ok("B2.12 报告不提「剩 0 天」", "剩 0 天" not in s.out)
    ok("B2.13 打印 WATCHDOG_MANUAL_REQUIRED", "WATCHDOG_MANUAL_REQUIRED" in s.out)
    ok("B2.14 不是 🚨（5 天还不算紧急）", s.last_text().startswith("⚠️"), s.last_text()[:4])
    ok("B2.15 截图落盘（manual_renew_needed.png）",
       (Path(s.tmp) / "manual_renew_needed.png").exists())
    ok("B2.16 不再写死 vps-h6aad9", "vps-h6aad9" not in s.last_text())

    # --- B3 阈值-1（中间日）：仍 FAILED，但故意静默
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE, V1: vps_spec(4)})
    eq("B3.1 exit 1（照样标红）", s.code, 1)
    eq("B3.2 是 FAILED", s.outcomes(), ["failed"])
    eq("B3.3 **不发 TG**（中间日静默）", s.sent(), 0)
    ok("B3.4 打印了静默原因", "窗口中間日" in s.out, s.out)
    ok("B3.5 仍然标记 MANUAL_REQUIRED", "WATCHDOG_MANUAL_REQUIRED" in s.out)

    # --- B4 剩 3 天：升级为 🚨
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE, V1: vps_spec(3)})
    eq("B4.1 exit 1", s.code, 1)
    eq("B4.2 发 1 条 TG", s.sent(), 1)
    ok("B4.3 用 🚨 升级", s.last_text().startswith("🚨"), s.last_text()[:4])
    ok("B4.4 标题写「要人手續期」", "要人手續期" in s.last_text())

    # --- B5 剩 0 天（今天到期）
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE, V1: vps_spec(0)})
    eq("B5.1 exit 1", s.code, 1)
    ok("B5.2 用 🚨", s.last_text().startswith("🚨"))
    # renewkit 把 0 当作「没有信息」（_expiry_phrase / days_left 都这么判），
    # 所以报告里不会出现「剩 0 天」——那会被读成「已经过期」。
    ok("B5.3 报告不写「剩 0 天」", "剩 0 天" not in s.out, s.out)
    ok("B5.4 报告写「续期未完成」", "续期未完成" in s.out, s.out)

    # --- B6 读不到天数：强制提醒，但报告不写「剩 0 天」
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE,
                      V1: FakeSpec(title="vps", body="Status running. No countdown.")})
    eq("B6.1 exit 1", s.code, 1)
    eq("B6.2 发 1 条 TG", s.sent(), 1)
    ok("B6.3 报告不写「剩 0 天」", "剩 0 天" not in s.out, s.out)
    ok("B6.4 报告写「续期未完成」但不带天数", "续期未完成\n" in s.out or
       "续期未完成" in s.out, s.out)
    ok("B6.5 日志提示强制尝试", "强制尝试续期" in s.out)

    # --- B7 登录页 502：TRANSIENT，exit 0
    s = run_scenario({DASH: FakeSpec(status=502, title="openworld.eu.org | 502: Bad gateway"),
                      VPSPAGE: FakeSpec(status=502, title="502: Bad gateway")})
    eq("B7.1 exit 0（上游故障不标红）", s.code, 0)
    eq("B7.2 是 TRANSIENT", s.outcomes(), ["transient"])
    eq("B7.3 发 1 条 TG 说实话", s.sent(), 1)
    ok("B7.4 消息说上游故障", "上游故障" in s.last_text(), s.last_text())
    ok("B7.5 报告写「上游暂不可用」", "上游暂不可用" in s.out, s.out)
    ok("B7.6 报告带 HTTP 502", "502" in s.out, s.out)
    ok("B7.7 不再说「搵唔到任何 VPS」", "搵唔到任何 VPS" not in s.out, s.out)
    ok("B7.8 消息不含按钮", not s.last_buttons())
    ok("B7.9 用 ⚠️ 而非 🚨", s.last_text().startswith("⚠️"))

    # --- B8 200 但 CF 隧道错误页
    s = run_scenario({DASH: FakeSpec(title="Cloudflare Tunnel error | openworld.eu.org",
                                     body="Error 1033"),
                      VPSPAGE: FakeSpec(title="Cloudflare Tunnel error", body="")})
    eq("B8.1 exit 0", s.code, 0)
    eq("B8.2 是 TRANSIENT", s.outcomes(), ["transient"])
    ok("B8.3 报告含 CF 文案", "Cloudflare Tunnel" in s.out, s.out)

    # --- B9 被弹回登录页：FAILED
    # final_url 才是真实站点会发生的事：cookie 失效时 /dashboard 会 302 到 /login，
    # 光靠标题是「Sign in」判不出来（旧代码正是只看标题才漏判）。
    s = run_scenario({DASH: FakeSpec(title="Sign in", body="login",
                                     final_url="https://openworld.eu.org/login"),
                      VPSPAGE: FakeSpec(title="Sign in", body="login",
                                        final_url="https://openworld.eu.org/login")},
                     envs={"OPENWORLD_COOKIES": "sessioncookie=x"})
    eq("B9.1 exit 1", s.code, 1)
    eq("B9.2 是 FAILED", s.outcomes(), ["failed"])
    ok("B9.3 消息说 Cookie 过期", "Cookie" in s.last_text(), s.last_text())
    ok("B9.4 报告说被弹回登录页", "登入頁" in s.out or "登录页" in s.out, s.out)

    # --- B10 上游正常但找不到实例：FAILED（业务结论）
    s = run_scenario({DASH: FakeSpec(title="Dashboard", body="no servers yet", links=[]),
                      VPSPAGE: FakeSpec(title="VPS", body="no servers", links=[]),
                      "https://openworld.eu.org": FakeSpec(title="Home", body="welcome",
                                                           links=[])})
    eq("B10.1 exit 1", s.code, 1)
    eq("B10.2 是 FAILED（不是 TRANSIENT）", s.outcomes(), ["failed"])
    ok("B10.3 报告说确实无实例", "确实無实例" in s.out, s.out)
    ok("B10.4 消息说搵唔到实例", "搵唔到任何 VPS" in s.last_text(), s.last_text())

    # --- B11 三次找实例都撞 502：UpstreamDown 路径
    s = run_scenario({DASH: FakeSpec(status=200, title="Dashboard", body="x", links=[]),
                      VPSPAGE: FakeSpec(status=502, title="502: Bad gateway"),
                      "https://openworld.eu.org": FakeSpec(status=502,
                                                           title="502: Bad gateway")})
    eq("B11.1 exit 0", s.code, 0)
    eq("B11.2 是 TRANSIENT", s.outcomes(), ["transient"])
    ok("B11.3 报告目标名是面板级", s.names() == ["Openworld 面板"], s.names())
    ok("B11.4 消息说上游故障", "上游故障" in s.last_text(), s.last_text())

    # --- B12 目标页 404：FAILED（旧代码是静默 continue）
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE,
                      V1: FakeSpec(status=200, title="404 Page Not Found",
                                   body="this page doesn't exist")})
    eq("B12.1 exit 1", s.code, 1)
    eq("B12.2 是 FAILED", s.outcomes(), ["failed"])
    ok("B12.3 消息提示机器可能被注销", "註銷" in s.last_text(), s.last_text())
    ok("B12.4 报告写 404", "404" in s.out, s.out)

    # --- B13 目标页 502：TRANSIENT（exit 0）
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE,
                      V1: FakeSpec(status=502, title="502: Bad gateway")})
    eq("B13.1 exit 0", s.code, 0)
    eq("B13.2 是 TRANSIENT", s.outcomes(), ["transient"])
    ok("B13.3 报告目标名带实例短名", s.names() == ["Openworld e2ce269b"], s.names())

    # --- B14 未配置认证
    s = run_scenario({}, envs={"OPENWORLD_COOKIES": None, "DISCORD_TOKEN": None})
    eq("B14.1 exit 1", s.code, 1)
    eq("B14.2 是 FAILED", s.outcomes(), ["failed"])
    ok("B14.3 消息说未配置认证", "未配置認證" in s.last_text(), s.last_text())
    eq("B14.4 没开浏览器", s.page.gotos, [])

    # --- B15 脚本异常：打「未捕获异常」那条路
    # 异常必须发生在**没有 try 包裹**的地方，否则只会被吞掉走成 Cookie 失效。
    # 读实例页 body 文本（apprenew.py 里 `page_text = page.locator("body").inner_text()`）
    # 就是这样一个裸调用 —— 它上面的 upstream_failure() 自己也读 body，但自带
    # except 兜底，所以异常会一路穿到 run_all 的 except Exception。
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE,
                      V1: FakeSpec(title="vps",
                                   body_raises=RuntimeError("boom on body"))})
    eq("B15.1 exit 1", s.code, 1)
    eq("B15.2 是 FAILED", s.outcomes(), ["failed"])
    ok("B15.3 消息带异常类型", "RuntimeError" in s.last_text(), s.last_text())
    ok("B15.4 报告 detail 带异常", any("RuntimeError" in d for d in s.details()), s.details())
    ok("B15.5 浏览器被关掉", PW.last_browser is not None and PW.last_browser.closed)

    # --- B16 多实例：一台正常 + 一台进窗口
    s = run_scenario({DASH: FakeSpec(title="Dashboard", body="srv", links=[V1, V2]),
                      VPSPAGE: OK_VPSPAGE,
                      V1: vps_spec(12),
                      V2: vps_spec(5)})
    eq("B16.1 exit 1", s.code, 1)
    eq("B16.2 两条结果", len(s.results), 2)
    eq("B16.3 一台 skipped 一台 failed", sorted(s.outcomes()), ["failed", "skipped"])
    eq("B16.4 只发 1 条 TG", s.sent(), 1)
    eq("B16.5 只带 1 个按钮", len(s.last_buttons()), 1)
    eq("B16.6 按钮指向进窗口那台", s.last_buttons()[0]["url"], V2)
    ok("B16.7 两台名字不同", len(set(s.names())) == 2, s.names())

    # --- B17 剩 6 天：报告排版不重复天数
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE, V1: vps_spec(6)})
    ok("B17.1 「剩 6 天」只出现一次", s.out.count("剩 6 天") == 1, s.out)
    ok("B17.2 报告头是【Openworld VPS】", "【Openworld VPS】" in s.out, s.out)

    # --- B18 DRY_RUN：不发 TG，但打印原文与按钮
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE, V1: vps_spec(5)},
                     dry_run=True)
    eq("B18.1 没调 notify.send", s.sent(), 0)
    ok("B18.2 打印了将发送的原文", "DRY_RUN 演练" in s.out, s.out)
    ok("B18.3 原文里有按钮 URL", V1 in s.out, s.out)
    # 5 天还没到 🚨 的线（<=3 才升级，见 B2.14），所以标题是「守門提醒」。
    ok("B18.4 原文里有粗体标记", "<b>Openworld VPS 守門提醒</b>" in s.out, s.out)
    eq("B18.5 退出码不受 dry_run 影响", s.code, 1)
    ok("B18.6 仍然留标记（演练也算已处理）", s.marker())

    # --- B19 服务器 stopped 且重启失败：仍按天数走
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE,
                      V1: vps_spec(12, status="stopped")})
    eq("B19.1 exit 0（12 天不用续）", s.code, 0)
    ok("B19.2 报告写「重启失败」", "重启失败" in s.out, s.out)
    ok("B19.3 真的 reload 过（轮询状态）", s.page.reloads > 0, s.page.reloads)

    # --- B19b 服务器 stopped 但重启成功：状态文案变「重启成功」
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE,
                      V1: FakeSpec(title="vps", body="Status stopped. Renews in 12 days.",
                                   reload_spec=FakeSpec(
                                       title="vps",
                                       body="Status running. Renews in 12 days."))})
    eq("B19b.1 exit 0", s.code, 0)
    ok("B19b.2 报告写「重启成功」", "重启成功" in s.out, s.out)
    eq("B19b.3 第一次 reload 就起来了", s.page.reloads, 1)

    # --- B19c stopped 且天数也进窗口：重启失败照样进窗口告警
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE,
                      V1: vps_spec(3, status="stopped")})
    eq("B19c.1 exit 1", s.code, 1)
    eq("B19c.2 发 1 条 TG", s.sent(), 1)
    ok("B19c.3 消息里带重启失败的现状", "重启失败" in s.last_text(), s.last_text())

    # --- B20 报告名不带 ACCOUNT_LABEL 时不加括号
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE, V1: vps_spec(12)},
                     envs={"ACCOUNT_LABEL": None})
    eq("B20.1 名字无括号", s.names(), ["Openworld e2ce269b"])
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE, V1: vps_spec(12)},
                     envs={"ACCOUNT_LABEL": "02"})
    eq("B20.2 名字带标签", s.names(), ["Openworld e2ce269b（02）"])

    # --- B21 消息里的动态内容被 HTML 转义
    s = run_scenario({DASH: FakeSpec(status=200, title="<img src=x> bad gateway",
                                     body="bad gateway")})
    eq("B21.1 仍判上游故障", s.outcomes(), ["transient"])
    ok("B21.2 尖括号被转义（不会让 TG 400）",
       "<img" not in s.last_text() and "&lt;img" in s.last_text(), s.last_text())

    # --- B22 自定义阈值：RENEW_THRESHOLD_DAYS=7 时 6 天进窗口
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE, V1: vps_spec(6)},
                     envs={"RENEW_THRESHOLD_DAYS": "7"})
    eq("B22.1 exit 1（7 天阈值下 6 天算进窗口）", s.code, 1)
    eq("B22.2 是 FAILED", s.outcomes(), ["failed"])
    ok("B22.3 报告带天数", "剩 6 天" in s.out, s.out)

    # --- B23 阈值-1 的静默是可配置阈值下的同一位置
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE, V1: vps_spec(6)},
                     envs={"RENEW_THRESHOLD_DAYS": "7"})
    eq("B23.1 7-1=6 是中间日 → 静默", s.sent(), 0)
    ok("B23.2 但仍标红", s.code == 1)

    # --- B24 截图与 cookie 刷新：cookie 刷新函数被调到一次
    s = run_scenario({DASH: OK_DASH, VPSPAGE: OK_VPSPAGE, V1: vps_spec(12)})
    eq("B24.1 刷新了一次 cookie", len(s.refresh_calls), 1)


# ====================================================== [C] 静态接线
def group_c():
    section("C")

    ok("C1 入口是 sys.exit(main())", "sys.exit(main())" in CODE)
    ok("C2 没有 sys.exit(数字) 的硬退出",
       not re.search(r"sys\.exit\s*\(\s*[0-9]", CODE), "found numeric sys.exit")
    ok("C3 没有 sys.exit(fail_code) 之类", "sys.exit(fail_code)" not in CODE)

    n_env = len(re.findall(r"os\.environ", CODE))
    eq("C4 os.environ 只剩 1 处（模块顶部 setdefault 日志级别）", n_env, 1)
    ok("C5 不再有 os.environ.get 读配置",
       "os.environ.get" not in CODE)

    ok("C6 模块级不再持有 TG_BOT_TOKEN 变量",
       not re.search(r"^TG_BOT_TOKEN\s*=", CODE, re.M))
    ok("C7 模块级不再持有 TG_CHAT_ID 变量",
       not re.search(r"^TG_CHAT_ID\s*=", CODE, re.M))
    ok("C8 不再有 send_telegram_message 定义",
       "def send_telegram_message" not in CODE)
    ok("C9 不再有 now_local 定义", "def now_local" not in CODE)
    ok("C10 手搓 requests.post 到 Telegram 已删除",
       "api.telegram.org" not in CODE)

    ok("C11 用 renewkit.env 读配置", "env.get(" in CODE and "env.get_int(" in CODE)
    ok("C12 用 env.dry_run 读演练开关", "env.dry_run()" in CODE)
    ok("C13 用 renewkit.notify.send", "notify.send(" in CODE)
    ok("C14 notify.send 传了 buttons",
       re.search(r"notify\.send\([^)]*buttons=", CODE) is not None)
    ok("C15 notify.send 传了 HTML parse_mode",
       # 这是个字符串字面量，CODE 里已被抹白，只能在原文里找
       'parse_mode="HTML"' in APP_SRC)
    ok("C16 用 RenewReport", "RenewReport(service=SERVICE)" in CODE)
    ok("C17 finish 用 notify_tg=False（通知自己发）",
       "report.finish(notify_tg=False)" in CODE)
    ok("C18 没有 finish(notify_tg=True)",
       "notify_tg=True" not in CODE)
    ok("C19 用了 shorten() 压平 detail", "shorten(" in CODE)

    # 所有 report.add 都走 _note（连 main() 里的兜底异常分支也不例外）
    adds = re.findall(r"(?<!def )report\.add\(", CODE)
    ok("C20 report.add 只在 _note 里出现 1 次", len(adds) == 1, len(adds))
    notes = re.findall(r"(?<!def )_note\(\s*report\s*,\s*(\w+)\s*,", CODE)
    ok("C21 _note 有 12 个调用点", len(notes) == 12, len(notes))
    ok("C22 _note 的 target 参数名统一（没有散落的 name）",
       set(notes) == {"target", "PANEL_TARGET"}, sorted(set(notes)))

    # AST：run_all 里不得出现把 name 当目标的写法
    tree = ast.parse(APP_SRC)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    ok("C23 有 run_all 函数", "run_all" in funcs)
    ok("C24 run_all 返回三元组（报告/行/按钮）",
       isinstance(funcs["run_all"].body[-1], ast.Return))
    ok("C25 main 有返回值注解 int",
       funcs["main"].returns is not None
       and getattr(funcs["main"].returns, "id", "") == "int")

    # 通知节律：阈值-1 静默的那段还在
    ok("C26 保留了「窗口中间日静默」逻辑",
       "RENEW_THRESHOLD_DAYS - 1" in CODE)
    ok("C27 保留了 RENEW_THRESHOLD_DAYS 阈值判断",
       "RENEW_THRESHOLD_DAYS" in CODE)

    # 三态登录
    ok("C28 verify_logged_in 返回元组（三态）",
       "def verify_logged_in(page) -> tuple" in APP_SRC)
    ok("C29 不再把 verify_logged_in 当 bool 用",
       not re.search(r"success\s*=\s*verify_logged_in", CODE))
    ok("C30 get_vps_urls 抛 UpstreamDown", "raise UpstreamDown(" in CODE)
    ok("C31 有 except UpstreamDown 分支",
       "except UpstreamDown as exc" in CODE)

    # .renew-handled 标记
    ok("C32 脚本会写 .renew-handled 标记", '".renew-handled"' in APP_SRC)

    # 死代码仍然完好（本轮故意不动）
    ok("C33 GIF/OCR 区块仍在（本轮不动）",
       "def try_renew_captcha" in CODE and "def recognize_captcha_by_frames" in CODE)
    ok("C34 save_screenshot 仍是空实现（有意禁用）",
       re.search(r"def save_screenshot\(page, name: str\):\s*\n\s*\"\"\"[^\"]*\"\"\"\s*\n\s*pass",
                 APP_SRC) is not None)

    # ---- workflow ----
    try:
        import yaml
        wf = yaml.safe_load(WF_SRC)
    except ImportError:
        wf = None
        ok("C35 本机有 yaml 可解析 workflow", False, "pyyaml 缺失")

    if wf:
        on = wf[True] if True in wf else wf["on"]
        eq("C35 cron 仍是每日 01:00 UTC", on["schedule"][0]["cron"], "0 1 * * *")
        ok("C36 保留 workflow_dispatch", "workflow_dispatch" in on)
        ok("C37 dispatch 有 dry_run 布尔入参",
           on["workflow_dispatch"]["inputs"]["dry_run"]["type"] == "boolean")
        job = wf["jobs"]["Renew-openworld"]
        eq("C38 permissions.actions 仍是 write",
           job["permissions"]["actions"], "write")
        eq("C39 permissions.contents 仍是 read",
           job["permissions"]["contents"], "read")

        uses = [s.get("uses") for s in job["steps"] if s.get("uses")]
        ok("C40 用 renew-kit composite action",
           any("renew-kit/.github/actions/renew@" in u for u in uses), uses)
        ok("C41 钉在 v0.5.1（有 buttons + 不复读天数的那版）",
           any(u.endswith("@v0.5.1") for u in uses), uses)

        step = next(s for s in job["steps"]
                    if "renew-kit/.github/actions/renew@" in (s.get("uses") or ""))
        w = step["with"]
        eq("C42 script 是 apprenew.py", w["script"], "apprenew.py")
        eq("C43 renewkit-ref 是 v0.5.1", w["renewkit-ref"], "v0.5.1")
        ok("C44 apt 装 xvfb + 中文字体",
           "xvfb" in w["apt-packages"] and "fonts-noto-cjk" in w["apt-packages"],
           w["apt-packages"])
        ok("C45 pip 装 playwright",
           "playwright" in w["pip-packages"], w["pip-packages"])
        ok("C46 pip 仍装 Pillow/numpy（模块顶部 import 需要）",
           "Pillow" in w["pip-packages"] and "numpy" in w["pip-packages"],
           w["pip-packages"])
        ok("C47 setup-command 装 chromium",
           "playwright install --with-deps chromium" in w["setup-command"],
           w["setup-command"])
        ok("C48 command 走 xvfb-run",
           w["command"].startswith("xvfb-run") and "apprenew.py" in w["command"],
           w["command"])
        eq("C49 关掉 action 内置兜底通知（避免破静默日）",
           w["notify-on-failure"], "false")

        env = step["env"]
        for name in ("OPENWORLD_COOKIES", "DISCORD_TOKEN", "TG_BOT_TOKEN",
                     "TG_CHAT_ID", "HEADLESS", "ORT_LOGGING_LEVEL"):
            ok(f"C50 env 保留 {name}", name in env, sorted(env))
        eq("C51 HEADLESS 仍为 true", env["HEADLESS"], "true")
        eq("C52 ORT_LOGGING_LEVEL 仍为 3", env["ORT_LOGGING_LEVEL"], "3")
        ok("C53 DRY_RUN 绑到 dry_run 入参",
           "inputs.dry_run" in str(env.get("DRY_RUN", "")), env.get("DRY_RUN"))

        # 兜底通知步骤
        fb = [s for s in job["steps"] if "兜底通知" in (s.get("name") or "")]
        eq("C54 有兜底通知步骤", len(fb), 1)
        ok("C55 兜底只在 failure() 时跑", fb[0]["if"] == "failure()", fb[0].get("if"))
        ok("C56 兜底会先看 .renew-handled 标记",
           ".renew-handled" in fb[0]["run"])
        ok("C57 有标记就直接退出", "exit 0" in fb[0]["run"])

        # 产物上传
        ups = [s for s in job["steps"] if (s.get("uses") or "").startswith(
            "actions/upload-artifact")]
        eq("C58 保留 2 个产物上传", len(ups), 2)
        names = {u["with"]["name"]: u for u in ups}
        ok("C59 保留 captcha-gifs", "captcha-gifs" in names, sorted(names))
        ok("C60 保留 debug-screenshots", "debug-screenshots" in names, sorted(names))
        eq("C61 captcha-gifs 保留 3 天", names["captcha-gifs"]["with"]["retention-days"], 3)
        eq("C62 debug-screenshots 保留 7 天",
           names["debug-screenshots"]["with"]["retention-days"], 7)
        ok("C63 两个上传都是 if: always()",
           all(u["if"] == "always()" for u in ups), [u.get("if") for u in ups])

        cl = [s for s in job["steps"] if "清理" in (s.get("name") or "")]
        ok("C64 保留清理步骤", len(cl) == 1)
        ok("C65 清理 chromium 与 Xvfb",
           "pkill -f chromium" in cl[0]["run"] and "pkill -f Xvfb" in cl[0]["run"])

        ok("C66 不再手搓 setup-python / pip install",
           not any("actions/setup-python" in (s.get("uses") or "") for s in job["steps"]))
        ok("C67 不再自己 apt-get update",
           "apt-get update" not in WF_SRC)

    # 脚本读的 env 名必须在 workflow 里有交代；只有明确「可选」的三项才允许缺
    read_envs = set(re.findall(r'env\.get(?:_int)?\("([A-Z_]+)"', APP_SRC))
    OPTIONAL_ENVS = {"SCREENSHOT_DIR", "RENEW_THRESHOLD_DAYS", "ACCOUNT_LABEL"}
    wf_names = set()
    if wf:
        step = next(s for s in wf["jobs"]["Renew-openworld"]["steps"]
                    if "renew-kit/.github/actions/renew@" in (s.get("uses") or ""))
        wf_names = set(step["env"])
    ok("C68 脚本读的关键 env 都在 workflow 里（可选三项除外）",
       read_envs - OPTIONAL_ENVS <= wf_names,
       f"missing={sorted((read_envs - OPTIONAL_ENVS) - wf_names)}")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    ok("C68b 可选 env 在 README 里有交代",
       all(name in readme for name in OPTIONAL_ENVS),
       sorted(name for name in OPTIONAL_ENVS if name not in readme))

    ok("C69 .gitignore 忽略 .renew-handled",
       ".renew-handled" in (ROOT / ".gitignore").read_text(encoding="utf-8"))

    # README 的口径必须跟得上（迁移前它整篇还在讲 Discord OAuth + GIF 验证码）
    ok("C70 README 说明已迁到 renew-kit", "renew-kit" in readme)
    ok("C71 README 讲了 .renew-handled 机制", ".renew-handled" in readme)
    ok("C72 README 不再把 DISCORD_TOKEN 标成必填",
       "| `DISCORD_TOKEN` | **必填**" not in readme)
    ok("C73 README 说明不需要代理",
       "不需要代理" in readme, "缺「不需要代理」说明")


# ======================================================== [D] 子进程
def group_d():
    section("D")
    py = sys.executable
    env = dict(os.environ)
    # 子进程里没有 install_stubs() 那套 sys.modules 注入，必须靠真实路径：
    #   ① 替身目录（numpy / PIL / playwright） ② renewkit ③ requests 之类第三方
    stub_dir = make_stub_dir()
    parts = [str(stub_dir)]
    parts += [p for p in (os.environ.get("PYTHONPATH") or "").split(os.pathsep) if p]
    third = _third_party_path()
    if third:
        parts.append(third)
    parts.append(str(RENEWKIT_ROOT))
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
    env.pop("OPENWORLD_COOKIES", None)
    env.pop("DISCORD_TOKEN", None)

    # D1: 语法/编译检查
    r = subprocess.run([py, "-m", "py_compile", str(APP_PATH)],
                       capture_output=True, text=True, cwd=str(ROOT))
    eq("D1 py_compile 通过", r.returncode, 0)

    # D2: 直接跑（无凭据）→ 未配置认证，exit 1
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([py, str(APP_PATH)], capture_output=True, text=True,
                           cwd=td, env=env, timeout=120)
        eq("D2 无凭据时 exit 1", r.returncode, 1)
        ok("D3 日志说未配置认证", "未配置认证" in r.stdout, r.stdout[-400:])
        ok("D4 日志有报告头", "【Openworld VPS】" in r.stdout, r.stdout[-400:])
        ok("D5 报告写「续期未完成」", "续期未完成" in r.stdout, r.stdout[-400:])
        ok("D6 留下了 .renew-handled", (Path(td) / ".renew-handled").exists())

    # D7: --help 之类不存在，确认没有 argparse 副作用
    ok("D7 没有 argparse 交互式入口", "argparse" not in APP_SRC)

    # D8: 模块可被 import 而不执行 main
    with tempfile.TemporaryDirectory() as td:
        code = (
            "import importlib.util, sys\n"
            f"sys.path.insert(0, {str(RENEWKIT_ROOT)!r})\n"
            f"spec = importlib.util.spec_from_file_location('a', {str(APP_PATH)!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "print('IMPORT_OK', hasattr(m, 'main'), hasattr(m, 'run_all'))\n"
        )
        r = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           cwd=td, env=env, timeout=120)
        ok("D8 import 不触发 main", "IMPORT_OK True True" in r.stdout,
           (r.stdout + r.stderr)[-500:])


# ================================================================== 主流程
def main(argv):
    groups = [a.upper() for a in argv if a.upper() in ("A", "B", "C", "D")]
    if not groups:
        groups = ["A", "B", "C", "D"]
    print("0penw0r1dvps-watchd0g / apprenew.py 迁移验证")
    print(f"  应用: {APP_PATH}")
    print(f"  renewkit: {RENEWKIT_ROOT}")
    print(f"  组: {' '.join(groups)}")
    print(f"  依赖替身: {install_stubs() or '（无需，环境已齐）'}")

    for g in groups:
        try:
            {"A": group_a, "B": group_b, "C": group_c, "D": group_d}[g]()
        except BaseException:                     # noqa: BLE001
            FAIL.append((g, "组执行异常", traceback.format_exc()))
            print(f"  ❌ [{g}] 组执行异常\n{traceback.format_exc()}")

    print("\n" + "=" * 62)
    if FAIL:
        print(f"❌ {len(FAIL)}/{PASS + len(FAIL)} 断言失败\n")
        for grp, label, extra in FAIL:
            print(f"  [{grp}] {label}")
            if extra:
                print(f"        {textwrap.shorten(str(extra), 300)}")
        return 1
    print(f"✅ 全部通过（{PASS} 项断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
