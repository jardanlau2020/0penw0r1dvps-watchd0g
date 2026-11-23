#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# v26-github: rotate inverse 模式 + token 通过即停 + odd 两两 NCC

import os
import re
import sys
import json
import random
import urllib.parse
import requests
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
try:
    # patchright 是带反检测补丁的 playwright，API 兼容；上游 requirements 用的它
    from patchright.sync_api import sync_playwright
    _PW_IMPL = "patchright"
except ImportError:  # 本地/旧环境没装 patchright 时退回普通 playwright
    from playwright.sync_api import sync_playwright
    _PW_IMPL = "playwright"


try:
    import numpy as np
    import cv2
except ImportError:
    print("❌ 缺少依赖，请先运行：")
    print("   pip install playwright requests numpy opencv-python-headless")
    print("   playwright install chromium")
    sys.exit(1)


CONFIG = {
    "DISCORD_TOKEN": os.environ.get("DISCORD_TOKEN", "").strip(),
    "TG_BOT_TOKEN":  os.environ.get("TG_BOT_TOKEN", "").strip(),
    "TG_CHAT_ID":    os.environ.get("TG_CHAT_ID", "").strip(),
    # 老板这边 secret 叫 ACCOUNT_LABEL，两个名字都认，免得重配
    "ACCOUNT_NAME":  (os.environ.get("ACCOUNT_NAME")
                      or os.environ.get("ACCOUNT_LABEL") or "GitHub Actions"),
    "SITE_BASE": "https://openworld.eu.org",
    "DISCORD_GUILD_ID": "1525632757072658502",
    "HEADLESS": os.environ.get("HEADLESS", "true").strip().lower() != "false",
    "SCREENSHOT_DIR": os.environ.get("SCREENSHOT_DIR", "./screenshots"),
    "RENEW_THRESHOLD_DAYS": int(os.environ.get("RENEW_THRESHOLD_DAYS", "5") or 5),
    "MATCH_STRATEGY": os.environ.get("MATCH_STRATEGY", "same").strip().lower(),
    "ROTATE_MODE": os.environ.get("ROTATE_MODE", "direct").strip().lower(),
}

DISCORD_TOKEN = CONFIG["DISCORD_TOKEN"]
DISCORD_GUILD_ID = CONFIG["DISCORD_GUILD_ID"]
TG_CHAT_ID    = CONFIG["TG_CHAT_ID"]
TG_BOT_TOKEN  = CONFIG["TG_BOT_TOKEN"]
ACCOUNT_NAME  = CONFIG["ACCOUNT_NAME"]
SITE_BASE     = CONFIG["SITE_BASE"]
HEADLESS      = CONFIG["HEADLESS"]
SCREENSHOT_DIR = CONFIG["SCREENSHOT_DIR"]
RENEW_THRESHOLD_DAYS = CONFIG["RENEW_THRESHOLD_DAYS"]
MATCH_STRATEGY = CONFIG["MATCH_STRATEGY"]
ROTATE_MODE = CONFIG["ROTATE_MODE"]

SCRIPT_DIR = Path(__file__).resolve().parent
if not os.path.isabs(SCREENSHOT_DIR):
    SCREENSHOT_DIR = str(SCRIPT_DIR / SCREENSHOT_DIR)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

SUPPORTED_KINDS = ("puzzle", "key", "rotate", "odd", "match")
MAX_SWITCH_PER_SESSION = 4
MAX_FAIL_PER_SESSION = 3
# 2026-11-23：连续多少次「WS 一开就被关、一题都没答出去」就判定出口不可用、提前放弃。
# 依据（同一台 VPS e2ce269b 两次对照）：
#   · 直连机房 IP  → 第 11/12 次会话是 WS 一开就 `⬅️ fail` 或直接 `⚠️ WS 已关闭`，无 ans；
#   · 住宅出口    → 前 5 次都答出来了（score 90~93 被拒），第 6 次过。
# 所以「失败几次就退」不能一刀切：解题层失败要留着继续试，连接层连败才是脏 IP。
MAX_WS_DEAD = int(os.environ.get("MAX_WS_DEAD", "3") or 3)

# ── 老板这份的补丁区（上游没有的部分）──────────────────────────────────────
# 降级开关：DISCORD_TOKEN 挂了、一时又拿不到新的时，SKIP_DISCORD=1 只走
# cookie 注入 + Clerk 换会话。续期链路本身不依赖 Discord。
SKIP_DISCORD = os.environ.get("SKIP_DISCORD", "").strip().lower() in ("1", "true", "yes", "on")

# DOM 侦察：只在 workflow_dispatch 的 dump_dom=true 时打开。打开后进到 VPS 页
# 就 dump 结构并跳过本轮判断——不点续期、不碰验证码。
DUMP_DOM = os.environ.get("DUMP_DOM", "").strip().lower() in ("1", "true", "yes", "on")

# 演练：不发 Telegram，原文打日志
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")

# 验证码自动解算开关：默认开（与上游行为一致）。设 ENABLE_CAPTCHA_SOLVE=0 时不尝试解算，
# 直接按「需人工过验证码」处理并发通知——留一条不依赖解算能力的降级路径。
ENABLE_CAPTCHA_SOLVE = os.environ.get("ENABLE_CAPTCHA_SOLVE", "true").strip().lower() not in ("0", "false", "no", "off")

# Clerk 会话换 sessioncookie 的最长等待（秒）
CLERK_EXCHANGE_WAIT = int(os.environ.get("CLERK_EXCHANGE_WAIT", "90") or 90)

# 跨轮次熔断：连续 N 轮 cron 都失败就停手并通知（B7）。
# 单轮内部仍给足上游的 12 次重试——熔断看的是「跨 run」，不是限制单轮。
MAX_FAIL_STREAK = int(os.environ.get("MAX_FAIL_STREAK", "2") or 2)
FAIL_STREAK_FILE = os.environ.get("FAIL_STREAK_FILE", "ow_fail_streak.txt")

# 面板会话 cookie 名（GET /logout 的 Set-Cookie 实证）
PANEL_SESSION_COOKIE = "sessioncookie"
PANEL_CSRF_COOKIE = "csrf_token"
CLERK_SESSION_COOKIE = "__session"
CLERK_COOKIE_NAMES = ("__session", "__client", "__client_uat")
CLERK_CALLBACK_PATH = "/auth/clerk-callback"
OPENWORLD_COOKIES = os.environ.get("OPENWORLD_COOKIES", "").strip()

ALIGN_STRATEGIES = [
    ("none",  0), ("none", -1), ("none", +1),
    ("sub",   0), ("sub",  -1), ("sub",  +1),
    ("none", -2), ("none", +2), ("add",   0),
]

ROTATE_NCC_MIN = 0.15
ROTATE_NCC_SUBMIT = 0.28
ALIGN_STATE = {"counter": 0}
_MOUSE_POS = {"x": None, "y": None}


STEALTH_JS = r"""
(function() {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => false }); } catch(e) {}
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => [{name:'PDF Viewer'},{name:'Chrome PDF Viewer'},{name:'Chromium PDF Viewer'}]
    });
  } catch(e) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en','zh-CN'] }); } catch(e) {}
  try { if (!window.chrome) window.chrome = { runtime:{}, loadTimes:function(){}, csi:function(){}, app:{} }; } catch(e) {}
  ['__selenium_unwrapped','__webdriver_evaluate','__selenium_evaluate','__driver_evaluate',
   '__fxdriver_evaluate','_Selenium_IDE_Recorder','__webdriver_script_fn','__webdriver_script_func',
   '__webdriver_script_url','__driver_script_fn','__driver_script_url','_phantom','__nightmare',
   'callPhantom','domAutomation','domAutomationController'].forEach(function(k){
    try { delete window[k]; } catch(e) {}
  });
  try { for (var k in window) if (k.indexOf('$cdc_')===0) { try{delete window[k];}catch(e){} } } catch(e) {}
  try { delete document.$cdc_asdjflasutopfhvcZLmcfl_; } catch(e) {}
})();
"""


WS_STATE = {
    "url": None, "meta": None, "frames": [], "last_resp": None,
    "sent": [], "closed": False, "fail_pending": False,
}


def _reset_ws_state():
    WS_STATE.update({"meta": None, "frames": [], "last_resp": None,
                     "sent": [], "closed": False, "fail_pending": False})


def _reset_align_pick():
    ALIGN_STATE["counter"] = 0


def _install_ws_hook(page):
    def on_ws(ws):
        if "openworld.eu.org" not in ws.url:
            return
        WS_STATE["url"] = ws.url
        WS_STATE["closed"] = False
        print(f"   🔌 WebSocket: {ws.url}")

        def on_sent(payload):
            try:
                if isinstance(payload, (bytes, bytearray)):
                    WS_STATE["sent"].append(f"[bin:{len(payload)}]")
                else:
                    s = str(payload)
                    WS_STATE["sent"].append(s[:200])
                    print(f"   ➡️ {s[:140]}")
            except Exception:
                pass

        def on_recv(payload):
            try:
                if isinstance(payload, (bytes, bytearray)):
                    WS_STATE["frames"].append(bytes(payload))
                else:
                    s = str(payload)
                    WS_STATE["last_resp"] = s
                    print(f"   ⬅️ {s[:180]}")
                    if s == "fail" or s.startswith("failed:"):
                        WS_STATE["fail_pending"] = True
                    try:
                        m = json.loads(s)
                        if isinstance(m, dict) and m.get("id"):
                            WS_STATE["meta"] = m
                            WS_STATE["frames"] = []
                    except Exception:
                        pass
            except Exception:
                pass

        def on_close():
            WS_STATE["closed"] = True
            print("   🔒 WebSocket 关闭")

        ws.on("framesent", on_sent)
        ws.on("framereceived", on_recv)
        ws.on("close", on_close)

    page.on("websocket", on_ws)


