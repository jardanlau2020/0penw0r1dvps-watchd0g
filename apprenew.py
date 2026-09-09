#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os

# 静默 ONNX Runtime 底层 C++ 的设备扫描 Warning（device_discovery 噪音）
os.environ.setdefault("ORT_LOGGING_LEVEL", "3")

import re
import sys
import json
import io
import urllib.parse
import requests
import time
from datetime import datetime, timedelta, timezone
from PIL import Image
import numpy as np
from playwright.sync_api import sync_playwright

# ================= 配置区 =================
# 从 GitHub Secrets 环境变量获取 Discord Token
# （已于 2026-09 失效：官网认证由 Discord OAuth 迁到 Clerk + Google OAuth，
#   /discord-login 现返回 404，/login 用 @clerk/clerk-js@6）
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")

# 首选认证方式：人工登入一次后贴入的完整 Cookie 请求头字符串。
# 必须包含 httpOnly 的 Clerk 会话（__session / __client），
# 所以要用 DevTools → Network → 点任一 openworld.eu.org 请求 →
# 复制 Request Headers 里「Cookie: ...」那一整行的值。
# 留空则退回已失效的 Discord OAuth 流程（保留仅为兼容旧配置）。
OPENWORLD_COOKIES = os.environ.get("OPENWORLD_COOKIES", "")  # 运行时回退读取 .env

# TG 通知（可选）
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")

# 网站根域

SITE_BASE = "https://openworld.eu.org"

# 续期天数阈值：剩余天数 <= 此值时才执行续期
RENEW_THRESHOLD_DAYS = 5
# ==========================================

# 截图保存目录（调试用）
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", ".")


def send_telegram_message(message: str):
    """发送 Telegram 通知（token 支持本地 .env 回退）"""
    global TG_BOT_TOKEN, TG_CHAT_ID
    if not TG_BOT_TOKEN:
        TG_BOT_TOKEN = load_env_fallback("TG_BOT_TOKEN")
    if not TG_CHAT_ID:
        TG_CHAT_ID = load_env_fallback("TG_CHAT_ID")
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")


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
    """
    val = os.environ.get(name, "").strip()
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


def verify_logged_in(page) -> bool:
    """确认注入的 cookie 真有有效会话（而不是被弹回登录页）。"""
    ok = False
    for path in ("/dashboard", "/vps"):
        try:
            page.goto(f"{SITE_BASE}{path}", wait_until="domcontentloaded", timeout=30000)
            wait_for_cloudflare(page)
            time.sleep(2)
            cur = page.url
            title = page.title() or ""
            print(f"   检查 {path}: URL={cur} | title={title}")
            if "/login" in cur or "/signin" in cur:
                print("❌ 被重定向到登录页：cookie 已失效")
                save_screenshot(page, "cookie_expired")
                return False
            if "404" in title or "Page Not Found" in title:
                print(f"   ⚠️ {path} 返回 404，试下一个路径")
                continue
            print(f"   ✅ 会话有效（{path}）")
            ok = True
            break
        except Exception as e:
            print(f"   ⚠️ 检查 {path} 异常: {e}")
    if not ok:
        print("❌ 没有任何面板路径可进入：cookie 可能失效")
        save_screenshot(page, "cookie_expired")
    return ok


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
    """到期預警：截圖 + TG 通知，請人親身到面板過驗證碼續期。

    2026-09 起站方把續期驗證碼換成 WebSocket 互動式真人驗證
    （多階段 + 行為檢測），自動突破在設計上不可行、也不應該做。
    腳本此後的職責：準時發現到期窗口，把人推到正確的頁面。
    """
    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        shot = os.path.join(SCREENSHOT_DIR, "manual_renew_needed.png")
        page.screenshot(path=shot)
        print(f"   💾 頁面截圖: {shot}")
    except Exception as e:
        print(f"   ⚠️ 截圖失敗: {e}")

    urgency = "🚨 3 天內到期！" if days_left <= 3 else "⚠️"
    send_telegram_message(
        f"{urgency} Openworld VPS 需要人工續期\n"
        f"實例: {target_url}\n"
        f"{status_text}\n"
        f"剩餘: {days_left} 天\n\n"
        f"請登入 openworld.eu.org → 該 VPS → 撳「Renew free」→ 完成互動驗證碼。\n"
        f"（真人驗證碼腳本代過唔到，呢步設計上就要人做）"
    )


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
    """
    vps_urls = []

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
            page.goto(SITE_BASE, wait_until="domcontentloaded", timeout=30000)
            wait_for_cloudflare(page)
            time.sleep(3)
            vps_urls = extract_vps_links()
        except Exception as e:
            print(f"   ⚠️ 前往首页提取失败: {e}")

    # 3. 如果还是没有，尝试访问 /dashboard 或 /vps
    if not vps_urls:
        for sub_path in ["/dashboard", "/vps"]:
            try:
                url = f"{SITE_BASE}{sub_path}"
                print(f"   尝试访问 {url} 提取实例列表...")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                wait_for_cloudflare(page)
                time.sleep(3)
                vps_urls = extract_vps_links()
                if vps_urls:
                    break
            except Exception:
                pass

    if vps_urls:
        print(f"   ✅ 成功检测到 {len(vps_urls)} 个 VPS 实例:")
        for u in vps_urls:
            print(f"      - {u}")
    else:
        print("   ❌ 未能在控制面板自动检测到任何 VPS 实例页面")

    return vps_urls


