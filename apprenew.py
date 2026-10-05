#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Openworld VPS 到期預警守門（Watchdog）。

2026-09 起站方把續期驗證碼換成 WebSocket 互動式真人驗證（puzzle/rotate/key/
odd/match 多階段 + 行為檢測），自動續期在設計上已不可行、也不應該做
（那屬於繞過反自動化）。本腳本此後的職責是**守門**：Cookie 注入登入 →
偵測 VPS 狀態與剩餘天數 → 進入續期窗口時把人推到正確的頁面。

本轮迁移到 renew-kit（v0.5.0）的改动：

1. 配置与通知收口。删掉模块级 TG_CHAT_ID/TG_BOT_TOKEN、now_local() 与
   send_telegram_message()，改用 renewkit.env / renewkit.notify /
   renewkit.timeutil。消息文本、按钮语义、通知节律原样保留。

2. **修一个真 bug（有生产证据）**：面板返回 5xx 时，旧的 verify_logged_in()
   只看「URL 里有没有 /login」和「标题是不是 404」，于是 502 页面被判成
   「会话有效」；接着三次找 VPS 链接全落在 502 页面上，最后报成
   「面板搵唔到任何 VPS 實例」——把上游故障说成账号没机器。
   2026-09-30 run #82（title=Cloudflare Tunnel error）与 2026-10-02 run #85
   （title=502: Bad gateway）都是这个签名，两次都白标红 + 发错告警。
   现在按 renew-kit 的口径：上游 5xx / CF 隧道故障 -> TRANSIENT -> exit 0。

3. 退出码收敛到 renew-kit 的 Outcome：
       全部机器剩余 > 阈值            -> SKIPPED    exit 0（静默）
       进入续期窗口（需人工）          -> FAILED     exit 1（标红）
       面板 5xx / CF 故障              -> TRANSIENT  exit 0（只发一句实话）
       Cookie 失效 / 找不到实例 / 异常  -> FAILED     exit 1
   原来 exit 2（需人工）与 exit 1（真失败）在 Actions 里都是红，收敛后
   仍都是红，只是口径统一、报告可读。

4. **标红 ≠ 发通知**：通知节律由 alerts 列表单独控制。窗口中间日
   （阈值-1）故意静默（照抄原逻辑），但那天照样 FAILED 标红。

5. TG 只发一条：把本轮所有要说的行拼成一条消息，附上每台待续期机器的
   内联按钮。report.finish(notify_tg=False) 只负责打印报告与算退出码，
   通知由本脚本自己发 —— 消息要带按钮，而 finish() 目前还传不了 buttons。

6. 第 469–1126 行的 GIF 验证码/OCR 区块自 2026-09 起已无呼叫者（死代码：
   只有 try_renew_captcha 可达，而它无人调用）。本轮不动，留待另行决定；
   因为它在模块顶部 import PIL/numpy，workflow 的 pip 依赖暂时拆不掉。