def send_telegram_message(message: str):
    if DRY_RUN:
        print(f"\nℹ️ DRY_RUN 演练，跳过 Telegram。本应发送：\n{message}")
        return
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print(f"   📢 [本地] {message}")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": f"👤 {ACCOUNT_NAME}\n{message}"},
            timeout=10,
        )
        print("✅ Telegram 已发送" if r.status_code == 200 else f"❌ TG {r.status_code}")
    except Exception as e:
        print(f"❌ TG 异常: {e}")


def save_screenshot(page, name):
    try:
        page.screenshot(path=os.path.join(SCREENSHOT_DIR, f"{name}.png"))
        print(f"   📸 {name}.png")
    except Exception as e:
        print(f"   ⚠️ 截图: {e}")


def dump_page_debug(page, name):
    save_screenshot(page, name)
    try:
        with open(os.path.join(SCREENSHOT_DIR, f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass


def wait_for_cloudflare(page, timeout=15):
    inds = ["verify you are human", "just a moment", "checking your browser",
            "cf-browser-verification", "challenge-platform"]
    start = time.time()
    while time.time() - start < timeout:
        try:
            if not any(i in page.content().lower() for i in inds):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ══════════════════════════════════════════════════════════════════════════
# 以下为本仓补丁（上游缺）：token 预检、cookie 注入、Clerk 换会话、DOM 侦察
# ══════════════════════════════════════════════════════════════════════════

DISCORD_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def discord_token_alive(dc_token: str):
    """预检 DISCORD_TOKEN 死活，返回 (True/False/None, 说明)。

    oauth2/authorize 对「token 被吊销」和「请求构造不对」回同一条 401，
    光看它分不清该换 token 还是改代码。先打一次 /users/@me 就能定性。
    """
    token = (dc_token or "").strip()
    if not token:
        return False, "token 为空"
    try:
        r = requests.get(
            "https://discord.com/api/v9/users/@me",
            headers={"authorization": token, "user-agent": DISCORD_UA,
                     "accept": "*/*", "x-discord-locale": "zh-CN"},
            timeout=15,
        )
    except Exception as e:
        return None, f"预检请求异常: {e}"
    if r.status_code == 200:
        try:
            u = r.json()
            return True, f"{u.get('username', '?')} (id {u.get('id', '?')})"
        except Exception:
            return True, "HTTP 200"
    return False, f"HTTP {r.status_code} {(r.text or '')[:120]}"


def login_with_cookies(page, cookie_header: str) -> bool:
    """把整行 Cookie 注入浏览器上下文（上游只认 token，这里补 cookie 兜底）。

    通用 name=value 拆分，Clerk 的 __session / __client_uat 以及带实例后缀的
    __session_<id> 都收；面板域与 clerk 子域各注一份。
    """
    raw = (cookie_header or "").strip()
    if not raw:
        return False
    pairs = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if name:
            pairs.append((name, value))
    if not pairs:
        print("   ❌ OPENWORLD_COOKIES 解析不到任何 cookie")
        return False

    cookies = [{"name": n, "value": v, "domain": "openworld.eu.org", "path": "/"}
               for n, v in pairs]
    clerk_extra = [{**c, "domain": "clerk.openworld.eu.org"}
                   for c in cookies if c["name"].startswith("__")]
    try:
        page.context.clear_cookies()
        page.context.add_cookies(cookies)
        if clerk_extra:
            page.context.add_cookies(clerk_extra)
    except Exception as e:
        print(f"   ❌ 注入 cookie 失败: {e}")
        return False
    print(f"   已注入 {len(cookies)} 个 cookie: {[n for n, _ in pairs]}")
    return True


def _cookie_map(page) -> dict:
    try:
        return {c.get("name"): c.get("value")
                for c in (page.context.cookies(SITE_BASE) or []) if c.get("name")}
    except Exception:
        return {}


def exchange_clerk_session(page, wait_seconds: int = 0) -> bool:
    """用 Clerk 前端会话换面板 sessioncookie（2026-10-27 改版后的登录口径）。

    站点已不再「贴 cookie 就完事」：面板只认 sessioncookie，而它只能由
    /login 的 Clerk 流程下发。两条路：
      1. 登录页自己的 JS 换（注入的 cookie 里还有 Clerk 会话时，页面加载后
         Clerk.addListener 会自动 POST /auth/clerk-callback）
      2. 页面 JS 不动时自己 POST（token 取 __session，带 X-CSRFToken 与 browser_fp）
    """
    wait_seconds = wait_seconds or CLERK_EXCHANGE_WAIT
    jar = _cookie_map(page)
    clerk_names = [n for n in CLERK_COOKIE_NAMES if jar.get(n)]
    if not clerk_names:
        print(f"   ℹ️ 没有 Clerk 会话 cookie（{list(CLERK_COOKIE_NAMES)}），跳过 clerk-callback")
        return bool(jar.get(PANEL_SESSION_COOKIE))

    print(f"   🔁 检测到 Clerk 会话 {clerk_names}，尝试换面板 sessioncookie")
    try:
        page.goto(f"{SITE_BASE}/login", wait_until="domcontentloaded", timeout=30000)
        wait_for_cloudflare(page)
    except Exception as e:
        print(f"   ⚠️ 打开 /login 异常: {e}")

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _cookie_map(page).get(PANEL_SESSION_COOKIE):
            print("   ✅ 登录页已自动换出 sessioncookie")
            return True
        if "/dashboard" in (page.url or ""):
            print("   ✅ 登录页已跳 dashboard（会话已建立）")
            return True
        time.sleep(3)

    token = (jar.get(CLERK_SESSION_COOKIE) or "").strip()
    if not token:
        print("   ⚠️ 没有 __session，无法直接换")
        return False
    jar = _cookie_map(page)
    csrf = (jar.get(PANEL_CSRF_COOKIE) or "").strip()
    if not csrf:
        print("   ⚠️ 拿不到 csrf_token，无法 POST clerk-callback")
        return False
    try:
        resp = page.request.post(
            f"{SITE_BASE}{CLERK_CALLBACK_PATH}",
            data=json.dumps({"token": token,
                             "browser_fp": {"webdriver": False, "cdc": [], "plugins": 5,
                                            "langs": 3, "chrome": True, "automation": False}}),
            headers={"Content-Type": "application/json", "X-CSRFToken": csrf,
                     "Origin": SITE_BASE, "Referer": f"{SITE_BASE}/login"},
            timeout=30000,
        )
        print(f"   📡 clerk-callback → HTTP {resp.status}")
        if resp.status == 200 and _cookie_map(page).get(PANEL_SESSION_COOKIE):
            print("   ✅ clerk-callback 已下发 sessioncookie")
            return True
    except Exception as e:
        print(f"   ⚠️ POST clerk-callback 异常: {e}")
    return False


DUMP_JS_ASSETS = r"""
() => {
  const out = [];
  document.querySelectorAll('script[src]').forEach(el => out.push(['script', el.src]));
  document.querySelectorAll('link[href]').forEach(el => out.push([el.rel || 'link', el.href]));
  document.querySelectorAll('iframe[src]').forEach(el => out.push(['iframe', el.src]));
  return out;
}
"""

DUMP_JS_CANDIDATES = r"""
() => {
  const sels = ['[class*=renew]', '[id*=renew]', '[class*=captcha]', '[id*=captcha]',
                '[class*=turnstile]', '[id*=turnstile]', '[class*=challenge]',
                '[class*=slider]', '[class*=drag]', 'button', 'form'];
  const seen = new Set();
  const out = [];
  for (const sel of sels) {
    document.querySelectorAll(sel).forEach(el => {
      if (seen.has(el)) return;
      seen.add(el);
      out.push({sel, tag: el.tagName, id: el.id || '', cls: el.className || '',
                html: (el.outerHTML || '').replace(/\s+/g, ' ').slice(0, 3000)});
    });
  }
  return out;
}
"""

DUMP_JS_GLOBALS = r"""
() => {
  return Object.keys(window)
    .filter(k => /captcha|turnstile|renew|gate|challenge|ow/i.test(k))
    .map(k => {
      let t = typeof window[k], v = '';
      try { v = (t === 'object' || t === 'function') ? '' : String(window[k]).slice(0, 200); } catch (e) {}
      return [k, t, v];
    });
}
"""


def dump_dom(page, url: str) -> list:
    """只读地把 VPS 页真实结构 dump 到 SCREENSHOT_DIR，不点任何按钮。

    验证码/续期按钮的结构在公网上没有可信来源（#captcha_box_* 是旧版、
    .fd-captcha-* 是另一个站的），只能让有登录态的脚本自己带回来。
    """
    slug = (url.rstrip("/").rsplit("/", 1)[-1] or "vps")[:8]
    files = []

    def _write(name: str, text: str) -> None:
        path = os.path.join(SCREENSHOT_DIR, name)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"   💾 {name}（{len(text)} 字符）")
            files.append(path)
        except Exception as e:
            print(f"   ⚠️ 写 {name} 失败: {e}")

    try:
        _write(f"dom_{slug}.html", page.content())
    except Exception as e:
        print(f"   ⚠️ page.content() 失败: {e}")
    try:
        assets = page.evaluate(DUMP_JS_ASSETS) or []
        _write(f"dom_{slug}_assets.txt", "\n".join(f"{k}\t{u}" for k, u in assets) + "\n")
        print(f"   📎 script/link/iframe 共 {len(assets)} 条")
    except Exception as e:
        print(f"   ⚠️ 资源清单失败: {e}")
    try:
        cands = page.evaluate(DUMP_JS_CANDIDATES) or []
        globs = page.evaluate(DUMP_JS_GLOBALS) or []
        lines = [f"# url={page.url}", f"# candidates={len(cands)}", ""]
        for c in cands:
            lines.append(f"--- [{c.get('sel')}] <{c.get('tag')}> id={c.get('id')!r} class={c.get('cls')!r}")
            lines.append(c.get("html", ""))
            lines.append("")
        lines.append(f"# window globals={len(globs)}")
        lines.extend(f"{k}\t{t}\t{v}" for k, t, v in globs)
        _write(f"dom_{slug}_candidates.txt", "\n".join(lines) + "\n")
        print(f"   🔎 可疑元素 {len(cands)} 个，相关全局变量 {len(globs)} 个")
    except Exception as e:
        print(f"   ⚠️ 元素抽取失败: {e}")
    try:
        shot = os.path.join(SCREENSHOT_DIR, f"dom_{slug}.png")
        page.screenshot(path=shot, full_page=True)
        files.append(shot)
        print(f"   💾 dom_{slug}.png")
    except Exception as e:
        print(f"   ⚠️ 整页截图失败: {e}")
    print(f"   ✅ DOM 侦察完成，共 {len(files)} 个文件")
    return files


def read_fail_streak() -> int:
    try:
        with open(FAIL_STREAK_FILE, "r", encoding="utf-8") as fh:
            return int((fh.read() or "0").strip() or 0)
    except Exception:
        return 0


def write_fail_streak(n: int) -> None:
    try:
        with open(FAIL_STREAK_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(max(0, int(n))))
    except Exception as e:
        print(f"   ⚠️ 写熔断计数失败: {e}")


def _diag_page(page, tag: str = "") -> None:
    """失败诊断：把当前 URL / 标题 / cookie 名 / 正文长度打出来。

    之前失败只打「未找到」，看不出是 cookie 死了、还是 VPS 列表选择器变了、
    还是被踢回登录页——三种情况处置完全不同，所以失败时必须有这些现场。
    只打 cookie 的 name，不打 value。
    """
    try:
        url = page.url
    except Exception as e:
        url = f"<取不到: {e}>"
    try:
        title = page.title()
    except Exception as e:
        title = f"<取不到: {e}>"
    try:
        names = sorted({c.get("name", "") for c in page.context.cookies()})
    except Exception as e:
        names = [f"<取不到: {e}>"]
    try:
        html_len = len(page.content() or "")
    except Exception:
        html_len = -1
    print(f"   🔎 [{tag}] url={url}")
    print(f"   🔎 [{tag}] title={title!r}")
    print(f"   🔎 [{tag}] cookies({len(names)})={names}")
    print(f"   🔎 [{tag}] html_len={html_len}")


def login(page) -> bool:
    """登录：token（首选）→ cookie + Clerk 换会话（兜底）。"""
    if DISCORD_TOKEN and SKIP_DISCORD:
        print("⏭️ SKIP_DISCORD=1：跳过 Discord 授权，只走 cookie / Clerk 换会话")
    elif DISCORD_TOKEN:
        alive, info = discord_token_alive(DISCORD_TOKEN)
        if alive is False:
            # token 被吊销时不再直接退出：OPENWORLD_COOKIES 已配置就走 cookie + Clerk 兜底，
            # 否则整轮续期会白白失败（2026-11-21 run 44891963738 就死在 return False）。
            print(f"⚠️ DISCORD_TOKEN 已被吊销（{info}），跳过 token，改走 cookie 兜底")
            print("   → 要恢复 token 登录：浏览器登录 Discord → DevTools → Network → 任一 /api/v9 请求")
            print("     → 复制 authorization 头的值（不要带 Bearer）")
        else:
            if alive:
                print(f"✅ token 预检通过：{info}")
            else:
                print(f"⚠️ token 预检无结论（{info}），继续试")
            if login_with_discord_token(page, DISCORD_TOKEN):
                return True
            print("⚠️ token 登录未成功，改用 cookie 兜底")

    if OPENWORLD_COOKIES:
        print(f"🔑 使用 cookie 登录（{len(OPENWORLD_COOKIES)} 字符）")
        if not login_with_cookies(page, OPENWORLD_COOKIES):
            return False
        _diag_page(page, "cookie注入后")
        if exchange_clerk_session(page):
            return True
        print("⚠️ Clerk 换会话未成功，直接验证会话是否可用")
        _diag_page(page, "Clerk换会话后")

    # 最后确认一次：能进 dashboard 就算登录成功
    try:
        page.goto(f"{SITE_BASE}/dashboard", wait_until="domcontentloaded", timeout=30000)
        wait_for_cloudflare(page)
        page.wait_for_timeout(2000)
        if "/login" not in page.url:
            print("✅ 会话有效（/dashboard 可达）")
            return True
    except Exception as e:
        print(f"⚠️ 会话校验异常: {e}")
    _diag_page(page, "dashboard校验")
    print("❌ 登录失败：token 与 cookie 都不通")
    return False


def login_with_discord_token(page, dc_token: str) -> bool:
    print("=" * 50)
    print("🔑 Discord OAuth 登录")
    print("=" * 50)

    try:
        page.goto(SITE_BASE, wait_until="domcontentloaded", timeout=30000)
        wait_for_cloudflare(page)
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"   ⚠️ 首页: {e}")

    try:
        page.goto(f"{SITE_BASE}/login", wait_until="domcontentloaded", timeout=30000)
        wait_for_cloudflare(page)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"   ⚠️ 登录页: {e}")

    def _click(sels, desc=""):
        for sel in sels:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=3000):
                    el.click()
                    print(f"   🔘 {desc}: {sel}")
                    return True
            except Exception:
                continue
        return False

    if "discord.com" not in page.url:
        clicked = _click(["#clerk-signin", "button[id='clerk-signin']",
                          "button:has-text('Sign in with Clerk')",
                          "[data-clerk-component] button"], "Clerk 入口")
        if clicked:
            page.wait_for_timeout(4000)
            d_sels = ["button:has-text('Continue with Discord')",
                      "button.cl-socialButtonsBlockButton",
                      "button[data-localization-key='socialButtonsBlockButton']",
                      "button:has-text('Discord')",
                      "a:has-text('Continue with Discord')"]
            _click(d_sels, "Clerk 内 Discord")
            try:
                page.wait_for_url(re.compile(r"discord\.com"), timeout=20000,
                                  wait_until="domcontentloaded")
                print("   ✅ 已跳到 Discord")
            except Exception:
                pass
        if "discord.com" not in page.url and not clicked:
            _click(["button:has-text('Sign in with Discord')",
                    "a:has-text('Sign in with Discord')",
                    "button:has-text('Continue with Discord')",
                    "button:has-text('Discord')",
                    "a:has-text('Discord')"], "通用入口")

    if "discord.com" not in page.url:
        for _ in range(15):
            page.wait_for_timeout(1000)
            if "discord.com" in page.url:
                break
        if "discord.com" not in page.url:
            print(f"   ❌ 无法跳转 Discord: {page.url}")
            save_screenshot(page, "login_failed_no_discord")
            return False

    oauth_url = page.url
    if "discord.com/login" in oauth_url and "redirect_to=" in oauth_url:
        p = urllib.parse.urlparse(oauth_url)
        q = urllib.parse.parse_qs(p.query)
        rt = q.get("redirect_to", [""])[0]
        if rt:
            oauth_url = ("https://discord.com" + rt) if rt.startswith("/") else rt

    p = urllib.parse.urlparse(oauth_url)
    q = urllib.parse.parse_qs(p.query)
    client_id     = q.get("client_id", [""])[0]
    redirect_uri  = q.get("redirect_uri", [""])[0]
    scope         = q.get("scope", ["identify email"])[0]
    state         = q.get("state", [""])[0]
    response_type = q.get("response_type", ["code"])[0]

    if not client_id or not redirect_uri:
        print("   ❌ OAuth 参数解析失败")
        save_screenshot(page, "login_failed_parse")
        return False

    api_p = urllib.parse.urlencode({
        "client_id": client_id, "response_type": response_type,
        "redirect_uri": redirect_uri, "scope": scope, "state": state,
    })
    try:
        r = requests.post(
            f"https://discord.com/api/v9/oauth2/authorize?{api_p}",
            headers={
                "accept": "*/*",
                "authorization": dc_token.strip(),
                "content-type": "application/json",
                "origin": "https://discord.com",
                "referer": f"https://discord.com/oauth2/authorize?{api_p}",
                "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/130.0.0.0 Safari/537.36"),
            },
            json={
                "guild_id": DISCORD_GUILD_ID,
                "permissions": "0", "authorize": True, "integration_type": 0,
                "location_context": {"guild_id": "10000", "channel_id": "10000",
                                     "channel_type": 10000},
            },
            timeout=20,
        )
        print(f"   API: {r.status_code}")
        if r.status_code in (401, 403):
            print("   ❌ Discord Token 失效")
            return False
        if r.status_code != 200:
            print(f"   ❌ {r.text[:200]}")
            return False
        location = r.json().get("location", "")
    except Exception as e:
        print(f"   ❌ API 异常: {e}")
        return False

    if not location:
        return False

    try:
        page.goto(location, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    page.wait_for_timeout(5000)
    wait_for_cloudflare(page)

    if "/login" in page.url and "discord" not in page.url:
        page.wait_for_timeout(5000)
        if "/login" in page.url:
            print(f"   ❌ 登录失败: {page.url}")
            save_screenshot(page, "login_callback_stuck")
            return False

    print(f"   ✅ 登录成功: {page.url}")
    return True


# ================= 求解器 =================

def _decode(b):
    return cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_UNCHANGED)