def main():
    global OPENWORLD_COOKIES

    print("#" * 50)
    print("   Openworld VPS 自动续期脚本")
    print("#" * 50)

    # secret 未配置时回退读仓库 .env；PAT 无 secrets:write 权限时这是唯一可用路径
    if not OPENWORLD_COOKIES:
        OPENWORLD_COOKIES = load_env_fallback("OPENWORLD_COOKIES")

    if not OPENWORLD_COOKIES and not DISCORD_TOKEN:
        print("❌ 未配置认证方式：请设置 OPENWORLD_COOKIES（首选）或 DISCORD_TOKEN。")
        sys.exit(1)

    if OPENWORLD_COOKIES:
        print(f"🔑 使用 Cookie 注入认证（{len(OPENWORLD_COOKIES)} 字符）")
        # Clerk 的 __session JWT 只有 60 秒有效期，尽早刷一次，
        # 让后面几分钟才启动的 Playwright 拿到新鲜会话
        OPENWORLD_COOKIES = refresh_cookie_server_side(OPENWORLD_COOKIES)

    headless_mode = os.environ.get("HEADLESS", "true").lower() == "true"
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

        manual_required = []

        try:
            # ========== 登录 ==========
            if OPENWORLD_COOKIES:
                print("\n🔑 使用 OPENWORLD_COOKIES 注入会话")
                success = login_with_cookies(context, OPENWORLD_COOKIES)
                if success:
                    success = verify_logged_in(page)
            else:
                print("\n🔑 使用 DISCORD_TOKEN 登录（该流程已于 2026-09 失效）")
                success = login_with_discord_token(page, DISCORD_TOKEN)

            if not success:
                print("\n❌ 登录流程失败（Cookie 大概率已过期）。")
                send_telegram_message(
                    "🚨 Openworld 登入失敗：Cookie 可能已過期\n"
                    "請重新登入 openworld.eu.org，用 DevTools 複製 Cookie 後交畀助手更新 .env"
                )
                browser.close()
                sys.exit(1)

            # ========== 自动检测 VPS 列表 ==========
            target_vps_list = get_vps_urls(page)

            if not target_vps_list:
                print("\n❌ 未能从面板自动检测到任何 VPS 实例。")
                print("💡 请检查账号是否有活跃的 VPS 实例")
                save_screenshot(page, "no_vps_found")
                send_telegram_message("❌ Openworld VPS 续期失败：未在面板找到任何 VPS 实例")
                browser.close()
                sys.exit(1)

            # 遍历每个 VPS 实例进行续期检测
            for idx, target_url in enumerate(target_vps_list, 1):
                print(f"\n{'=' * 50}")
                print(f"📌 [{idx}/{len(target_vps_list)}] 导航到目标 VPS 页面: {target_url}")
                print(f"{'=' * 50}")

                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"⚠️ 页面加载异常: {e}")

                wait_for_cloudflare(page)
                time.sleep(3)

                current_url = page.url
                page_title = page.title()
                print(f"📝 当前 URL: {current_url}")
                print(f"📝 页面标题: {page_title}")

                # 验证是否真正到达了 VPS 页面（而非被重定向到登录页）
                if "/login" in current_url:
                    print("❌ 被重定向到登录页，Cookie 可能无效")
                    save_screenshot(page, f"redirect_to_login_{idx}")
                    send_telegram_message("❌ Openworld VPS 续期失败：登录后仍被重定向到登录页")
                    break

                page_text = page.locator("body").inner_text()

                # 检查是否 404 Page Not Found
                if "404" in page_title or "Page Not Found" in page_title or "doesn't exist" in page_text.lower():
                    print(f"❌ 目标 VPS 页面不存在或无权访问 (404 Not Found): {target_url}")
                    print("⚠️ 原因分析: 此 URL 对应的机器可能已被注销或不存在。")
                    save_screenshot(page, f"vps_404_{idx}")
                    send_telegram_message(f"❌ Openworld VPS 续期失败：页面 404 Not Found\nURL: {target_url}")
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
                        # 未到期保持静默：只在需要人介入时才通知（TG 只留給人工續期/Cookie 過期）
                        msg = f"⏳ 剩余 {days_left} 天 > {RENEW_THRESHOLD_DAYS} 天阈值，跳过续期（静默）"
                        print(msg)
                        continue
                    else:
                        print(f"⚠️ 剩余 {days_left} 天 ≤ {RENEW_THRESHOLD_DAYS} 天，开始执行续期...")
                else:
                    print("⚠️ 未能从页面提取剩余天数，将强制尝试续期")
                    print(f"   页面文本片段: {page_text[:500]}")
                    days_left = 0  # 未知天数，强制尝试续期

                # ========== 到期預警：人工續期 ==========
                # 2026-09 站方將續期驗證碼換成 WebSocket 互動式真人驗證
                # （puzzle/rotate/key/odd/match 多階段 + 行為檢測），
                # 自動突破屬繞過反自動化，不做；此處改為通知人工處理。
                print(f"\n{'=' * 50}")
                print("🖐 已进入续期窗口：需要人工完成真人验证")
                print(f"{'=' * 50}")

                request_manual_renewal(page, target_url, days_left, status_text_tg)
                manual_required.append(target_url)

        except Exception as e:
            print(f"\n💥 脚本发生未捕获异常: {e}")
            import traceback
            traceback.print_exc()
            save_screenshot(page, "uncaught_error")
            send_telegram_message(f"❌ Openworld VPS 续期脚本异常: {str(e)[:200]}")
            sys.exit(1)

        finally:
            print("\n🏁 脚本执行完毕")

        if manual_required:
            print(f"\n🖐 {len(manual_required)} 個 VPS 已發出人工續期提醒: {manual_required}")
            print("WATCHDOG_MANUAL_REQUIRED")
            browser.close()
            sys.exit(2)
        print("\n✅ 本輪檢查完成：所有 VPS 均在有效期内")
        print("WATCHDOG_OK")
        browser.close()


if __name__ == "__main__":
    main()