"""
from __future__ import annotations

import os

# 静默 ONNX Runtime 底层 C++ 的设备扫描 Warning（device_discovery 噪音）
os.environ.setdefault("ORT_LOGGING_LEVEL", "3")

import io
import json
import re
import sys
import time
import traceback
import urllib.parse

import numpy as np
import requests
from PIL import Image
from playwright.sync_api import sync_playwright

from renewkit import env, notify, timeutil
from renewkit.outcome import Outcome
from renewkit.report import RenewReport, shorten

# ================= 配置区 =================
# 从 GitHub Secrets 环境变量获取 Discord Token
# （已于 2026-09 失效：官网认证由 Discord OAuth 迁到 Clerk + Google OAuth，
#   /discord-login 现返回 404，/login 用 @clerk/clerk-js@6）
DISCORD_TOKEN = env.get("DISCORD_TOKEN")

# 首选认证方式：人工登入一次后贴入的完整 Cookie 请求头字符串。
# 必须包含 httpOnly 的 Clerk 会话（__session / __client），
# 所以要用 DevTools → Network → 点任一 openworld.eu.org 请求 →
# 复制 Request Headers 里「Cookie: ...」那一整行的值。
# 留空则退回已失效的 Discord OAuth 流程（保留仅为兼容旧配置）。
OPENWORLD_COOKIES = env.get("OPENWORLD_COOKIES")  # 运行时回退读取 .env

# 网站根域
SITE_BASE = "https://openworld.eu.org"

# 续期天数阈值：剩余天数 <= 此值时才进入续期窗口
RENEW_THRESHOLD_DAYS = env.get_int("RENEW_THRESHOLD_DAYS", 5)
# ==========================================

# 截图保存目录（调试用）
SCREENSHOT_DIR = env.get("SCREENSHOT_DIR") or "."

SERVICE = "Openworld VPS"
PANEL_TARGET = "Openworld 面板"
DETAIL_LIMIT = 120

# 上游故障指纹。判据是「这页面根本不是我们的面板」，而不是「面板说了不行」。
# 与 renew-kit 的 TRANSIENT_STATUS 同源，另加 CF 隧道错误页的特征文案。
UPSTREAM_BAD_STATUS = frozenset({500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530})
UPSTREAM_TEXT_HINTS = (
    "502: bad gateway",
    "503 service unavailable",
    "504 gateway time-out",
    "bad gateway",
    "cloudflare tunnel error",
    "origin is unreachable",
    "web server is down",
    "error 522",
    "error 523",
    "error 1020",
)

# verify_logged_in() 的三态返回值。用 bool 会把「上游挂了」与「cookie 过期」
# 挤成同一个 False，正是 2026-09-30 / 2026-10-02 两次误报的土壤。
AUTH_OK = "ok"
AUTH_COOKIE_DEAD = "cookie_dead"
AUTH_UPSTREAM_DOWN = "upstream_down"


class UpstreamDown(RuntimeError):
    """面板 / Cloudflare 返回 5xx，本轮拿不到真实状态。

    单独定义一个异常而不是返回空列表：空列表的语义是「账号下确实没有
    实例」，那是要标红的业务结论；上游挂了则应该 TRANSIENT 放行。
    两者混在一起正是上面那两次误报的根因。
    """


def _slug(url: str) -> str:
    """从 VPS 详情页 URL 取实例标识。

    真实 URL 形如 /vps/e2ce269b-b14a-4beb-a851-b439b323828f，而旧代码在
    通知里写死了「vps-h6aad9」—— 那是过期的手抄值。改为从 URL 现取，
    多实例时也不会串。UUID 形的名字太长，显示前 8 位就够辨认；
    人读的名字（如 vps-h6aad9）原样保留。
    """
    raw = urllib.parse.urlparse(url or "").path.rstrip("/").rsplit("/", 1)[-1] or "vps"
    return raw[:8] if len(raw) > 12 else raw


def _target_name(url: str = "") -> str:
    """报告里的目标名。"""
    label = env.get("ACCOUNT_LABEL").strip()
    name = f"Openworld {_slug(url)}" if url else "Openworld"
    return f"{name}（{label}）" if label else name


def upstream_failure(page, status: int = 0) -> str:
    """页面是不是上游 / Cloudflare 故障页？是就返回一句人话，否则返回空串。

    status 取 page.goto() 返回的 Response.status；拿不到就传 0，纯靠文案判。
    """
    if status in UPSTREAM_BAD_STATUS:
        return f"HTTP {status}"
    try:
        title = (page.title() or "").strip()
        body = page.locator("body").inner_text(timeout=5000)[:2000].lower()
    except Exception:
        return ""
    blob = f"{title} {body}".lower()
    for hint in UPSTREAM_TEXT_HINTS:
        if hint in blob:
            return f"页面提示「{shorten(title, 60)}」" if title else hint
    return ""


def _note(report, target, outcome, *, expire=None, detail="") -> None:
    """所有 report.add 都走这里：detail 统一压平空白 + 截断。"""
    report.add(target, outcome, expire=expire,
               detail=shorten(" ".join(str(detail).split()), DETAIL_LIMIT))


def _esc(text) -> str:
    """转义 HTML 特殊字符。

    消息统一用 parse_mode=HTML 发送，而里面有几处动态内容（页面 title、
    异常消息）是不受控的 —— 一个「<」就能让 Telegram 400，整条告警丢掉。
    """
    return (str(text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def save_screenshot(page, name: str):
    """已禁用 PNG 截图保存（仅保留原始验证码 GIF 文件）"""
    pass


def wait_for_cloudflare(page, timeout=15):
    """
    等待 Cloudflare 挑战通过。
    如果页面包含 CF 挑战指示器，等待其消失。
    """
    cf_indicators = ["verify you are human", "just a moment", "checking your browser",
                     "cf-browser-verification", "challenge-platform"]
    start = time.time()
    while time.time() - start < timeout:
        try:
            content = page.content().lower()
            if not any(indicator in content for indicator in cf_indicators):
                return True
        except Exception:
            pass
        time.sleep(1)
    print("⚠️ Cloudflare 挑战等待超时")
    return False


def load_env_fallback(name: str) -> str:
    """读取环境变量，未设置时回退读取本目录 .env 文件。

    ⚠️ .env 含登录凭据，必须保持被 .gitignore 忽略、绝不提交。
    （历史上 .env 曾被误提交进 git 历史，凭据须视为已泄露并轮换。）
    运行区建议放在 NAS 持久目录，容器重置后凭据不丢。

    读环境变量走 renewkit.env.get（会自动 strip），别直接摸 os.environ ——
    这样「配置从哪来」只有一处口径。
    """
    val = env.get(name)
    if val:
        return val
    try:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == name:
                    return value.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def refresh_cookie_server_side(cookie_header: str, timeout: int = 10) -> str:
    """用 curl 打一次面板，把服务端 Set-Cookie 里刷新出的新值拼回完整 Cookie 头。

    Clerk 的 __session 是一个 JWT，exp 只有 60 秒左右；而 Actions runner 从
    checkout 到启动 Chromium 要几分钟，直接注入原始 cookie 时 JWT 早就过期了。
    但 Clerk 对每个请求都会在 Set-Cookie 里下发一个新的 __session，所以先在
    安装 Chromium 之前尽早刷一次，Playwright 拿到的就是新鲜会话。

    找不到新 Set-Cookie 时原样返回（不抛异常、不影响后续流程）。
    """
    import subprocess

    if not cookie_header.strip():
        return cookie_header
    try:
        proc = subprocess.run(
            [
                "curl", "-s", "-o", "/dev/null", "-D", "-",
                "-c", "/dev/null",
                "-H", f"Cookie: {cookie_header}",
                "--max-time", str(timeout),
                "https://openworld.eu.org/dashboard",
            ],
            capture_output=True, text=True, timeout=timeout + 10,
        )
    except Exception:
        return cookie_header

    raw = proc.stdout or ""
    if "set-cookie:" not in raw.lower():
        print("   🔄 Cookie 刷新：服务端未下发 Set-Cookie，沿用原值")
        return cookie_header

    updated = {}
    for line in raw.splitlines():
        if line.lower().startswith("set-cookie:"):
            val = line.split(":", 1)[1].strip()
            if "=" not in val:
                continue
            updated[val.split("=", 1)[0].strip().lower()] = val.split("=", 1)[1].split(";")[0].strip()

    if not updated:
        return cookie_header

    new_header = cookie_header
    for key, val in updated.items():
        pat = re.compile(rf"(?i)(?:^|;\s*){re.escape(key)}=.*?(?=;\s*|$)")
        if pat.search(new_header):
            new_header = pat.sub(f"{key}={val}", new_header)
        else:
            new_header = f"{new_header.rstrip(';')} ; {key}={val}"
    print(f"   🔄 Cookie 已由服务端刷新：更新字段 {sorted(updated)}")
    return new_header


COOKIE_CACHE_FILE = env.get("COOKIE_CACHE_FILE", "ow_cookie_cache.txt")


def load_cached_cookies() -> str:
    """读 actions/cache 里存回来的 cookie（跨 run 持久化）。

    为什么要有 cache：GHA fork 仓对 secrets API 永久 403，写 secret 这条路
    封死；cache 是唯一不改仓库结构就能跨 run 维持会话的地方。cache 里的
    cookie 比 secret 新（每轮服务端刷新后写回），所以作首选凭证。

    但 cache 只作「快路」，不是单点——见 authenticate_candidates()：
    快路必须自带可达的失效兜底，否则一次 cache miss / 一次被淘汰就变成永久故障。
    """
    try:
        with open(COOKIE_CACHE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def save_cached_cookies(cookie_header: str) -> None:
    """把当前可用的 cookie 写回本地文件，由 workflow 末尾的 save cache 收走。

    只在认证真跑通（AUTH_OK）后调用——写一份坏 cookie 进 cache 等于给下一轮
    埋一颗地雷。全程不打印 value。
    """
    body = (cookie_header or "").strip()
    if not body:
        return
    try:
        with open(COOKIE_CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(COOKIE_CACHE_FILE, 0o600)
        print(f"   💾 已把可用 cookie 写回 cache 文件（{len(body)} 字符，{COOKIE_CACHE_FILE}）")
    except Exception as e:
        print(f"   ⚠️ 写回 cache 文件失败（本轮仍可用，下次回落 secret）: {e}")


def authenticate_candidates(context, page) -> tuple:
    """按「cache → secret」顺序逐个试 cookie，返回 (状态, 说明, 生效的 cookie)。

    快路（cache）永远配一条可达的兜底（secret）：cache 被驱逐、被服务端淘汰，
    或者用户刚更新了 secret 而 cache 里的旧值还在时，都要能自动退到下一条，
    而不是直接判死。两条都失败才回报失败。

    每条候选都先过 refresh_cookie_server_side()——Clerk 的 __session 只有约
    60 秒寿命，checkout 到起 Chromium 要几分钟，不先刷一次注进去的 JWT 早死了。
    """
    candidates = []
    for label, value in (("cache", load_cached_cookies()),
                         ("secret", (OPENWORLD_COOKIES or "").strip())):
        if value and value not in [v for _, v in candidates]:
            candidates.append((label, value))

    if not candidates:
        return AUTH_COOKIE_DEAD, "無憑證（cache 與 OPENWORLD_COOKIES 都係空）", ""

    last_reason = ""
    for label, value in candidates:
        print(f"\n🔑 嘗試 {label} cookie（{len(value)} 字符）")
        refreshed = refresh_cookie_server_side(value)
        if not login_with_cookies(context, refreshed):
            last_reason = f"{label} cookie 注入失敗"
            continue
        auth_state, auth_reason = verify_logged_in(page)
        if auth_state == AUTH_OK:
            if label == "secret":
                # secret 比 cache 新（用戶剛換過）：把新值同步回 cache，
                # 否則這份舊 cache 會一直壓住新 secret。
                print("   🔄 secret 比 cache 新，同步寫回 cache")
            save_cached_cookies(refreshed)
            return AUTH_OK, "", refreshed
        if auth_state == AUTH_UPSTREAM_DOWN:
            # 面板 5xx / CF 故障：换凭证也没用，本轮拿不到真实状态，交由调用方按上游故障处理
            return auth_state, f"{label}：{auth_reason}", ""
        last_reason = f"{label}：{auth_reason}"
        print(f"   ⚠️ {last_reason}")

    return AUTH_COOKIE_DEAD, last_reason or "憑證全部失效", ""


def login_with_cookies(context, cookie_header: str) -> bool:
    """用贴入的 Cookie 请求头字符串注入会话，取代已失效的 Discord OAuth 登录。

    官网 2026-09 将认证从 Discord OAuth 迁移到 Clerk + Google OAuth
    （/discord-login 返回 404，/login 用 @clerk/clerk-js@6，并新增
    window.__owFp 反自动化指纹）。GitHub Actions 的 Azure 机房 IP 过不了
    Google OAuth，所以改为人工登录一次后注入完整 Cookie（含 httpOnly 的
    Clerk 会话）。
    """
    raw = (cookie_header or "").strip()
    if not raw:
        return False

    cookies = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": "openworld.eu.org",
            "path": "/",
        })

    if not cookies:
        print("❌ OPENWORLD_COOKIES 无效：解析不到任何 cookie")
        return False

    try:
        context.clear_cookies()
        context.add_cookies(cookies)
    except Exception as e:
        print(f"❌ 注入 cookie 失败: {e}")
        return False

    print(f"   已注入 {len(cookies)} 个 cookie: {[c['name'] for c in cookies]}")
    return True


def verify_logged_in(page) -> tuple:
    """确认注入的 cookie 真有有效会话（而不是被弹回登录页）。

    返回 (状态, 说明)：状态是 AUTH_OK / AUTH_COOKIE_DEAD / AUTH_UPSTREAM_DOWN。

    为什么要三态：旧版只看「URL 里有没有 /login」和「标题是不是 404」，
    于是 502 页面被判成「会话有效」放行，最后报成「面板找不到 VPS」。
    上游挂了与 cookie 失效必须分开，两者的处置完全不同。
    """
    upstream = ""
    for path in ("/dashboard", "/vps"):
        try:
            resp = page.goto(f"{SITE_BASE}{path}", wait_until="domcontentloaded",
                             timeout=30000)
            wait_for_cloudflare(page)
            time.sleep(2)
            cur = page.url
            title = page.title() or ""
            status = resp.status if resp else 0
            print(f"   检查 {path}: URL={cur} | title={title} | HTTP {status}")

            reason = upstream_failure(page, status)
            if reason:
                print(f"   🌐 {path} 是上游故障页（{reason}），本轮判断不了会话")
                upstream = f"{path} {reason}"
                continue

            if "/login" in cur or "/signin" in cur:
                print("❌ 被重定向到登录页：cookie 已失效")
                save_screenshot(page, "cookie_expired")
                return AUTH_COOKIE_DEAD, "被重定向到登录页"
            if "404" in title or "Page Not Found" in title:
                print(f"   ⚠️ {path} 返回 404，试下一个路径")
                continue
            print(f"   ✅ 会话有效（{path}）")
            return AUTH_OK, ""
        except Exception as e:
            print(f"   ⚠️ 检查 {path} 异常: {e}")
    if upstream:
        return AUTH_UPSTREAM_DOWN, upstream
    print("❌ 没有任何面板路径可进入：cookie 可能失效")
    save_screenshot(page, "cookie_expired")
    return AUTH_COOKIE_DEAD, "没有任何面板路径可进入"


def login_with_discord_token(page, dc_token: str) -> bool:
    """
    通过 Discord Token 完成 OAuth 登录到 openworld.eu.org。
    
    流程：
    1. 访问 /discord-login 触发服务端 302 重定向到 Discord OAuth 页面
    2. 从重定向后的 URL 中提取 OAuth 参数（client_id, redirect_uri, scope, state）
    3. 使用 Discord Token 通过 API 直接完成授权
    4. 用返回的回调 URL 完成登录
    """
    print("=" * 50)
    print("🔑 开始 Discord OAuth 登录流程")
    print("=" * 50)

    # ========== 第1步：触发 Discord OAuth 重定向 ==========
    # openworld.eu.org 的登录按钮指向 /discord-login，
    # 服务端会 302 重定向到 Discord 的 OAuth2 授权页面
    discord_login_url = f"{SITE_BASE}/discord-login"
    print(f"\n📌 第1步：访问 Discord 登录入口: {discord_login_url}")

    try:
        # 先访问首页建立基础 cookie/session
        page.goto(SITE_BASE, wait_until="domcontentloaded", timeout=30000)
        wait_for_cloudflare(page)
        time.sleep(2)
        print(f"   首页加载完成，当前 URL: {page.url}")

        # 访问 /discord-login，这会触发 302 到 Discord
        page.goto(discord_login_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
    except Exception as e:
        print(f"   ⚠️ 页面加载异常: {e}")
        # 即使超时也可能已经跳转了，继续检查

    current_url = page.url
    print(f"   跳转后 URL: {current_url}")

    # ========== 第2步：检查是否到达了 Discord 授权页 ==========
    print(f"\n📌 第2步：检查 Discord OAuth 页面")

    # 如果还在 openworld 的登录页，尝试点击 Discord 按钮
    if "discord.com" not in current_url:
        print("   未自动跳转到 Discord，尝试在登录页查找 Discord 按钮...")
        save_screenshot(page, "before_discord_click")

        try:
            # 查找登录页上的 Discord 登录链接/按钮
            discord_btn = page.locator("a[href*='discord-login'], a[href*='discord'], a:has-text('Discord')").first
            if discord_btn.is_visible(timeout=5000):
                href = discord_btn.get_attribute("href")
                print(f"   找到 Discord 按钮，href={href}")
                discord_btn.click()
                time.sleep(5)
                current_url = page.url
                print(f"   点击后 URL: {current_url}")
        except Exception as e:
            print(f"   ⚠️ 查找/点击 Discord 按钮失败: {e}")

    # 再次检查
    if "discord.com" not in current_url:
        # 最后尝试：有些网站的 /discord-login 可能需要处理 Cloudflare
        print("   仍未到达 Discord，等待可能的延迟重定向...")
        for i in range(10):
            time.sleep(1)
            current_url = page.url
            if "discord.com" in current_url:
                break
        
        if "discord.com" not in current_url:
            print(f"   ❌ 无法跳转到 Discord 授权页面")
            print(f"   当前 URL: {current_url}")
            print(f"   页面标题: {page.title()}")
            save_screenshot(page, "login_failed_no_discord")
            return False

    # ========== 第3步：从 URL 解析 OAuth 参数 ==========
    print(f"\n📌 第3步：解析 OAuth 参数")
    oauth_url = page.url
    print(f"   Discord OAuth URL: {oauth_url[:100]}...")

    parsed = urllib.parse.urlparse(oauth_url)
    params = urllib.parse.parse_qs(parsed.query)

    client_id    = params.get("client_id", [""])[0]
    redirect_uri = params.get("redirect_uri", [""])[0]
    scope        = params.get("scope", ["identify email"])[0]
    state        = params.get("state", [""])[0]
    response_type = params.get("response_type", ["code"])[0]

    print(f"   Client ID:    {client_id}")
    print(f"   Redirect URI: {redirect_uri}")
    print(f"   Scope:        {scope}")
    print(f"   State:        {state[:20]}..." if state else "   State:        (空)")

    if not client_id or not redirect_uri:
        print("   ❌ 无法解析关键 OAuth 参数 (client_id 或 redirect_uri)")
        save_screenshot(page, "login_failed_parse")
        return False

    # ========== 第4步：通过 API 完成 Discord 授权 ==========
    print(f"\n📌 第4步：通过 Discord API 完成授权")

    # 构建 API URL
    api_params = urllib.parse.urlencode({
        "client_id":     client_id,
        "response_type": response_type,
        "redirect_uri":  redirect_uri,
        "scope":         scope,
        "state":         state,
    })
    authorize_api = f"https://discord.com/api/v9/oauth2/authorize?{api_params}"

    # 构建 referer
    referer_params = urllib.parse.urlencode({
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": response_type,
        "scope":         scope,
        "state":         state,
    })
    referer = f"https://discord.com/oauth2/authorize?{referer_params}"

    headers = {
        "accept":           "*/*",
        "authorization":    dc_token.strip(),
        "content-type":     "application/json",
        "origin":           "https://discord.com",
        "referer":          referer,
        "user-agent":       ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        "x-discord-locale": "zh-CN",
    }

    body = {
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    }

    try:
        resp = requests.post(authorize_api, headers=headers, json=body, timeout=20)
        print(f"   API 响应状态码: {resp.status_code}")

        if resp.status_code != 200:
            print(f"   ❌ Discord 授权失败: HTTP {resp.status_code}")
            print(f"   响应内容: {resp.text[:300]}")
            return False

        resp_data = resp.json()
    except Exception as e:
        print(f"   ❌ Discord API 请求异常: {e}")
        return False

    location = resp_data.get("location", "")
    if not location:
        print(f"   ❌ 授权响应中未找到 location 字段")
        print(f"   响应内容: {json.dumps(resp_data, ensure_ascii=False)[:300]}")
        return False

    masked_location = re.sub(r"code=[^&]+", "code=***", location)
    print(f"   ✅ 拿到回调 URL: {masked_location}")

    # ========== 第5步：用回调 URL 完成登录 ==========
    print(f"\n📌 第5步：通过回调 URL 完成登录写入 Cookie")

    try:
        page.goto(location, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"   ⚠️ 回调页面加载异常（可能正常）: {e}")

    time.sleep(5)
    wait_for_cloudflare(page)

    final_url = page.url
    print(f"   回调后 URL: {final_url}")

    # 检查是否登录成功
    if "/login" in final_url and "discord" not in final_url:
        print("   ⚠️ 回调后仍在登录页，登录可能失败")
        save_screenshot(page, "login_callback_stuck")
        # 有些情况下需要等待更久
        time.sleep(5)
        final_url = page.url
        if "/login" in final_url:
            print(f"   ❌ 登录最终失败，停留在: {final_url}")
            return False

    if "openworld.eu.org" in final_url:
        print(f"   ✅ 登录成功！当前 URL: {final_url}")
        save_screenshot(page, "login_success")
        return True

    print(f"   ⚠️ 登录状态不确定，当前 URL: {final_url}")
    save_screenshot(page, "login_uncertain")
    # 尝试继续，后续访问 VPS 页面会验证
    return True


def extract_gif_frames(gif_bytes: bytes) -> list:
    """提取 GIF 所有帧为 PIL Image 列表"""
    gif = Image.open(io.BytesIO(gif_bytes))
    frames = []
    try:
        while True:
            frame = gif.convert("L")  # 转灰度
            frames.append(frame.copy())
            gif.seek(gif.tell() + 1)
    except EOFError:
        pass
    print(f"   📊 成功提取 GIF 共 {len(frames)} 帧")
    return frames


def preprocess_frame(img: Image.Image) -> Image.Image:
    """对单帧图像进行预处理：二值化 + 放大"""
    threshold = 170
    binary = img.point(lambda p: 0 if p < threshold else 255, "L")
    w, h = binary.size
    binary = binary.resize((w * 2, h * 2), Image.LANCZOS)
    return binary



# 前景像素佔比低於呢個值就當近乎空白幀跳過。
# 實測（run 34321580401 的 5 張 artifact × 25 幀）：淡入淡出式驗證碼嘅最弱幀
# 只有 1.7-2.4% 畫布有內容，而且連通域分析顯示佢哋 0 個有效字塊；
# 對佢哋做 OCR 只會得到 garbage 並污染投票。4.5% 以上則全部有字塊。
MIN_FOREGROUND_RATIO = 0.03

# 每幀試幾組 (二值化閾值, 放大倍率)。不同幀對比度唔同，多跑幾組提高命中率。
OCR_VARIANTS = [(170, 2), (140, 3), (200, 2)]

# OCR 常見錯別字 → 數字。
# 已合併上游 09-01/09-04 實測別名：補 c→0 / d→0 / >→7；
# 移除 T/t→7（上游實測 T/t 更常係運算符「+」的上半部，唔係 7）。
DIGIT_MAP = {
    '0': '0', 'O': '0', 'o': '0', 'D': '0', 'C': '0', 'c': '0', 'd': '0',
    '1': '1', 'l': '1', 'I': '1', '|': '1', '!': '1', 'i': '1',
    '2': '2', 'Z': '2', 'z': '2',
    '3': '3',
    '4': '4',
    '5': '5', 'S': '5', 's': '5',
    '6': '6', 'b': '6', 'G': '6', '&': '6',
    '7': '7', '>': '7',
    '8': '8', 'B': '8',
    '9': '9', 'q': '9', 'Q': '9', 'g': '9', 'y': '9',
}

# 運算符別名。上游實測：「+」上半部／倒立 T 常被讀成 t/T/┴/⊥/丄；
# 長橫線讀成 「—」/「–」/「一」；乘號讀成 x/X/×/y。
OP_MAP = {
    '+': '+', '十': '+', 't': '+', 'T': '+', '┴': '+', '⊥': '+', '丄': '+',
    '-': '-', '—': '-', '–': '-', '一': '-',
    '*': '*', 'x': '*', 'X': '*', '×': '*', 'y': '*',
    '/': '/', '÷': '/',
}


def frame_foreground_ratio(img: Image.Image) -> float:
    """前景（灰度 < 170）像素佔比"""
    arr = np.array(img)
    return float((arr < 170).sum() / arr.size) if arr.size else 0.0


def normalize_expression(s: str) -> str:
    """把 OCR 原文正規化成只含數字同 + - * / 嘅算式字串，其餘一律丟棄。
    運算符／數字別名映射見 OP_MAP / DIGIT_MAP（已合併上游 09-01 實測別名）。"""
    s = s.replace('−', '-').replace(':', '/')
    out = []
    for ch in s:
        if ch in OP_MAP:
            out.append(OP_MAP[ch])
        elif ch in DIGIT_MAP:
            out.append(DIGIT_MAP[ch])
    return ''.join(out)


def normalize_captcha_number(s: str) -> str:
    """Openworld 驗證碼數字範圍上限係 12，唔係 9。

    OCR 容易因運算符向左／右偏移把運算符讀成第二位數字，
    產生 13~99 嘅偽數位。按範圍回退成真實數字（上游 09-04 實測）：
        13~19 -> 1；20~29 -> 2；... 90~99 -> 9；>99 -> 取首位；<=12 -> 保持。
    """
    if not s or not s.isdigit():
        return s
    val = int(s)
    if val <= 12:
        return str(val)
    if 13 <= val <= 19:
        return "1"
    if 20 <= val <= 99:
        return str(val // 10)
    return s[0]


def solve_expression(expr: str):
    """'12+7' -> 19；解析唔到就返 None"""
    m = re.fullmatch(r'(\d+)\s*([+\-*/])\s*(\d+)', expr.strip())
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    if b > 1000:
        return None
    try:
        if op == '+':
            return a + b
        if op == '-':
            return a - b
        if op == '*':
            return a * b
        return int(a / b) if b else None
    except Exception:
        return None


def recognize_captcha_by_frames(gif_bytes: bytes, ocr) -> str:
    """
    整幀 OCR + 空白幀過濾 + 整條算式加權多數投票。

    舊版按固定 42% / 35% / 58% 把 140px 寬切成 Left / Middle / Right 三塊，
    然後三個區域各自投票再拼接。實測（run 34321580401 的 5 張 artifact × 25 幀
    連通域分析）顯示字塊位置每幀都唔同：
        raw_1 f0: 17-29 / 42-55 / 88-103 / 110-126（4 個字塊，3 個喺 58px 以內）
        raw_2 f1: 46-65（只一個）
        raw_3 f0: 29-40 / 41-65 / 97-116
        raw_5 f0: 11-42 / 43-66 / 44-106 / 105-116
    固定百分比必然切錯位，結果運算符 4/5 次讀成空白並靜默回退成 '+'，
    5 個答案入面有 4 個嘅算式係編出來嘅。

    新版：
      1. 前景佔比 < MIN_FOREGROUND_RATIO 嘅幀跳過。
      2. 對剩返嘅幀做整幀 OCR，唔再切區域。
      3. 每幀跑 OCR_VARIANTS 幾組預處理，票數按前景佔比加權
         （前景越多代表畫面对，權重越大）。
      4. 對整條算式字串投票，唔係左/中/右分開投票。
    """
    frames = extract_gif_frames(gif_bytes)
    if not frames:
        return ""

    from collections import Counter

    votes = Counter()
    raw_samples = []
    used = 0

    for idx, frame in enumerate(frames):
        ratio = frame_foreground_ratio(frame)
        if ratio < MIN_FOREGROUND_RATIO:
            print(f"   ⏭️  f{idx} 前景僅 {ratio * 100:.1f}% "
                  f"(< {MIN_FOREGROUND_RATIO * 100:.0f}%)，跳過空白幀")
            continue
        weight = max(1, int(ratio * 25))   # 12% -> 3 票；3% -> 1 票
        used += 1

        for th, sc in OCR_VARIANTS:
            proc = frame.point(lambda p, t=th: 0 if p < t else 255, "L")
            w, h = proc.size
            proc = proc.resize((w * sc, h * sc), Image.LANCZOS)
            buf = io.BytesIO()
            proc.save(buf, format="PNG")

            res = ocr.classification(buf.getvalue()).strip()
            norm = normalize_expression(res)
            raw_samples.append((idx, th, sc, res))
            print(f"   🔎 f{idx} th{th}x{sc} w{weight}: OCR={res!r} -> {norm!r}")
            if norm:
                votes.update({norm: weight})

    if not votes:
        print("   ⚠️ 無有效 OCR 結果")
        for idx, th, sc, res in raw_samples:
            print(f"      f{idx} th{th}x{sc}: {res!r}")
        return ""

    print(f"   🗳️ 有效幀 {used}/{len(frames)}，投票 {dict(votes.most_common())}")

    # 第 1 步：運算符讀漏時補 '-'/'+' 重試（必須喺範圍正規化之前，
    # 否則 '57' 會被 normalize 成單一數字 '5'，運算符永遠補唔返）。
    # 讀漏後候選通常係一個連續數字串（如 '57'），按合法拆法試；
    # Openworld 數字上限 12，右段唔容許前導 0（'07' 唔係合法數字）。
    # 補運算符屬於猜測，放低優先：只有正常候選全部解唔到先會用到。
    normal_votes = Counter()
    guessed_votes = Counter()
    for cand, cnt in votes.items():
        s = cand.strip()
        if re.fullmatch(r'\d+', s) and len(s) >= 2:
            found = False
            for split in range(1, len(s)):
                left, right = s[:split], s[split:]
                if int(left) <= 12 and int(right) <= 12 \
                        and not (len(right) > 1 and right[0] == '0'):
                    for op in ('-', '+'):
                        guessed_votes[left + op + right] += cnt
                    found = True
            if found:
                print(f"   🔁 運算符讀漏，按合法拆法補運算符：{cand!r}")
        normal_votes[cand] += cnt
    # 第 2 步：0~12 範圍正規化（運算符偏移誤讀成第二位數字，如 57 → 5）。
    # 先試正常候選，全部解唔到先降到「補運算符」嘅猜測候選。
    for label, src in (("正常", normal_votes), ("補運算符", guessed_votes)):
        norm = Counter()
        for cand, cnt in src.items():
            parts = re.findall(r'\d+|[+\-*/]', cand)
            nc = ''.join(normalize_captcha_number(p) if p.isdigit() else p for p in parts)
            if nc:
                norm[nc] += cnt

        print(f"   🗳️ {label}候選範圍正規化後：{dict(norm.most_common())}")

        for cand, cnt in norm.most_common():
            val = solve_expression(cand)
            if val is not None:
                print(f"   ✅ {label}投票 -> {cand!r} (票數 {cnt}) = {val}")
                return str(val)

    print("   ⚠️ 所有候選都無法解析成 A op B")
    return ""


def init_ddddocr():
    """初始化 ddddocr 實例，並在初始化期間把 C++ 層 stderr (fd 2)
    重定向到 /dev/null，徹底杜絕 ONNX Runtime device_discovery 警告噪音。
    （上游 09-03 加入）"""
    old_stderr_fd = None
    try:
        null_fd = os.open(os.devnull, os.O_WRONLY)
        old_stderr_fd = os.dup(2)
        os.dup2(null_fd, 2)
        os.close(null_fd)
    except Exception:
        pass
    try:
        import ddddocr
        return ddddocr.DdddOcr(show_ad=False)
    except ImportError:
        return None
    finally:
        if old_stderr_fd is not None:
            try:
                os.dup2(old_stderr_fd, 2)
                os.close(old_stderr_fd)
            except Exception:
                pass


def diagnose_captcha(page) -> dict:
    """侦察 2026-09 改版后的新版交互验证码，只采集数据不做求解。

    旧版是 140x40 的数学式动画 GIF，用 ddddocr 做 OCR。改版后变成
    WebSocket 下发的 5 种交互类型，且带行为反爬：
      puzzle = 拖动碎片到背景缺口（或用下方滑块）
      rotate = 旋转碎片直到正立
      key    = 把碎片拖到匹配的形状上
      odd    = 点选不属于的那一个
      match  = 逐个点选左右两项再点其配对项
    另外有 navigator.webdriver 检查、browser_fp 指纹上报、鼠标轨迹行为门控。
    """
    info = {"hint": "", "kind": "unknown", "vmax": None, "id": "",
            "box_size": None, "n_img": 0, "webdriver": None}

    box = page.locator("div[id^='captcha_box']").first
    try:
        if not box.is_visible(timeout=8000):
            print("   ⚠️ 未找到验证码框 div[id^='captcha_box']")
            return info
    except Exception:
        print("   ⚠️ 验证码框不可见")
        return info

    try:
        bb = box.bounding_box()
        info["box_size"] = (round(bb["width"]), round(bb["height"])) if bb else None
    except Exception:
        pass
    try:
        info["hint"] = (page.locator("div[id^='captcha_hint']").first
                        .inner_text(timeout=3000) or "").strip()
    except Exception:
        pass
    try:
        info["vmax"] = page.locator("div[id^='captcha_track']").first.get_attribute("aria-valuemax")
    except Exception:
        pass
    try:
        info["id"] = page.locator("input[id^='captcha_id']").first.get_attribute("value") or ""
    except Exception:
        pass
    try:
        info["n_img"] = page.locator("img[id^='captcha_']").count()
    except Exception:
        pass

    kind_hints = {
        "Drag the piece into the gap": "puzzle",
        "Rotate the artifact": "rotate",
        "Drag the chip onto the matching shape": "key",
        "Tap the one that doesn't belong": "odd",
        "Click each item": "match",
    }
    for frag, kind in kind_hints.items():
        if frag.lower() in info["hint"].lower():
            info["kind"] = kind
            break

    print(f"   🔬 新版交互验证码诊断:")
    print(f"      kind   = {info['kind']}")
    print(f"      提示   = {info['hint'][:80]}")
    print(f"      vmax   = {info['vmax']}   challenge_id = {info['id'][:20]}")
    print(f"      尺寸   = {info['box_size']}   img 元素数 = {info['n_img']}")

    try:
        shot = os.path.join(SCREENSHOT_DIR, "captcha_recon.png")
        box.screenshot(path=shot)
        print(f"      💾 验证码截图: {shot}")
    except Exception as e:
        print(f"      ⚠️ 验证码截图失败: {e}")

    try:
        fp = page.evaluate("""() => ({
            webdriver: navigator.webdriver,
            fpKeys: window.__owFp ? Object.keys(window.__owFp).length : -1,
            ua: (navigator.userAgent || '').slice(0, 70),
            plugins: navigator.plugins.length,
            hasCdc: !!(window.cdc_adoQpoasnfa76pfcZLmcfl),
        })""")
        info["webdriver"] = fp.get("webdriver")
        print(f"      指纹 webdriver={fp.get('webdriver')}  __owFp_keys={fp.get('fpKeys')}"
              f"  plugins={fp.get('plugins')}  cdc注入={fp.get('hasCdc')}")
        print(f"      UA: {fp.get('ua')}")
    except Exception as e:
        print(f"      ⚠️ 指纹读取失败: {e}")

    return info


def download_captcha_gif(page) -> bytes:
    """
    从页面中获取验证码图片的原始字节数据。
    重点处理 blob: URL —— 必须在浏览器上下文内 fetch 才能拿到完整的多帧 GIF。
    """
    import base64

    captcha_selectors = [
        # 2026-09 改版后的新版交互验证码：#captcha_box_{kind} 容器，
        # 内含 #captcha_bg_{kind}（背景图）与 #captcha_chip_{kind}（可拖动碎片）
        "img[id^='captcha_bg']",
        "img[id^='captcha_chip']",
        "div[id^='captcha_box'] img",
        # 旧版数学式 GIF（保留兼容）
        "img[alt='Captcha']",
        "img[alt='captcha']",
        "img[src*='captcha']",
        ".captcha img",
    ]

    captcha_element = None
    for selector in captcha_selectors:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=5000):
                captcha_element = el
                print(f"   找到验证码元素 (选择器: {selector})")
                break
        except Exception:
            continue

    if not captcha_element:
        print("   ❌ 未找到验证码图片")
        return None

    src = captcha_element.get_attribute("src") or ""
    print(f"   📥 验证码 src: {src[:100]}")

    # ========== 方法1：blob: URL —— 在浏览器内 fetch 获取完整 GIF ==========
    if src.startswith("blob:"):
        print("   📦 检测到 blob: URL，通过浏览器内 fetch 获取完整 GIF...")
        try:
            b64_data = page.evaluate("""
                async (blobUrl) => {
                    try {
                        const resp = await fetch(blobUrl);
                        const arrayBuffer = await resp.arrayBuffer();
                        const bytes = new Uint8Array(arrayBuffer);
                        let binary = '';
                        for (let i = 0; i < bytes.length; i++) {
                            binary += String.fromCharCode(bytes[i]);
                        }
                        return btoa(binary);
                    } catch (e) {
                        return null;
                    }
                }
            """, src)
            if b64_data:
                gif_bytes = base64.b64decode(b64_data)
                print(f"   ✅ 通过 blob fetch 获取成功 ({len(gif_bytes)} bytes)")
                return gif_bytes
            else:
                print("   ⚠️ blob fetch 返回空")
        except Exception as e:
            print(f"   ⚠️ blob fetch 失败: {e}")

    # ========== 方法2：普通 http/https URL —— 用 requests 下载 ==========
    elif src.startswith("http"):
        try:
            cookies = page.context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            resp = requests.get(src, cookies=cookie_dict, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 100:
                print(f"   ✅ HTTP 下载成功 ({len(resp.content)} bytes)")
                return resp.content
            else:
                print(f"   ⚠️ HTTP 下载失败: {resp.status_code}, {len(resp.content)} bytes")
        except Exception as e:
            print(f"   ⚠️ HTTP 下载异常: {e}")

    # ========== 方法3：相对路径 URL ==========
    elif src.startswith("/"):
        full_url = f"{SITE_BASE}{src}"
        try:
            cookies = page.context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            resp = requests.get(full_url, cookies=cookie_dict, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 100:
                print(f"   ✅ 相对路径下载成功 ({len(resp.content)} bytes)")
                return resp.content
        except Exception as e:
            print(f"   ⚠️ 相对路径下载异常: {e}")

    # ========== 方法4：data: URL ==========
    elif src.startswith("data:"):
        try:
            # data:image/gif;base64,xxxxx
            b64_part = src.split(",", 1)[1]
            gif_bytes = base64.b64decode(b64_part)
            print(f"   ✅ data: URL 解码成功 ({len(gif_bytes)} bytes)")
            return gif_bytes
        except Exception as e:
            print(f"   ⚠️ data: URL 解码失败: {e}")

    # ========== 回退：元素截图（只能拍当前帧，最后手段） ==========
    print("   ⚠️ 所有下载方式失败，回退到元素截图（只能获取单帧）")
    try:
        return captcha_element.screenshot()
    except Exception as e:
        print(f"   ❌ 截图也失败了: {e}")
        return None


def try_renew_captcha(page, initial_days: int, max_attempts=5) -> bool:
    """
    尝试执行验证码续期流程，最多重试 max_attempts 次。
    以提交后剩余天数是否增加到 6 天来判断续期是否真正成功。
    返回 True 表示续期成功。
    """
    for attempt in range(1, max_attempts + 1):
        print(f"\n   {'='*40}")
        print(f"   🔄 第 {attempt}/{max_attempts} 次尝试")
        print(f"   {'='*40}")

        try:
            # ========== 第1步：点击 Renew free 按钮打开弹窗 ==========
            print("   🔍 寻找并点击 [Renew free] 按钮...")
            renew_selectors = [
                "button:has-text('Renew free')",
                "button:has-text('Renew')",
                "a:has-text('Renew free')",
                "a:has-text('Renew')",
                "[class*='renew']",
            ]
            clicked = False
            for selector in renew_selectors:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible(timeout=3000):
                        btn_text = btn.inner_text()
                        print(f"   找到按钮: '{btn_text}' (选择器: {selector})")
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                print("   ❌ 未找到可用的续期按钮")
                return False
            time.sleep(3)

            # ========== 第2步：下载并识别验证码 ==========
            print("   ⏳ 等待验证码加载...")
            time.sleep(1)

            # 侦察新版交互验证码（采集 kind/vmax/指纹，为后续实现提供数据）
            captcha_info = diagnose_captcha(page)

            # 新版交互验证码不是数学图片，ddddocr OCR 路线完全不适用，直接中止
            if captcha_info.get("kind") not in ("unknown", "", None):
                print(f"   ❌ 当前是新版交互验证码 kind={captcha_info['kind']}，"
                      f"提示: {captcha_info['hint'][:60]}")
                print(f"      旧版 ddddocr 数学式 OCR 路线不适用，需针对该类型实现交互求解。")
                return False

            ocr = init_ddddocr()
            if ocr is None:
                print("   ⚠️ ddddocr 未安装，无法执行验证码识别")
                print("   请运行: pip install ddddocr")
                return False

            gif_bytes = download_captcha_gif(page)
            if not gif_bytes:
                print("   ⚠️ 未获取到验证码图片")
                continue

            # 保存原始 GIF（调试用）
            gif_path = os.path.join(SCREENSHOT_DIR, f"captcha_raw_{attempt}.gif")
            try:
                with open(gif_path, "wb") as f:
                    f.write(gif_bytes)
                print(f"   💾 原始验证码已保存: {gif_path}")
            except Exception:
                pass

            # 分解帧识别算式并求解
            answer = recognize_captcha_by_frames(gif_bytes, ocr)
            if not answer:
                print("   ⚠️ 验证码识别求解失败，刷新重试...")
                # 刷新页面恢复干净状态
                try:
                    page.reload(wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                continue

            print(f"   📝 最终计算答案: {answer}")

            # ========== 第3步：填入并提交 ==========
            input_selectors = [
                "input[placeholder='Answer']",
                "input[placeholder='answer']",
                "input[name='captcha']",
                "input[name='answer']",
                "input[type='text']",
            ]

            input_filled = False
            for selector in input_selectors:
                try:
                    inp = page.locator(selector).first
                    if inp.is_visible(timeout=3000):
                        inp.fill("")  # 先清空
                        inp.fill(answer)
                        input_filled = True
                        print(f"   ✅ 答案已填入: {answer} (选择器: {selector})")
                        break
                except Exception:
                    continue

            if not input_filled:
                print("   ❌ 未找到验证码输入框")
                continue

            # 提交。舊版只係 click 完 sleep(4) 再 reload，完全冇讀伺服器回應，
            # 令「驗證碼答錯」同「伺服器因為冷卻/次數限制拒絕」喺 log 入面係
            # 同一副面孔。改用 expect_response 攞埋回應。
            confirm_selectors = [
                "button:has-text('Confirm Renewal')",
                "button:has-text('Confirm')",
                "button:has-text('Submit')",
                "button[type='submit']",
            ]

            submitted = False
            for selector in confirm_selectors:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible(timeout=3000):
                        try:
                            with page.expect_response(
                                    lambda r: r.request.method == "POST",
                                    timeout=15000) as info:
                                btn.click()
                            resp = info.value
                            print(f"   🌐 提交回應: HTTP {resp.status} {resp.url}")
                            try:
                                body = resp.text()
                                print(f"   🌐 回應內容: {body[:300]}")
                            except Exception as be:
                                print(f"   ⚠️ 讀回應內容失敗: {be}")
                        except Exception as we:
                            # click 已經發出，只係攞唔到回應；唔好再 click 一次
                            print(f"   ⚠️ 未攞到提交 POST 回應（{we}）")
                        submitted = True
                        print(f"   ✅ 已点击提交按钮 (选择器: {selector})")
                        break
                except Exception:
                    continue

            if not submitted:
                print("   ❌ 未找到提交按钮")
                continue

            # 順手讀頁面彈出嘅錯誤提示（驗證碼答錯時通常會彈 alert）
            try:
                err_text = page.evaluate("""() => {
                    const sels = ['.alert-danger', '.text-danger', '.error',
                                  '[role=alert]', '.ant-message-error', '.ant-message'];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el && el.innerText.trim()) {
                            return s + ': ' + el.innerText.trim().slice(0, 160);
                        }
                    }
                    return '';
                }""")
                if err_text:
                    print(f"   🚫 頁面錯誤提示: {err_text}")
            except Exception:
                pass

            # ========== 第4步：等待提交完成并刷新页面读取真实天数 ==========
            print("   ⏳ 等待提交请求处理完成...")
            time.sleep(4)

            print("   🔄 刷新页面验证最新剩余天数...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"   ⚠️ 页面刷新异常: {e}")
            wait_for_cloudflare(page)
            time.sleep(2)

            # 重新读取页面中的剩余天数
            page_text = page.locator("body").inner_text()
            match = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", page_text)

            if match:
                new_days = int(match.group(1))
                print(f"   📊 刷新后最新剩余天数: {new_days} 天")

                if new_days >= 6:
                    print(f"   ✅ 续期成功！天数已从 {initial_days} 天更新为 {new_days} 天")
                    return True
                else:
                    print(f"   ❌ 续期失败！天数仍为 {new_days} 天（未达到 6 天），验证码可能填错，准备重试...")
                    continue
            else:
                print("   ⚠️ 页面刷新后无法解析剩余天数")
                continue

        except Exception as e:
            print(f"   ❌ 第 {attempt} 次尝试发生错误: {e}")
            continue

    print(f"   ❌ {max_attempts} 次尝试均失败")
    return False


def request_manual_renewal(page, target_url: str, days_left: int, status_text: str):
    """到期預警：截圖 + 產出要發的 TG 內容，請人親身到面板過驗證碼續期。

    2026-09 起站方把續期驗證碼換成 WebSocket 互動式真人驗證
    （多階段 + 行為檢測），自動突破在設計上不可行、也不應該做。
    腳本此後的職責：準時發現到期窗口，把人推到正確的頁面。

    通知節奏（每日跑批但不每日轟炸）：
    未入窗口靜默；進入窗口當日（=閾值）提醒一次，閾值-1 當日靜默，
    剩 <=3 天逐日升級提醒。

    本函数只准备内容、不发送 —— main 里把所有行拼成一条消息再发，
    好带上内联按钮。返回 (消息行, 按钮)；静默时返回 (None, None)。
    """
    if days_left > RENEW_THRESHOLD_DAYS:
        print(f"   🔕 剩 {days_left} 天未入續期窗口（閾值 {RENEW_THRESHOLD_DAYS}），靜默")
        return None, None

    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        shot = os.path.join(SCREENSHOT_DIR, "manual_renew_needed.png")
        page.screenshot(path=shot)
        print(f"   💾 頁面截圖: {shot}")
    except Exception as e:
        print(f"   ⚠️ 截圖失敗: {e}")

    if days_left == RENEW_THRESHOLD_DAYS - 1:
        print(f"   🔕 剩 {days_left} 天屬窗口中間日，今日靜默（D{RENEW_THRESHOLD_DAYS}/D3起才通知）")
        return None, None

    slug = _slug(target_url)
    line = (f"▪️ {slug} · {status_text} · 剩 <b>{days_left} 天</b>\n"
            f"▪️ 撳「Renew free」+ 過互動驗證碼（真人步驟，腳本做唔到）")
    return line, {"text": f"🔓 去續期 {slug}", "url": target_url}


def get_vps_status(page) -> str:
    """读取 VPS 页面上的服务器状态。
    返回: 'running' / 'stopped' / 'suspended' / 'unknown'（默认 running）。"""
    try:
        status_elem = page.locator("#vpsStatusLabel, #vpsStatusPill").first
        if status_elem.count() > 0:
            data_status = (status_elem.get_attribute("data-status") or "").lower().strip()
            text_status = (status_elem.text_content() or "").lower().strip()
            if "running" in data_status or "running" in text_status:
                return "running"
            if "suspended" in data_status or "suspended" in text_status:
                return "suspended"
            if "stopped" in data_status or "stopped" in text_status:
                return "stopped"
    except Exception:
        pass
    try:
        page_text = page.locator("body").inner_text().lower()
        if "suspended" in page_text:
            return "suspended"
        if "stopped" in page_text and "running" not in page_text:
            return "stopped"
        if "running" in page_text:
            return "running"
    except Exception:
        pass
    return "running"


def check_and_handle_vps_status(page) -> str:
    """检测 VPS 状态；Stopped 则自动点 Start 重试 3 次，每次轮询最多 60 秒。
    返回用于 TG 通知的状态文本行。"""
    initial_status = get_vps_status(page)
    print(f"📊 检测到服务器初始状态: '{initial_status}'")

    if initial_status == "running":
        return "服务器状态：正常（Running）"
    if initial_status == "suspended":
        return "服务器状态：暂停使用（Suspended），请登录面板处理"

    print("⚠️ 服务器处于 Stopped 状态，准备自动尝试启动...")
    restarted_ok = False
    for start_attempt in range(1, 4):
        print(f"   🚀 [第 {start_attempt}/3 次尝试] 点击 Start 按钮启动服务器...")
        clicked = False
        for sel in ["#btnStart", "button:has-text('Start')",
                    "form[action*='/action/start'] button"]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=3000):
                    btn.click()
                    clicked = True
                    print(f"   ✅ 已点击 Start 按钮 (选择器: {sel})")
                    break
            except Exception:
                continue
        if not clicked:
            print("   ⚠️ 未能点击 Start 按钮")

        print("   ⏳ 等待服务器后台启动任务处理（最长等待 60 秒，每 8 秒刷新检测）...")
        job_start_time = time.time()
        while time.time() - job_start_time < 60:
            time.sleep(8)
            try:
                page.reload(wait_until="domcontentloaded", timeout=15000)
                wait_for_cloudflare(page)
            except Exception:
                pass
            curr_st = get_vps_status(page)
            print(f"   📊 刷新后服务器状态: '{curr_st}'")
            if curr_st == "running":
                restarted_ok = True
                print(f"   🎉 服务器在第 {start_attempt} 次尝试中成功重启为 Running 状态！")
                break
        if restarted_ok:
            break

    if restarted_ok:
        return "服务器状态：重启成功，正常（Running）"
    print("   ❌ 重试 3 次后服务器依然处于 Stopped 状态")
    return "服务器状态：重启失败，停止（Stopped），请登录面板检查"


def get_vps_urls(page) -> list:
    """
    自动从当前页面或控制面板/仪表盘中寻找用户绑定的 VPS 详情页 URL。

    一条都找不到时**不返回空列表**，而是抛 UpstreamDown：见该异常的说明，
    「上游 5xx」与「账号下确实没有实例」是两种完全不同的结论。
    """
    vps_urls = []
    upstream = ""

    def extract_vps_links():
        found = []
        try:
            links = page.locator("a[href*='/vps/']").all()
            for link in links:
                href = link.get_attribute("href") or ""
                if href:
                    full_url = urllib.parse.urljoin(SITE_BASE, href)
                    path = urllib.parse.urlparse(full_url).path.rstrip('/')
                    if path != "/vps" and full_url not in found:
                        found.append(full_url)
        except Exception as e:
            print(f"   ⚠️ 提取 VPS 链接异常: {e}")
        return found

    print("\n🔍 正在自动识别账号下的 VPS 实例...")
    # 1. 先从当前登录落地页提取
    vps_urls = extract_vps_links()

    # 2. 如果没有，前往首页 SITE_BASE
    if not vps_urls:
        try:
            print(f"   前往首页 {SITE_BASE} 提取实例列表...")
            resp = page.goto(SITE_BASE, wait_until="domcontentloaded", timeout=30000)
            wait_for_cloudflare(page)
            time.sleep(3)
            upstream = upstream or upstream_failure(page, resp.status if resp else 0)
            vps_urls = extract_vps_links()
        except Exception as e:
            print(f"   ⚠️ 前往首页提取失败: {e}")

    # 3. 如果还是没有，尝试访问 /dashboard 或 /vps
    if not vps_urls:
        for sub_path in ["/dashboard", "/vps"]:
            try:
                url = f"{SITE_BASE}{sub_path}"
                print(f"   尝试访问 {url} 提取实例列表...")
                resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                wait_for_cloudflare(page)
                time.sleep(3)
                upstream = upstream or upstream_failure(page, resp.status if resp else 0)
                vps_urls = extract_vps_links()
                if vps_urls:
                    break
            except Exception as e:
                print(f"   ⚠️ 访问 {sub_path} 异常: {e}")

    if not vps_urls:
        if upstream:
            raise UpstreamDown(upstream)
        print("   ❌ 未能在控制面板自动检测到任何 VPS 实例页面")
        return []

    print(f"   ✅ 成功检测到 {len(vps_urls)} 个 VPS 实例:")
    for u in vps_urls:
        print(f"      - {u}")
    return vps_urls


def run_all() -> tuple:
    """跑一轮守门。返回 (报告, 要发到 TG 的行, 内联按钮)。"""
    global OPENWORLD_COOKIES

    report = RenewReport(service=SERVICE)
    alerts: list = []
    buttons: list = []
    manual_required = []

    print("#" * 50)
    print("   Openworld VPS 到期預警守門")
    print("#" * 50)

    # secret 未配置时回退读仓库 .env；PAT 无 secrets:write 权限时这是唯一可用路径
    if not OPENWORLD_COOKIES:
        OPENWORLD_COOKIES = load_env_fallback("OPENWORLD_COOKIES")

    if not OPENWORLD_COOKIES and not DISCORD_TOKEN:
        print("❌ 未配置认证方式：请设置 OPENWORLD_COOKIES（首选）或 DISCORD_TOKEN。")
        _note(report, PANEL_TARGET, Outcome.FAILED,
              detail="未配置认证：OPENWORLD_COOKIES 与 DISCORD_TOKEN 都是空的")
        alerts.append("▪️ 未配置認證：OPENWORLD_COOKIES / DISCORD_TOKEN 都係空")
        return report, alerts, buttons

    if OPENWORLD_COOKIES:
        print(f"🔑 使用 Cookie 注入认证（{len(OPENWORLD_COOKIES)} 字符）")
        # Clerk 的 __session JWT 有效期很短，尽早刷一次，
        # 让后面几分钟才启动的 Playwright 拿到新鲜会话
        OPENWORLD_COOKIES = refresh_cookie_server_side(OPENWORLD_COOKIES)

    headless_mode = env.get("HEADLESS", "true").lower() == "true"
    print(f"🖥️  运行模式: {'无头' if headless_mode else '有头'}")
    print("🎯 登录后将自动从面板检测 VPS 实例")

    with sync_playwright() as p:
        # 使用更真实的浏览器配置以避免被检测
        browser = p.chromium.launch(
            headless=headless_mode,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()

        try:
            # ========== 登录 ==========
            # 候选链：cache（跨 run 持久化，首选）→ secret OPENWORLD_COOKIES（兜底）
            if OPENWORLD_COOKIES or os.path.exists(COOKIE_CACHE_FILE):
                auth_state, auth_reason, active_cookie = authenticate_candidates(context, page)
            else:
                auth_state, auth_reason, active_cookie = AUTH_COOKIE_DEAD, "", ""
                print("\n🔑 使用 DISCORD_TOKEN 登录（该流程已于 2026-09 失效）")
                if login_with_discord_token(page, DISCORD_TOKEN):
                    auth_state, auth_reason = AUTH_OK, ""
                else:
                    auth_state, auth_reason = AUTH_COOKIE_DEAD, "Discord OAuth 已于 2026-09 失效"

            if auth_state == AUTH_UPSTREAM_DOWN:
                # 面板 5xx / CF 隧道故障：这不是业务结论，别标红也别乱发告警
                print(f"\n🌐 面板上游故障，本轮拿不到真实状态（{auth_reason}）")
                _note(report, PANEL_TARGET, Outcome.TRANSIENT, detail=auth_reason)
                alerts.append("▪️ 面板上游故障（{}）· 本輪跳過，等下次排程".format(
                    _esc(shorten(auth_reason, 60))))
                return report, alerts, buttons

            if auth_state != AUTH_OK:
                print("\n❌ 登录流程失败（Cookie 大概率已过期）。")
                _note(report, PANEL_TARGET, Outcome.FAILED,
                      detail="Cookie 可能已過期——重新登入抄 Cookie 交助手更新")
                alerts.append("▪️ 登入失敗：Cookie 可能已過期——重新登入抄 Cookie 交助手更新")
                return report, alerts, buttons

            # ========== 自动检测 VPS 列表 ==========
            target_vps_list = get_vps_urls(page)

            if not target_vps_list:
                print("\n❌ 未能从面板自动检测到任何 VPS 实例。")
                print("💡 请检查账号是否有活跃的 VPS 实例")
                save_screenshot(page, "no_vps_found")
                _note(report, PANEL_TARGET, Outcome.FAILED,
                      detail="面板搵唔到任何 VPS 實例（上游正常，账号下确实無实例）")
                alerts.append("▪️ 面板搵唔到任何 VPS 實例")
                return report, alerts, buttons

            # 遍历每个 VPS 实例进行检测
            for idx, target_url in enumerate(target_vps_list, 1):
                target = _target_name(target_url)
                slug = _slug(target_url)
                print(f"\n{'=' * 50}")
                print(f"📌 [{idx}/{len(target_vps_list)}] 导航到目标 VPS 页面: {target_url}")
                print(f"{'=' * 50}")

                resp = None
                try:
                    resp = page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"⚠️ 页面加载异常: {e}")

                wait_for_cloudflare(page)
                time.sleep(3)

                current_url = page.url
                page_title = page.title()
                print(f"📝 当前 URL: {current_url}")
                print(f"📝 页面标题: {page_title}")

                # 上游 5xx / CF 隧道故障：这一台本轮读不到，跳过而不是报失败
                reason = upstream_failure(page, resp.status if resp else 0)
                if reason:
                    print(f"🌐 上游故障页（{reason}），跳过 {target_url}")
                    _note(report, target, Outcome.TRANSIENT, detail=reason)
                    alerts.append("▪️ {} · 面板上游故障（{}）".format(slug, _esc(shorten(reason, 60))))
                    continue

                # 验证是否真正到达了 VPS 页面（而非被重定向到登录页）
                if "/login" in current_url:
                    print("❌ 被重定向到登录页，Cookie 可能无效")
                    save_screenshot(page, f"redirect_to_login_{idx}")
                    _note(report, target, Outcome.FAILED,
                          detail="登入後仍被彈返登入頁（Cookie 失效）")
                    alerts.append(f"▪️ {slug} · 登入後仍被彈返登入頁（Cookie 失效）")
                    break

                page_text = page.locator("body").inner_text()

                # 检查是否 404 Page Not Found
                if "404" in page_title or "Page Not Found" in page_title or "doesn't exist" in page_text.lower():
                    print(f"❌ 目标 VPS 页面不存在或无权访问 (404 Not Found): {target_url}")
                    print("⚠️ 原因分析: 此 URL 对应的机器可能已被注销或不存在。")
                    save_screenshot(page, f"vps_404_{idx}")
                    # 旧代码这里是 continue（静默放过），于是「监控着一台不存在的
                    # 机器」永远不会有人知道。改成报出来。
                    _note(report, target, Outcome.FAILED,
                          detail="頁面 404 Not Found，機器可能已被註銷")
                    alerts.append(f"▪️ {slug} · 頁面 404 Not Found，機器可能已被註銷")
                    continue

                if "/vps/" not in current_url:
                    print(f"⚠️ 当前页面可能不是 VPS 详情页: {current_url}")
                    save_screenshot(page, f"not_vps_page_{idx}")

                print("✅ 已成功到达目标 VPS 页面")
                save_screenshot(page, f"vps_page_loaded_{idx}")

                # ========== 检查服务器状态与自动重启（上游 09-03 加入） ==========
                status_text_tg = check_and_handle_vps_status(page)
                print(f"📌 {status_text_tg}")
                # 重启后页面内容可能变化，重新读取以获取最新天数
                page_text = page.locator("body").inner_text()

                # ========== 检查剩余天数 ==========
                match = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", page_text)

                if match:
                    days_left = int(match.group(1))
                    print(f"🔍 当前 VPS 剩余续期时间: {days_left} 天")

                    if days_left > RENEW_THRESHOLD_DAYS:
                        # 未到期保持静默：只在需要人介入时才通知
                        print(f"⏳ 剩余 {days_left} 天 > {RENEW_THRESHOLD_DAYS} 天阈值，跳过续期（静默）")
                        _note(report, target, Outcome.SKIPPED, expire=days_left)
                        continue
                    print(f"⚠️ 剩余 {days_left} 天 ≤ {RENEW_THRESHOLD_DAYS} 天，开始执行续期...")
                    expire = days_left
                else:
                    print("⚠️ 未能从页面提取剩余天数，将强制尝试续期")
                    print(f"   页面文本片段: {page_text[:500]}")
                    days_left = 0   # 未知天数，强制尝试续期
                    expire = None   # 报告里不写「剩 0 天」，那会被读成「已过期」

                # ========== 到期預警：人工續期 ==========
                # 2026-09 站方將續期驗證碼換成 WebSocket 互動式真人驗證
                # （puzzle/rotate/key/odd/match 多階段 + 行為檢測），
                # 自動突破屬繞過反自動化，不做；此處改為通知人工處理。
                print(f"\n{'=' * 50}")
                print("🖐 已进入续期窗口：需要人工完成真人验证")
                print(f"{'=' * 50}")

                line, button = request_manual_renewal(page, target_url, days_left, status_text_tg)
                manual_required.append(target_url)
                # 进窗口就 FAILED（标红），但窗口中间日故意不发通知 ——
                # 「标红」与「有没有 alerts 行」是两件事，别混。
                _note(report, target, Outcome.FAILED, expire=expire,
                      detail=f"{status_text_tg} · 需人工過互動驗證碼續期")
                if line:
                    alerts.append(line)
                    buttons.append(button)

        except UpstreamDown as exc:
            print(f"\n🌐 面板上游故障，本轮跳过（{exc}）")
            _note(report, PANEL_TARGET, Outcome.TRANSIENT, detail=str(exc))
            alerts.append("▪️ 面板上游故障（{}）· 本輪跳過，等下次排程".format(
                _esc(shorten(str(exc), 60))))
        except Exception as e:
            print(f"\n💥 脚本发生未捕获异常: {e}")
            traceback.print_exc()
            save_screenshot(page, "uncaught_error")
            _note(report, PANEL_TARGET, Outcome.FAILED,
                  detail=f"脚本异常: {type(e).__name__}: {e}")
            alerts.append("▪️ 腳本異常：{}".format(
                _esc(shorten(f"{type(e).__name__}: {e}", 120))))
        finally:
            print("\n🏁 脚本执行完毕")
            try:
                browser.close()
            except Exception:
                pass

    if manual_required:
        print(f"\n🖐 {len(manual_required)} 個 VPS 已進入人工續期窗口: {manual_required}")
        print("WATCHDOG_MANUAL_REQUIRED")
    else:
        print("\n✅ 本輪檢查完成：所有 VPS 均在有效期内")
        print("WATCHDOG_OK")

    return report, alerts, buttons


def main() -> int:
    try:
        report, alerts, buttons = run_all()
    except Exception as exc:          # run_all 内部已兜底；这里防的是它自己出意外
        traceback.print_exc()
        detail = f"{type(exc).__name__}: {shorten(str(exc), 200)}"
        report = RenewReport(service=SERVICE)
        _note(report, PANEL_TARGET, Outcome.FAILED, detail=detail)
        alerts = ["▪️ 腳本異常：{}".format(_esc(shorten(detail, 120)))]
        buttons = []

    # 报告只负责打印 + 算退出码；TG 由下面自己发 —— 消息要带内联按钮，
    # 而 renewkit 的 finish() 目前还传不了 buttons。
    code = report.finish(notify_tg=False)

    if alerts:
        # 剩 <=3 天才用 🚨；其余（含上游故障、cookie 失效）用 ⚠️
        urgent = any(isinstance(r.expire, int) and r.expire <= 3 for r in report.results)
        head = "🚨" if urgent else "⚠️"
        title = "Openworld VPS 要人手續期" if urgent else "Openworld VPS 守門提醒"
        text = f"{head} <b>{title}</b> ｜ {timeutil.now_local()}\n" + "\n".join(alerts)
        if env.dry_run():
            # 演练时不发 TG，但把原文与按钮原样打出来 —— 否则「演练通过」
            # 只能证明流程没崩，证明不了消息对不对。
            print("\nℹ️ DRY_RUN 演练，跳过 Telegram 通知。本轮本应发送：")
            print(text)
            print(f"   内联按钮: {buttons or '（无）'}")
        else:
            notify.send(text, parse_mode="HTML", buttons=buttons or None)

    # 标记「通知这件事已经由本脚本负责过了」。
    # 为什么需要：窗口中间日（阈值-1）是**故意静默**的（见 request_manual_renewal），
    # 那天 job 会标红但一条消息都不发。如果 workflow 再用「failure() 就发兜底通知」
    # 那条路，静默日就会被兜底通知破功。反过来，如果干脆关掉兜底，脚本连
    # import 都没跑起来时（pip/playwright 装挂）就彻底没人知道了。
    # 所以：脚本跑到底就留个标记，workflow 的兜底步骤只在没标记时才发。
    try:
        with open(".renew-handled", "w", encoding="utf-8") as fh:
            fh.write(f"exit={code} alerts={len(alerts)}\n")
    except Exception:
        pass

    return code


if __name__ == "__main__":
    sys.exit(main())