def _chip_shape(chip):
    alpha = chip[:, :, 3]
    ys, xs = np.where(alpha > 200)
    if len(xs) == 0:
        return None
    cx0, cx1 = int(xs.min()), int(xs.max()) + 1
    cy0, cy1 = int(ys.min()), int(ys.max()) + 1
    a = alpha[cy0:cy1, cx0:cx1]
    _, th = cv2.threshold(a, 200, 255, cv2.THRESH_BINARY)
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    cnt = max(cnts, key=cv2.contourArea)
    area = cv2.contourArea(cnt)
    peri = cv2.arcLength(cnt, True)
    circ = 4 * np.pi * area / (peri * peri) if peri > 0 else 0
    approx = cv2.approxPolyDP(cnt, 0.04 * peri, True)
    return {
        "w": cx1 - cx0, "h": cy1 - cy0,
        "circ": circ, "nv": len(approx),
        "alpha_cx": (cx0 + cx1) / 2.0,
        "alpha_cy": (cy0 + cy1) / 2.0,
    }


def _bg_black_shapes(bg_gray):
    _, th = cv2.threshold(bg_gray, 40, 255, cv2.THRESH_BINARY_INV)
    kernel = np.ones((3, 3), np.uint8)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel, iterations=1)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        area = cv2.contourArea(cnt)
        if w < 40 or h < 40 or area < 1500:
            continue
        peri = cv2.arcLength(cnt, True)
        if peri < 1:
            continue
        circ = 4 * np.pi * area / (peri * peri)
        approx = cv2.approxPolyDP(cnt, 0.04 * peri, True)
        out.append({"bbox": (x, y, w, h), "circ": circ,
                    "nv": len(approx), "area": area})
    return out


