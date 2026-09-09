#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
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
OPENWORLD_COOKIES = os.environ.get("OPENWORLD_COOKIES", "")

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
    """发送 Telegram 通知"""
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
DIGIT_MAP = {
    '0': '0', 'O': '0', 'o': '0', 'D': '0', 'C': '0',
    '1': '1', 'l': '1', 'I': '1', '|': '1', '!': '1', 'i': '1',
    '2': '2', 'Z': '2', 'z': '2',
    '3': '3',
    '4': '4',
    '5': '5', 'S': '5', 's': '5',
    '6': '6', 'b': '6', 'G': '6', '&': '6',
    '7': '7', 'T': '7', 't': '7',
    '8': '8', 'B': '8',
    '9': '9', 'q': '9', 'Q': '9', 'g': '9', 'y': '9',
}


def frame_foreground_ratio(img: Image.Image) -> float:
    """前景（灰度 < 170）像素佔比"""
    arr = np.array(img)
    return float((arr < 170).sum() / arr.size) if arr.size else 0.0


def normalize_expression(s: str) -> str:
    """把 OCR 原文正規化成只含數字同 + - * / 嘅算式字串，其餘一律丟棄"""
    s = (s.replace('−', '-').replace('–', '-').replace('—', '-')
         .replace('×', '*').replace('÷', '/').replace(':', '/'))
    out = []
    for ch in s:
        if ch in '+-*/':
            out.append(ch)
        elif ch == '十':
            out.append('+')
        elif ch in ('x', 'X'):
            out.append('*')
        elif ch in DIGIT_MAP:
            out.append(DIGIT_MAP[ch])
    return ''.join(out)


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

    for cand, cnt in votes.most_common():
        val = solve_expression(cand)
        if val is not None:
            print(f"   ✅ 多數投票 -> {cand!r} (票數 {cnt}) = {val}")
            return str(val)

    print("   ⚠️ 所有候選都無法解析成 A op B")
    return ""


def download_captcha_gif(page) -> bytes:
    """
    从页面中获取验证码 GIF 图片的原始字节数据。
    重点处理 blob: URL —— 必须在浏览器上下文内 fetch 才能拿到完整的多帧 GIF。
    """
    import base64

    captcha_selectors = [
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
    try:
        import ddddocr
    except ImportError:
        print("   ⚠️ ddddocr 未安装，无法执行验证码识别")
        print("   请运行: pip install ddddocr")
        return False

    ocr = ddddocr.DdddOcr(show_ad=False)

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
            print("   ⏳ 等待验证码图片加载...")
            time.sleep(1)

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
    print("#" * 50)
    print("   Openworld VPS 自动续期脚本")
    print("#" * 50)

    if not OPENWORLD_COOKIES and not DISCORD_TOKEN:
        print("❌ 未配置认证方式：请设置 OPENWORLD_COOKIES（首选）或 DISCORD_TOKEN。")
        sys.exit(1)

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

        failed_targets = []

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
                print("\n❌ 登录流程失败，脚本退出。")
                send_telegram_message("❌ Openworld VPS 续期失败：登录流程失败")
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

                # ========== 检查剩余天数 ==========
                match = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", page_text)

                if match:
                    days_left = int(match.group(1))
                    print(f"🔍 当前 VPS 剩余续期时间: {days_left} 天")

                    if days_left > RENEW_THRESHOLD_DAYS:
                        msg = f"⏳ 剩余 {days_left} 天 > {RENEW_THRESHOLD_DAYS} 天阈值，跳过续期"
                        print(msg)
                        send_telegram_message(f"ℹ️ Openworld VPS 无需续期\n实例: {target_url}\n剩余时间: {days_left} 天")
                        continue
                    else:
                        print(f"⚠️ 剩余 {days_left} 天 ≤ {RENEW_THRESHOLD_DAYS} 天，开始执行续期...")
                else:
                    print("⚠️ 未能从页面提取剩余天数，将强制尝试续期")
                    print(f"   页面文本片段: {page_text[:500]}")
                    days_left = 0  # 未知天数，强制尝试续期

                # ========== 执行续期 ==========
                print(f"\n{'=' * 50}")
                print("🔄 开始执行验证码续期")
                print(f"{'=' * 50}")

                renew_success = try_renew_captcha(page, initial_days=days_left)
                if not renew_success:
                    failed_targets.append(target_url)

                if renew_success:
                    # 计算续期后的到期时间（当前时间 + 6天）
                    expiry_time = datetime.now(timezone(timedelta(hours=8))) + timedelta(days=6)
                    expiry_str = expiry_time.strftime("%Y-%m-%d %H:%M:%S") + " (GMT+8)"
                    msg = f"✅ Openworld VPS 续期成功！\n实例: {target_url}\n天数已更新为 6 天\n续期至: {expiry_str}"
                    print(f"✅ 续期成功！天数已更新为 6 天")
                    print(f"📅 续期至: {expiry_str}")
                    send_telegram_message(msg)
                else:
                    print("❌ 续期失败（5次尝试均未成功）")
                    send_telegram_message(f"❌ Openworld VPS 续期失败：5次验证码尝试均未成功\n实例: {target_url}")

        except Exception as e:
            print(f"\n💥 脚本发生未捕获异常: {e}")
            import traceback
            traceback.print_exc()
            save_screenshot(page, "uncaught_error")
            send_telegram_message(f"❌ Openworld VPS 续期脚本异常: {str(e)[:200]}")
            sys.exit(1)

        finally:
            print("\n🏁 脚本执行完毕")

        if failed_targets:
            print(f"\n❌ {len(failed_targets)} 個 VPS 續期失敗: {failed_targets}")
            browser.close()
            sys.exit(1)
        print("\n✅ 全部 VPS 續期成功")
        browser.close()


if __name__ == "__main__":
    main()