def _solve_puzzle(bg_bytes, chip_bytes, meta, align_idx=0):
    bg = _decode(bg_bytes)
    chip = _decode(chip_bytes)
    if bg is None or chip is None:
        raise RuntimeError("解码失败")
    bg_rgb  = bg[:, :, :3] if bg.ndim == 3 else cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)
    bg_gray = cv2.cvtColor(bg_rgb, cv2.COLOR_BGR2GRAY)

    if chip.ndim != 3 or chip.shape[2] != 4:
        raise RuntimeError("chip 缺少 alpha")

    img_h, img_w = chip.shape[:2]
    chip_info = _chip_shape(chip)
    if chip_info is None:
        raise RuntimeError("chip 轮廓为空")
    chip_w, chip_h = chip_info["w"], chip_info["h"]
    chip_circ, chip_nv = chip_info["circ"], chip_info["nv"]
    alpha_cx = chip_info["alpha_cx"]
    print(f"   📐 chip img: {img_w}x{img_h}  alpha bbox: {chip_w}x{chip_h} "
          f"circ={chip_circ:.2f} nv={chip_nv} alpha_cx={alpha_cx:.1f}")

    candidates = _bg_black_shapes(bg_gray)
    if not candidates:
        raise RuntimeError("bg 上未找到黑色形状")
    print(f"   🔍 bg 上检测到 {len(candidates)} 个形状:")

    scored = []
    for c in candidates:
        bw, bh = c["bbox"][2], c["bbox"][3]
        size_score = 1.0 - min(1.0, abs(bw - chip_w) + abs(bh - chip_h)) / 100.0

        if chip_nv in (3, 4, 5, 6, 7, 8):
            nv_diff = abs(c["nv"] - chip_nv)
            nv_score = max(0.0, 1.0 - nv_diff * 0.3)
            if chip_circ > 0.75:
                circ_score = 1.0 if c["circ"] > 0.7 else 0.4
                shape_score = 0.6 * nv_score + 0.4 * circ_score
            else:
                shape_score = nv_score
        elif chip_circ > 0.75:
            shape_score = 1.0 if c["circ"] > 0.7 else 0.2
        else:
            shape_score = max(0.0, 1.0 - abs(c["circ"] - chip_circ))

        score = 0.7 * shape_score + 0.3 * size_score
        scored.append((score, c))
        print(f"      bbox={c['bbox']} nv={c['nv']} circ={c['circ']:.2f} "
              f"shape={shape_score:.2f} size={size_score:.2f} total={score:.2f}")

    scored.sort(reverse=True, key=lambda x: x[0])
    best_score, best = scored[0]
    if best_score < 0.5:
        raise RuntimeError(f"形状匹配度过低 ({best_score:.2f})")

    x, y, w, h = best["bbox"]
    shape_cx = x + w / 2.0
    px = int(meta.get("px") or 0)
    pw = int(meta.get("pw") or img_w)
    vmax = int(meta.get("vmax") or 300)

    mode, offset = ALIGN_STRATEGIES[align_idx % len(ALIGN_STRATEGIES)]
    base = shape_cx - alpha_cx
    if mode == "sub":
        base -= px
    elif mode == "add":
        base += px
    value = int(round(base + offset))
    value = max(0, min(vmax, value))

    print(f"   🎯 bbox=({x},{y},{w},{h}) score={best_score:.2f} "
          f"shape_cx={shape_cx:.1f} alpha_cx={alpha_cx:.1f} px={px} pw={pw} "
          f"strategy=#{align_idx % len(ALIGN_STRATEGIES)}({mode},{offset:+d}) → value={value}")

    try:
        vis = bg_rgb.copy()
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.imwrite(os.path.join(SCREENSHOT_DIR,
                                 f"puzzle_align_{meta['id'][:6]}_a{align_idx}.png"), vis)
    except Exception as e:
        print(f"   ⚠️ 可视化: {e}")

    return value


def _solve_rotate(bg_bytes, chip_bytes, meta, tag=""):
    bg = _decode(bg_bytes)
    chip = _decode(chip_bytes)
    if bg is None or chip is None:
        return -1
    bg_rgb = bg[:, :, :3] if bg.ndim == 3 else cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)
    bg_gray = cv2.cvtColor(bg_rgb, cv2.COLOR_BGR2GRAY)
    H, W = bg_gray.shape

    ox, oy = int(meta["ox"]), int(meta["oy"])
    ow, oh = int(meta["ow"]), int(meta["oh"])

    pad = 30
    x0 = max(0, ox - pad); y0 = max(0, oy - pad)
    x1 = min(W, ox + ow + pad); y1 = min(H, oy + oh + pad)
    target = bg_gray[y0:y1, x0:x1]
    th, tw = target.shape

    if chip.ndim == 3 and chip.shape[2] == 4:
        chip_rgb = chip[:, :, :3]
        chip_alpha = chip[:, :, 3]
    else:
        chip_rgb = chip[:, :, :3] if chip.ndim == 3 else cv2.cvtColor(chip, cv2.COLOR_GRAY2BGR)
        cg = cv2.cvtColor(chip_rgb, cv2.COLOR_BGR2GRAY)
        _, chip_alpha = cv2.threshold(cg, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    if chip_alpha.max() > 100:
        ys, xs = np.where(chip_alpha > 100)
        if len(xs) > 0:
            cx0, cx1 = int(xs.min()), int(xs.max()) + 1
            cy0, cy1 = int(ys.min()), int(ys.max()) + 1
            chip_gray = cv2.cvtColor(chip_rgb[cy0:cy1, cx0:cx1], cv2.COLOR_BGR2GRAY)
            chip_mask = chip_alpha[cy0:cy1, cx0:cx1]
        else:
            chip_gray = cv2.cvtColor(chip_rgb, cv2.COLOR_BGR2GRAY)
            chip_mask = chip_alpha
    else:
        chip_gray = cv2.cvtColor(chip_rgb, cv2.COLOR_BGR2GRAY)
        chip_mask = chip_alpha

    ch, cw = chip_gray.shape

    solid_ys, solid_xs = np.where(chip_mask > 100)
    if len(solid_xs) > 0:
        shape_cx = float(solid_xs.mean())
        shape_cy = float(solid_ys.mean())
    else:
        shape_cx, shape_cy = cw / 2.0, ch / 2.0
    print(f"   🌀 旋转中心: 形状质心 ({shape_cx:.1f},{shape_cy:.1f}) "
          f"vs 几何中心 ({cw/2:.1f},{ch/2:.1f})")

    tgt_cx = (ox + ow / 2.0) - x0
    tgt_cy = (oy + oh / 2.0) - y0

    rot_cache = {}

    def _get_rot(angle):
        if angle in rot_cache:
            return rot_cache[angle]
        M = cv2.getRotationMatrix2D((shape_cx, shape_cy), angle, 1.0)
        cos_a, sin_a = abs(M[0, 0]), abs(M[0, 1])
        nw = int(np.ceil(ch * sin_a + cw * cos_a))
        nh = int(np.ceil(ch * cos_a + cw * sin_a))
        M[0, 2] += nw / 2.0 - shape_cx
        M[1, 2] += nh / 2.0 - shape_cy
        rot = cv2.warpAffine(chip_gray, M, (nw, nh),
                             flags=cv2.INTER_LINEAR, borderValue=0)
        rot_m = cv2.warpAffine(chip_mask, M, (nw, nh),
                               flags=cv2.INTER_NEAREST, borderValue=0)
        rot_cache[angle] = (rot, rot_m, nw, nh)
        return rot_cache[angle]

    def _score(angle, off_x=0, off_y=0):
        rot, rot_m, nw, nh = _get_rot(angle)
        px = int(round(tgt_cx - nw / 2.0)) + off_x
        py = int(round(tgt_cy - nh / 2.0)) + off_y
        tx0, ty0 = max(0, px), max(0, py)
        tx1, ty1 = min(tw, px + nw), min(th, py + nh)
        if tx1 <= tx0 or ty1 <= ty0:
            return -1.0, rot, rot_m, px, py
        rx0, ry0 = tx0 - px, ty0 - py
        rx1 = rx0 + (tx1 - tx0); ry1 = ry0 + (ty1 - ty0)
        a = target[ty0:ty1, tx0:tx1].astype(np.float32)
        b = rot[ry0:ry1, rx0:rx1].astype(np.float32)
        m = rot_m[ry0:ry1, rx0:rx1] > 0
        if m.sum() < 200:
            return -1.0, rot, rot_m, px, py
        av = a[m] - a[m].mean()
        bv = b[m] - b[m].mean()
        d = float(np.sqrt((av * av).sum()) * np.sqrt((bv * bv).sum()))
        if d < 1e-6:
            return -1.0, rot, rot_m, px, py
        return float((av * bv).sum() / d), rot, rot_m, px, py

    best_angle, best_score = 0, -1.0
    best_rot = best_rot_m = None
    best_px = best_py = 0

    all_scores = {}
    for angle in range(0, 360, 3):
        s, rot, rot_m, px, py = _score(angle)
        all_scores[angle] = s
        if s > best_score:
            best_score, best_angle = s, angle
            best_rot, best_rot_m = rot, rot_m
            best_px, best_py = px, py

    for da in (-2, -1, 1, 2):
        angle = (best_angle + da) % 360
        if angle not in all_scores:
            s, rot, rot_m, px, py = _score(angle)
            all_scores[angle] = s
        else:
            s = all_scores[angle]
            rot, rot_m, px, py = _score(angle)[1:]
        if s > best_score:
            best_score, best_angle = s, angle
            best_rot, best_rot_m = rot, rot_m
            best_px, best_py = px, py

    best_off_x, best_off_y = 0, 0
    for dx in (-6, -4, -2, 2, 4, 6):
        for dy in (-6, -4, -2, 2, 4, 6):
            s, rot, rot_m, px, py = _score(best_angle, dx, dy)
            if s > best_score:
                best_score = s
                best_off_x, best_off_y = dx, dy
                best_rot, best_rot_m = rot, rot_m
                best_px, best_py = px, py
    if best_off_x or best_off_y:
        print(f"   🔧 平移细化: off=({best_off_x:+d},{best_off_y:+d}) ncc={best_score:.3f}")

    alt = (best_angle + 180) % 360
    alt_score = all_scores.get(alt, _score(alt)[0])
    print(f"   📐 双峰: best={best_angle}° ncc={best_score:.3f} | "
          f"alt={alt}° ncc={alt_score:.3f}")
    if alt_score > best_score + 0.08:
        s, rot, rot_m, px, py = _score(alt)
        best_score, best_angle = s, alt
        best_rot, best_rot_m = rot, rot_m
        best_px, best_py = px, py
        print(f"   🔀 采用 alt={alt}° (ncc {alt_score:.3f} 显著高于 {best_score:.3f})")
    elif abs(alt_score - best_score) < 0.08:
        choose_alt = best_angle > 180
        if choose_alt:
            s, rot, rot_m, px, py = _score(alt)
            best_score, best_angle = s, alt
            best_rot, best_rot_m = rot, rot_m
            best_px, best_py = px, py
            print(f"   🔀 双峰接近 (diff={abs(alt_score-best_score):.3f})，"
                  f"启发式选 {alt}°")
        else:
            print(f"   🔀 双峰接近 (diff={abs(alt_score-best_score):.3f})，保留 {best_angle}°")

    print(f"   🎯 rotate: 逆时针={best_angle}° ncc={best_score:.3f}")

    try:
        if best_rot is not None:
            vis = cv2.cvtColor(target, cv2.COLOR_GRAY2BGR)
            h2, w2 = best_rot.shape
            tx0, ty0 = max(0, best_px), max(0, best_py)
            tx1, ty1 = min(tw, best_px + w2), min(th, best_py + h2)
            rx0, ry0 = tx0 - best_px, ty0 - best_py
            rx1 = rx0 + (tx1 - tx0); ry1 = ry0 + (ty1 - ty0)
            m3 = (best_rot_m[ry0:ry1, rx0:rx1] > 0)
            sub = vis[ty0:ty1, tx0:tx1]
            sub[m3] = (sub[m3] * 0.5 +
                       np.array([0, 0, 255]) * 0.5).astype(np.uint8)
            cv2.imwrite(os.path.join(SCREENSHOT_DIR,
                                     f"rotate_vis_{tag}.png"), vis)
    except Exception as e:
        print(f"   ⚠️ 可视化: {e}")

    if best_score < ROTATE_NCC_SUBMIT:
        print(f"   ⚠️ NCC {best_score:.3f} 低于提交阈值 {ROTATE_NCC_SUBMIT}，放弃提交")
        return -1

    if ROTATE_MODE == "inverse":
        value = (360 - best_angle) % 360
    else:
        value = best_angle
    print(f"   📤 返回 value={value} (mode={ROTATE_MODE})")
    return value


def _solve_odd(bg_bytes, meta):
    """★ v26: 形状分支改用"两两 NCC 平均"算法"""
    bg = _decode(bg_bytes)
    if bg is None:
        raise RuntimeError("解码失败")
    rgb = bg[:, :, :3] if bg.ndim == 3 else cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)

    items = meta.get("items") or []
    if len(items) < 2:
        raise RuntimeError("items 不足")

    colors = []
    for it in items:
        x, y = int(it["x"]), int(it["y"])
        patch = rgb[max(0, y - 3):y + 6, max(0, x - 3):x + 6]
        colors.append(patch.reshape(-1, 3).astype(np.float32).mean(axis=0))
    colors = np.array(colors)

    n = len(items)
    dists = []
    for i in range(n):
        d = sum(float(np.linalg.norm(colors[i] - colors[j]))
                for j in range(n) if j != i)
        dists.append(d / (n - 1))

    ci = int(np.argmax(dists))
    mean_d = float(np.mean(dists))
    color_threshold = max(mean_d * 1.5, 180.0)
    if dists[ci] > color_threshold:
        print(f"   🎨 odd 颜色异类: item#{ci} {items[ci]} "
              f"dist={dists[ci]:.1f} avg={mean_d:.1f} thr={color_threshold:.1f}")
        return ci

    gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)
    patches = []
    for it in items:
        x, y, r = int(it["x"]), int(it["y"]), int(it.get("r") or 24)
        sub = gray[max(0, y - r):y + r, max(0, x - r):x + r]
        patches.append(sub.astype(np.float32))

    # ★ v26: 两两 NCC 平均
    h = min(p.shape[0] for p in patches)
    w = min(p.shape[1] for p in patches)
    rp = [cv2.resize(p, (w, h)) for p in patches]

    def _ncc_pair(a, b):
        av = a - a.mean()
        bv = b - b.mean()
        d = float(np.sqrt((av * av).sum()) * np.sqrt((bv * bv).sum()))
        return float((av * bv).sum() / d) if d > 1e-6 else 0.0

    ncc_mat = [[_ncc_pair(rp[i], rp[j]) for j in range(n)] for i in range(n)]
    avg_ncc = []
    for i in range(n):
        others_ncc = [ncc_mat[i][j] for j in range(n) if j != i]
        avg_ncc.append(sum(others_ncc) / len(others_ncc))

    si = int(np.argmin(avg_ncc))

    # 检查其它三个是否彼此相似
    others_idx = [j for j in range(n) if j != si]
    pairwise_ok = all(
        ncc_mat[a][b] > 0.6
        for a in others_idx for b in others_idx if a != b
    )

    print(f"   🎨 odd 形状异类: item#{si} {items[si]} avg_ncc={avg_ncc[si]:.3f} "
          f"其它三个 pairwise_ok={pairwise_ok}")
    print(f"      avg_ncc={[f'{x:.3f}' for x in avg_ncc]}")
    return si


def _match_map_calc(bg_bytes, meta, strategy=None):
    if strategy is None:
        strategy = MATCH_STRATEGY
    bg = _decode(bg_bytes)
    if bg is None:
        raise RuntimeError("解码失败")
    rgb = bg[:, :, :3] if bg.ndim == 3 else cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)
    gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)

    left = meta.get("left") or []
    right = meta.get("right") or []
    if len(left) != 3 or len(right) != 3:
        raise RuntimeError("match left/right 需各 3 个")

    def _patch(it):
        x, y, r = int(it["x"]), int(it["y"]), int(it.get("r") or 24)
        sub = gray[max(0, y - r):y + r, max(0, x - r):x + r]
        return sub.astype(np.float32)

    lp = [_patch(it) for it in left]
    rp = [_patch(it) for it in right]
    h = min(p.shape[0] for p in lp + rp)
    w = min(p.shape[1] for p in lp + rp)
    lp = [cv2.resize(p, (w, h)) for p in lp]
    rp = [cv2.resize(p, (w, h)) for p in rp]

    def _ncc(a, b):
        av = a - a.mean()
        bv = b - b.mean()
        d = float(np.sqrt((av * av).sum()) * np.sqrt((bv * bv).sum()))
        return float((av * bv).sum() / d) if d > 1e-6 else 0.0

    mat = [[_ncc(lp[i], rp[j]) for j in range(3)] for i in range(3)]
    print(f"   📊 NCC 矩阵 (策略={strategy}):  r0      r1      r2")
    for i in range(3):
        row = "  ".join(f"{mat[i][j]:+.2f}" for j in range(3))
        print(f"      l{i}: {row}")

    match_map = []
    used = set()
    for i in range(3):
        if strategy == "diff":
            sims = sorted([(mat[i][j], j) for j in range(3)])
        else:
            sims = sorted([(mat[i][j], j) for j in range(3)], reverse=True)
        for _, j in sims:
            if j not in used:
                used.add(j)
                match_map.append(j)
                break
        else:
            match_map.append(0)
    print(f"   🎨 match 配对: {match_map}")
    return match_map


def _solve_match(bg_bytes, meta):
    m = _match_map_calc(bg_bytes, meta)
    return m[0] * 100 + m[1] * 10 + m[2]


# ================= 交互 =================

def _box_pos_to_page(page, x, y):
    box = page.locator("#captcha_box_default")
    box.wait_for(state="visible", timeout=5000)
    bb = box.bounding_box()
    if not bb:
        raise RuntimeError("captcha box 不可见")
    meta_w = 300
    meta_h = 160
    px = bb["x"] + x / meta_w * bb["width"]
    py = bb["y"] + y / meta_h * bb["height"]
    return px, py


def _hover_to(page, tx, ty, steps=None):
    if steps is None:
        dist = 0
        if _MOUSE_POS["x"] is not None:
            dist = ((tx - _MOUSE_POS["x"]) ** 2 + (ty - _MOUSE_POS["y"]) ** 2) ** 0.5
        if dist < 80:
            steps = random.randint(6, 10)
        elif dist < 400:
            steps = random.randint(10, 18)
        else:
            steps = random.randint(16, 26)

    cx = _MOUSE_POS["x"] if _MOUSE_POS["x"] is not None else 640
    cy = _MOUSE_POS["y"] if _MOUSE_POS["y"] is not None else 360

    for i in range(1, steps + 1):
        t = i / steps
        eased = 1 - (1 - t) ** 2
        x = cx + (tx - cx) * eased + random.uniform(-1.5, 1.5)
        y = cy + (ty - cy) * eased + random.uniform(-1.5, 1.5)
        page.mouse.move(x, y)
        page.wait_for_timeout(random.randint(18, 38))
    page.mouse.move(tx + random.uniform(-0.5, 0.5), ty + random.uniform(-0.5, 0.5))
    page.wait_for_timeout(random.randint(60, 150))
    _MOUSE_POS["x"] = tx
    _MOUSE_POS["y"] = ty


def _click_captcha_point(page, x, y):
    px, py = _box_pos_to_page(page, x, y)
    box = page.locator("#captcha_box_default")
    bb = box.bounding_box()
    hx = bb["x"] + bb["width"] * random.uniform(0.2, 0.8)
    hy = bb["y"] + bb["height"] * random.uniform(0.2, 0.8)
    _hover_to(page, hx, hy)
    page.wait_for_timeout(random.randint(150, 320))

    px += random.gauss(0, 6)
    py += random.gauss(0, 6)

    _hover_to(page, px, py)
    page.wait_for_timeout(random.randint(80, 200))

    hold = random.choices(
        [random.randint(60, 100),
         random.randint(120, 220),
         random.randint(240, 380)],
        weights=[0.55, 0.35, 0.10])[0]
    page.mouse.down()
    page.wait_for_timeout(hold)
    page.mouse.up()
    page.wait_for_timeout(random.randint(200, 450))


def _click_match_pairs(page, meta, match_map):
    left = meta.get("left") or []
    right = meta.get("right") or []
    if len(left) != 3 or len(right) != 3:
        raise RuntimeError("match left/right 需各 3 个")

    box = page.locator("#captcha_box_default")
    bb = box.bounding_box()

    def _jitter(px, py):
        return px + random.gauss(0, 5), py + random.gauss(0, 5)

    def _human_click(px, py):
        near_x = px + random.uniform(-15, 15)
        near_y = py + random.uniform(-15, 15)
        _hover_to(page, near_x, near_y)
        page.wait_for_timeout(random.randint(120, 280))
        _hover_to(page, px, py)
        page.wait_for_timeout(random.randint(80, 200))

        hold = random.choices(
            [random.randint(60, 100),
             random.randint(120, 220),
             random.randint(240, 380)],
            weights=[0.55, 0.35, 0.10])[0]
        page.mouse.down()
        page.wait_for_timeout(hold)
        page.mouse.up()
        if random.random() < 0.20:
            page.wait_for_timeout(random.randint(80, 200))
            page.mouse.down()
            page.wait_for_timeout(random.randint(50, 130))
            page.mouse.up()

    for i in range(3):
        hx = bb["x"] + bb["width"] * random.uniform(0.15, 0.85)
        hy = bb["y"] + bb["height"] * random.uniform(0.15, 0.85)
        _hover_to(page, hx, hy)
        page.wait_for_timeout(random.randint(180, 420))

        lx, ly = _box_pos_to_page(page, left[i]["x"], left[i]["y"])
        lx, ly = _jitter(lx, ly)
        _human_click(lx, ly)

        if i == 1:
            page.wait_for_timeout(random.randint(1400, 2200))
        else:
            page.wait_for_timeout(random.randint(600, 1200))

        rj = right[match_map[i]]
        rx, ry = _box_pos_to_page(page, rj["x"], rj["y"])
        rx, ry = _jitter(rx, ry)
        _human_click(rx, ry)

        page.wait_for_timeout(random.randint(700, 1500))

    page.wait_for_timeout(random.randint(600, 1100))


def _drag_slider(page, value, vmax):
    track = page.locator("#captcha_track_default")
    track.wait_for(state="visible", timeout=5000)
    box = track.bounding_box()
    if not box:
        raise RuntimeError("track 不可见")

    hw = page.evaluate("""() => {
        const h = document.getElementById('captcha_handle_default');
        return h ? h.offsetWidth : 24;
    }""") or 24

    frac = max(0.0, min(1.0, value / max(1, vmax)))
    usable = max(1.0, box["width"] - hw)
    target_x = box["x"] + hw / 2 + frac * usable
    start_x = box["x"] + hw / 2
    y = box["y"] + box["height"] / 2
    dist = target_x - start_x

    page.mouse.move(start_x, y)
    _MOUSE_POS["x"], _MOUSE_POS["y"] = start_x, y
    page.wait_for_timeout(random.randint(80, 200))
    page.mouse.down()
    page.wait_for_timeout(random.randint(80, 180))

    seg_fracs = [0.60, 0.25, 0.15]
    seg_steps = [random.randint(14, 18), random.randint(8, 12), random.randint(8, 12)]
    cum = 0.0
    for si, (sf, ns) in enumerate(zip(seg_fracs, seg_steps)):
        seg_end = start_x + dist * (cum + sf)
        prev = start_x + dist * cum
        for i in range(1, ns + 1):
            t = i / ns
            eased = 1 - (1 - t) ** 2
            x = prev + (seg_end - prev) * eased + random.uniform(-1.2, 1.2)
            yy = y + random.uniform(-1.5, 1.5)
            page.mouse.move(x, yy)
            base_dt = (24, 60) if si == 2 else ((40, 90) if si == 1 else (18, 38))
            page.wait_for_timeout(random.randint(*base_dt))
            if si == 1 and random.random() < 0.25:
                page.mouse.move(x + random.uniform(-1, 1), yy + random.uniform(-1, 1))
                page.wait_for_timeout(random.randint(60, 150))
        cum += sf

    over = random.uniform(2, 5)
    safe_right = box["x"] + box["width"] - hw / 2 - 1
    page.mouse.move(min(target_x + over, safe_right), y + random.uniform(-2, 2))
    page.wait_for_timeout(random.randint(40, 80))
    back = random.uniform(1, 3)
    page.mouse.move(target_x - back, y + random.uniform(-1, 1))
    page.wait_for_timeout(random.randint(30, 70))
    if random.random() < 0.5:
        page.mouse.move(target_x - back + random.uniform(0.5, 1.5),
                        y + random.uniform(-0.5, 0.5))
        page.wait_for_timeout(random.randint(20, 50))
    page.mouse.move(target_x + random.uniform(-1, 1), y + random.uniform(-1, 1))
    page.wait_for_timeout(random.randint(30, 80))
    page.mouse.up()
    _MOUSE_POS["x"], _MOUSE_POS["y"] = target_x, y
    page.wait_for_timeout(random.randint(120, 300))


# ================= 一关处理 =================

def _handle_one_stage(page, meta, frames, tag="", align_idx=0):
    kind = meta.get("kind")
    alt = meta.get("alt")
    print(f"   🎯 kind={kind} nf={meta.get('nf')} "
          f"stage={meta.get('stage')}/{meta.get('stages')} alt={alt}")

    if kind not in SUPPORTED_KINDS:
        if alt in SUPPORTED_KINDS:
            try:
                btn = page.locator("#captcha_switch_default").first
                if btn.is_visible(timeout=1500):
                    page.wait_for_timeout(random.randint(900, 1800))
                    btn.click()
                    print(f"   🔁 切换类型: {kind} → {alt}")
                    return "switched"
            except Exception as e:
                print(f"   ⚠️ 切换失败: {e}")
        print(f"   ⚠️ {kind} 无法处理（alt={alt}），放弃会话")
        return False

    if kind in ("puzzle", "key"):
        if len(frames) < 2:
            print("   ⚠️ nf<2")
            return False
        try:
            value = _solve_puzzle(frames[0], frames[1], meta, align_idx=align_idx)
        except Exception as e:
            print(f"   ⚠️ puzzle 解算失败（{e}），尝试切换 {alt}")
            if alt in SUPPORTED_KINDS:
                try:
                    btn = page.locator("#captcha_switch_default").first
                    if btn.is_visible(timeout=1500):
                        page.wait_for_timeout(random.randint(900, 1800))
                        btn.click()
                        print(f"   🔁 切换类型: {kind} → {alt}")
                        return "switched"
                except Exception as e2:
                    print(f"   ⚠️ 切换失败: {e2}")
            return False
        print(f"   🧩 value={value} vmax={meta.get('vmax')}")
        _drag_slider(page, value, int(meta.get("vmax") or 300))
        return True

    if kind == "rotate":
        if len(frames) < 2:
            print("   ⚠️ nf<2")
            return False
        try:
            value = _solve_rotate(frames[0], frames[1], meta, tag=tag)
            if value < 0:
                print(f"   ⚠️ rotate 求解失败（NCC 过低），尝试切换 {alt}")
                if alt in SUPPORTED_KINDS:
                    try:
                        btn = page.locator("#captcha_switch_default").first
                        if btn.is_visible(timeout=1500):
                            page.wait_for_timeout(random.randint(900, 1800))
                            btn.click()
                            print(f"   🔁 切换类型: rotate → {alt}")
                            return "switched"
                    except Exception as e2:
                        print(f"   ⚠️ 切换失败: {e2}")
                return False
            print(f"   🎯 value={value} vmax={meta.get('vmax')}")
            _drag_slider(page, value, int(meta.get("vmax") or 359))
            return True
        except Exception as e:
            print(f"   ❌ rotate: {e}")
            return False

    if kind in ("odd", "match"):
        if len(frames) < 1:
            print("   ⚠️ nf<1")
            return False
        try:
            if kind == "odd":
                si = _solve_odd(frames[0], meta)
                items = meta.get("items") or []
                item = items[si]
                print(f"   🖱️ odd 点击 item#{si} ({item['x']},{item['y']})")
                _click_captcha_point(page, item["x"], item["y"])
            else:
                mm = _match_map_calc(frames[0], meta)
                ans = mm[0] * 100 + mm[1] * 10 + mm[2]
                print(f"   🖱️ match ans={ans}")
                _click_match_pairs(page, meta, mm)
            return True
        except Exception as e:
            print(f"   ❌ {kind}: {e}")
            return False

    print(f"   ⚠️ 未支持 kind: {kind}")
    return False


# ================= 多 stage 会话 =================

def _try_renew_session(page, attempt, initial_days):
    print(f"\n   {'='*40}\n   🔄 第 {attempt} 次会话\n   {'='*40}")

    try:
        btn = page.locator("button:has-text('Renew free')").first
        btn.wait_for(state="visible", timeout=8000)
        btn.click()
        print("   ✅ 已点击 Renew free")
    except Exception as e:
        print(f"   ❌ 未找到续期按钮: {e}")
        return None

    ok = False
    for _ in range(80):
        if WS_STATE["meta"] and WS_STATE["meta"].get("id"):
            ok = True
            break
        page.wait_for_timeout(200)
    if not ok:
        print(f"   ❌ 无 meta，last_resp={WS_STATE['last_resp']!r}")
        dump_page_debug(page, f"no_meta_{attempt}")
        return "ws_dead"

    handled_fps = set()
    switch_count = 0
    fail_count = 0
    last_action = time.time()
    start = time.time()
    max_total = 300

    while time.time() - start < max_total:
        if WS_STATE.get("closed"):
            # 2026-11-23：区分「连接层被踢」和「解题层失败」。
            # 一次答案都没发出就被关 = 出口 IP 被针对（脏 IP 特征），早放弃；
            # 有答过题但被拒 = 正常博弈，值得继续试（实测第 6 次才过）。
            if not WS_STATE.get("sent"):
                print("   ⚠️ WS 已关闭且未发出任何答案（出口被踢特征）")
                return "ws_dead"
            print("   ⚠️ WS 已关闭，退出会话")
            return None

        if WS_STATE.get("fail_pending"):
            WS_STATE["fail_pending"] = False
            fail_count += 1
            print(f"   ⚠️ 答案被拒 (fail #{fail_count}/{MAX_FAIL_PER_SESSION})")
            if fail_count >= MAX_FAIL_PER_SESSION:
                print(f"   ⚠️ fail 次数达上限，退出会话")
                return None
            handled_fps.clear()
            WS_STATE["frames"] = []
            page.wait_for_timeout(350)
            continue

        resp = WS_STATE["last_resp"]

        if resp == "rate":
            print("   ⚠️ 收到 rate 限流，本轮不再尝试")
            return None

        if resp and resp.startswith("ok:"):
            token = resp[3:]
            print(f"   🎉 全部通过！token 长度={len(token)}")

            save_screenshot(page, "confirm_before")

            confirm_ok = False
            for sel in ("button.btn-primary:has-text('Confirm Renewal')",
                         "button:has-text('Confirm Renewal')",
                         "button:has-text('Confirm')"):
                try:
                    confirm = page.locator(sel).first
                    confirm.wait_for(state="visible", timeout=6000)
                    confirm.click(timeout=3000)
                    print(f"   ✅ 已点击 Confirm Renewal ({sel})")
                    confirm_ok = True
                    break
                except Exception as e:
                    print(f"   ⚠️ Confirm 选择器 {sel} 失败: {str(e)[:120]}")
            if not confirm_ok:
                try:
                    page.locator("button:has-text('Confirm')").first.click(
                        timeout=3000, force=True)
                    print("   ✅ 已强制点击 Confirm")
                    confirm_ok = True
                except Exception as e:
                    print(f"   ❌ Confirm 未能点击: {e}")

            save_screenshot(page, "confirm_after")

            page.wait_for_timeout(3000)
            try:
                page.wait_for_url(
                    re.compile(r"/vps/"),
                    timeout=15000,
                    wait_until="domcontentloaded")
            except Exception:
                pass
            wait_for_cloudflare(page)

            save_screenshot(page, "confirm_result")

            new_days = None
            for _ in range(3):
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                wait_for_cloudflare(page)
                page.wait_for_timeout(2000)
                try:
                    text = page.locator("body").inner_text()
                except Exception:
                    text = ""
                m = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", text)
                if m:
                    candidate = int(m.group(1))
                    print(f"   📊 刷新后剩余: {candidate} 天")
                    if candidate > initial_days:
                        new_days = candidate
                        break
                page.wait_for_timeout(8000)

            if new_days is not None:
                print(f"   ✅ 续期成功！{initial_days} → {new_days} 天")
                return True

            # ★★★ v26: token 已通过但天数未变 → 先分清「24h 冷却」还是真异常。
            # Openworld FAQ：7 天周期，「Renewal is free and can be done once every
            # 24 hours to reset the countdown」。所以天数没变最常见的原因是冷却没过，
            # 那是正常节奏，不能算失败、更不能推高熔断计数。
            hint_blob = ""
            try:
                hint_blob = (page.locator("body").inner_text() or "").lower()
            except Exception:
                pass
            hint_blob += " " + str(resp or "").lower()
            cooldown_hits = ("24 hour", "24-hour", "once every", "cooldown",
                             "already renewed", "too soon", "rate limit", "429")
            if any(k in hint_blob for k in cooldown_hits):
                print("   ℹ️ 24 小时冷却未过，本轮跳过（正常，不计失败）")
                return "cooldown"

            print("   ⚠️ 验证码已过、Confirm 已点，但剩余天数未变（无冷却提示，判为异常）")
            print("   ℹ️ 不重试，按「已提交」处理")
            return "submitted"

        if resp in ("burned", "blocked", "rate"):
            print(f"   ❌ 服务端拒绝: {resp}")
            return None
        if resp and resp.startswith("bot:"):
            print(f"   ❌ bot: {resp}")
            return None
        meta = WS_STATE["meta"]
        if not meta or not meta.get("id"):
            page.wait_for_timeout(300)
            continue

        fp = (meta["id"], meta.get("stage"), meta.get("nf"))
        if fp in handled_fps:
            page.wait_for_timeout(300)
            if time.time() - last_action > 60:
                print("   ⚠️ 60s 无新状态，退出")
                return None
            continue

        nf = int(meta.get("nf") or 1)
        if len(WS_STATE["frames"]) < nf:
            page.wait_for_timeout(300)
            continue

        frames = list(WS_STATE["frames"])[:nf]
        handled_fps.add(fp)
        last_action = time.time()

        tag = f"a{attempt}_{meta['id'][:6]}"
        for i, fb in enumerate(frames):
            try:
                with open(os.path.join(
                    SCREENSHOT_DIR,
                    f"captcha_{tag}_{meta.get('kind')}_f{i}.png"), "wb") as f:
                    f.write(fb)
            except Exception:
                pass

        align_idx = ALIGN_STATE["counter"]
        result = _handle_one_stage(page, meta, frames,
                                   tag=tag, align_idx=align_idx)

        if result == "switched":
            switch_count += 1
            if switch_count > MAX_SWITCH_PER_SESSION:
                print("   ⚠️ 切换次数过多，退出")
                return None
            page.wait_for_timeout(random.randint(900, 1800))
            continue

        if not result:
            return None

        ALIGN_STATE["counter"] += 1
        page.wait_for_timeout(600)

    print(f"   ❌ 超时 {max_total}s")
    save_screenshot(page, f"renew_timeout_a{attempt}")
    return None


def try_renew_captcha(page, initial_days, max_attempts=12):
    """★ v26: 处理 "submitted" 特殊返回值

    2026-11-23 防复发：仍保留 max_attempts=12 的总上限，但加一条早退——
    连续 MAX_WS_DEAD 次「WS 一开就被关、一题都没答出去」就判定出口不可用、立刻放弃。
    不能直接把次数砍到 3~4：实测住宅出口下前 5 次都是 score 90~93 被拒、
    第 6 次才过，砍到 4 会把成功案例一起杀掉。
    """
    ws_dead_streak = 0
    for attempt in range(1, max_attempts + 1):
        _reset_align_pick()
        try:
            r = _try_renew_session(page, attempt, initial_days)
        except Exception as e:
            print(f"   ❌ 第 {attempt} 次会话异常: {e}")
            import traceback
            traceback.print_exc()
            r = None

        if r is True:
            return True

        # 连接层连败：出口 IP 大概率不可用，别再白耗（也少给风控积累指纹）
        if r == "ws_dead":
            ws_dead_streak += 1
            print(f"   ⚠️ 连接层失败 {ws_dead_streak}/{MAX_WS_DEAD}（未答出任何题）")
            if ws_dead_streak >= MAX_WS_DEAD:
                print(f"   ❌ 连续 {MAX_WS_DEAD} 次未发出答案即断连 → 判定出口不可用，提前放弃")
                return False
        else:
            ws_dead_streak = 0

        # ★★★ v26: token 通过但天数未变，停止重试
        if r == "cooldown":
            print(f"   ℹ️ 第 {attempt} 次会话命中 24 小时冷却，本轮跳过（正常）")
            return "cooldown"

        if r == "submitted":
            print(f"   ℹ️ 第 {attempt} 次会话已提交续期请求，停止重试")
            return "submitted"

        if attempt < max_attempts:
            _reset_ws_state()
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
                page.wait_for_timeout(random.randint(1500, 3000))
                page.reload(wait_until="domcontentloaded", timeout=30000)
                wait_for_cloudflare(page)
                page.wait_for_timeout(2000)
            except Exception as e:
                print(f"   ⚠️ 刷新: {e}")

    print(f"   ❌ {max_attempts} 次会话均失败")
    return False


def get_vps_urls(page):
    def extract():
        found = []
        try:
            for link in page.locator("a[href*='/vps/']").all():
                href = link.get_attribute("href") or ""
                if not href:
                    continue
                full = urllib.parse.urljoin(SITE_BASE, href)
                path = urllib.parse.urlparse(full).path.rstrip('/')
                parts = [p for p in path.split('/') if p]
                if len(parts) == 2 and parts[0] == "vps" and \
                   parts[1] not in ("list", "new", "create"):
                    if full not in found:
                        found.append(full)
        except Exception:
            pass
        return found

    print("\n🔍 识别 VPS 实例...")
    urls = extract()
    if not urls:
        try:
            page.goto(SITE_BASE, wait_until="domcontentloaded", timeout=30000)
            wait_for_cloudflare(page)
            page.wait_for_timeout(3000)
            urls = extract()
        except Exception:
            pass
    if urls:
        print(f"   ✅ {len(urls)} 个:")
        for u in urls:
            print(f"      - {u}")
    else:
        print("   ❌ 未找到")
        _diag_page(page, "未找到VPS")
    return urls


def check_config():
    print("=" * 50)
    print("⚙️  配置检查")
    print("=" * 50)
    print(f"   DISCORD_TOKEN  : {'✅ 已设置' if DISCORD_TOKEN else '❌ 空'}")
    print(f"   TG 通知        : {'✅ 已启用' if (TG_BOT_TOKEN and TG_CHAT_ID) else '⚪ 未启用'}")
    print(f"   SITE_BASE      : {SITE_BASE}")
    print(f"   运行模式       : {'✅ 无头' if HEADLESS else '❌ 有头'}")
    print(f"   截图目录       : {SCREENSHOT_DIR}")
    print(f"   MATCH_STRATEGY : {MATCH_STRATEGY}  (same / diff)")
    print(f"   ROTATE_MODE    : {ROTATE_MODE}  (direct / inverse)")

    print(f"   OPENWORLD_COOKIES: {'✅ 已设置' if OPENWORLD_COOKIES else '⚪ 未设置'}")
    print(f"   验证码自动解    : {'✅ 全自动（上游 WS + opencv 五类）' if ENABLE_CAPTCHA_SOLVE else '⚪ 关（跳过解算，转人工）'}")
    print(f"   SKIP_DISCORD    : {'✅ 开（跳过 Discord 授权）' if SKIP_DISCORD else '⚪ 关'}")
    print(f"   DUMP_DOM        : {'✅ 开（只抓结构）' if DUMP_DOM else '⚪ 关'}")
    print(f"   熔断阈值        : 连续 {MAX_FAIL_STREAK} 轮失败停手")

    if not DISCORD_TOKEN and not OPENWORLD_COOKIES:
        print()
        print("❌ DISCORD_TOKEN 与 OPENWORLD_COOKIES 都是空，没有任何登录方式")
        return False
    return True


def main() -> int:
    print("#" * 60)
    print("   Openworld VPS 自动续期 (v26-github)")
    print("#" * 60)

    if not check_config():
        return 1

    streak = read_fail_streak()
    if streak >= MAX_FAIL_STREAK:
        # 2026-11-23 防复发：已熔断 = 主动停手，不是故障。
        # 原实现 return 1 + 每天发一条 TG，结果 cron 照跑、run 照红、TG 照刷 ——
        # 熔断反而变成新的噪音源。这里改成静默早退（return 0）：
        #   · 触发熔断的那一轮已经发过 TG，不用每天重复；
        #   · 不报红，避免「明明是保护动作，却天天显示失败」。
        # 恢复方式不变：人工处理完把 FAIL_STREAK_FILE 归零。
        print(f"🛑 已熔断：连续 {streak} 轮失败（阈值 {MAX_FAIL_STREAK}），本轮静默跳过")
        print(f"   人工处理完把 {FAIL_STREAK_FILE} 归零即可恢复（不归零就一直静默）")
        return 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            proxy={
                "server": os.environ.get("CHROME_PROXY", ""),
            } if os.environ.get("CHROME_PROXY") else None,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/130.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            timezone_id="America/New_York",
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.new_page()
        _install_ws_hook(page)

        failures = 0
        try:
            if not login(page):
                send_telegram_message("❌ 登录流程失败")
                write_fail_streak(read_fail_streak() + 1)
                return 1

            vps_list = get_vps_urls(page)
            if not vps_list:
                send_telegram_message("❌ 未找到 VPS")
                return 1

            for idx, url in enumerate(vps_list, 1):
                print(f"\n{'=' * 50}")
                print(f"📌 [{idx}/{len(vps_list)}] {url}")
                print(f"{'=' * 50}")

                _reset_ws_state()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"⚠️ 加载: {e}")
                wait_for_cloudflare(page)
                page.wait_for_timeout(3000)

                if "/login" in page.url:
                    print("❌ 被重定向")
                    send_telegram_message("❌ 登录后仍被重定向")
                    break

                try:
                    text = page.locator("body").inner_text()
                except Exception:
                    text = ""

                # 未登录时站点会把页面跳到 /404 或 /login（前端 JS 直接改地址），
                # 那不是「实例不存在」，是会话死了。先按 URL 判，再按标题判。
                cur_url = page.url or ""
                logged_out = ("/login" in cur_url or "/404" in cur_url)
                if logged_out:
                    print(f"❌ 被弹到 {cur_url}：cookie/会话已失效（不是实例不存在）")
                    send_telegram_message(
                        f"❌ 会话失效\n实例: {url}\n页面被跳到 {cur_url}\n"
                        f"需要更新 DISCORD_TOKEN 或 OPENWORLD_COOKIES")
                    failures += 1
                    break

                if "404" in page.title() or "Page Not Found" in page.title():
                    print("❌ 404（会话仍在，判定为实例不存在）")
                    continue

                print("✅ 到达 VPS 页面")
                save_screenshot(page, f"vps_page_loaded_{idx}")

                if DUMP_DOM:
                    print("\n🧪 DUMP_DOM 已开启：只抓结构，不点续期、不碰验证码")
                    dump_dom(page, url)
                    continue

                m = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", text)
                if m:
                    days = int(m.group(1))
                    print(f"🔍 剩余: {days} 天")
                    if days > RENEW_THRESHOLD_DAYS:
                        print("⏳ 跳过续期")
                        # 老板这份的节律：未到窗口静默，只在要人介入时才发
                        print("   🔕 未到续期窗口，静默")
                        continue
                    print(f"⚠️ {days} ≤ {RENEW_THRESHOLD_DAYS}，开始续期")
                else:
                    print("⚠️ 未解析天数，强制尝试")
                    days = 0

                if not ENABLE_CAPTCHA_SOLVE:
                    print("⏭️ ENABLE_CAPTCHA_SOLVE=0：跳过自动解算，按需人工过验证码处理")
                    ok = False
                else:
                    print(f"\n{'=' * 50}\n🔄 验证码续期\n{'=' * 50}")
                    ok = try_renew_captcha(page, initial_days=days)

                if ok is True:
                    expiry = datetime.now(timezone(timedelta(hours=8))) + \
                             timedelta(days=6)
                    es = expiry.strftime("%Y-%m-%d %H:%M:%S") + " (GMT+8)"
                    print(f"✅ 续期成功，至: {es}")
                    send_telegram_message(
                        f"✅ 续期成功！\n实例: {url}\n续期至: {es}")
                elif ok == "cooldown":
                    # 24 小时冷却没过：正常节奏，不算失败，也不推高熔断计数
                    print("ℹ️ 24 小时冷却未过，本轮跳过（正常，不计失败）")
                    send_telegram_message(
                        f"ℹ️ 24 小时冷却未过，本轮跳过（正常）\n实例: {url}\n"
                        f"剩余 {days} 天，明天这一轮会再试")
                elif ok == "submitted":
                    print(f"⚠️ 续期请求已提交，等待服务端处理")
                    send_telegram_message(
                        f"⚠️ 续期请求已提交\n实例: {url}\n"
                        f"服务端未立即更新天数，可能需要延迟生效")
                    # submitted 不视为失败
                else:
                    print("❌ 续期失败")
                    failures += 1
                    send_telegram_message(f"❌ 续期失败\n实例: {url}")

        except KeyboardInterrupt:
            print("\n\n⚠️ 用户中断 (Ctrl+C)")
            try:
                save_screenshot(page, "user_interrupt")
            except Exception:
                pass
            return 1
        except Exception as e:
            print(f"\n💥 异常: {e}")
            import traceback
            traceback.print_exc()
            try:
                save_screenshot(page, "uncaught_error")
            except Exception:
                pass
            send_telegram_message(f"❌ 异常: {str(e)[:200]}")
            return 1
        finally:
            browser.close()
            print("\n🏁 执行完毕")

    if failures:
        streak = read_fail_streak() + 1
        write_fail_streak(streak)
        print(f"\n❌ 共 {failures} 台 VPS 续期失败（连续第 {streak} 轮）")
        if streak >= MAX_FAIL_STREAK:
            send_telegram_message(
                f"🛑 连续 {streak} 轮续期失败，已熔断停手\n"
                f"请人工登录面板处理，处理完把 {FAIL_STREAK_FILE} 归零（或删掉缓存）")
        return 1
    if read_fail_streak():
        write_fail_streak(0)
    print("\n✅ 全部 VPS 处理完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
