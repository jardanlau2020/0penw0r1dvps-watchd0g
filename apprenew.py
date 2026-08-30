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
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")

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


def recognize_captcha_by_frames(gif_bytes: bytes, ocr) -> str:
    """
    分解帧识别验证码：
    1. 获取每一帧。
    2. 对每一帧分为 Left（左半边，数字A）、Middle（中间，运算符）、Right（右半边，数字B）。
    3. 过滤并只保留数字/运算符字符，跨帧统计出现频率最高的字符。
    4. 组合成算式并计算结果。
    """
    frames = extract_gif_frames(gif_bytes)
    if not frames:
        return ""

    left_candidates = []   # 数字A候选
    op_candidates = []     # 运算符候选
    right_candidates = []  # 数字B候选

    for idx, frame in enumerate(frames):
        w, h = frame.size
        # 裁剪三个区域
        left_crop = frame.crop((0, 0, int(w * 0.42), h))
        mid_crop = frame.crop((int(w * 0.35), 0, int(w * 0.65), h))
        right_crop = frame.crop((int(w * 0.58), 0, w, h))

        for region_name, crop_img, cand_list in [
            ("Left", left_crop, left_candidates),
            ("Middle", mid_crop, op_candidates),
            ("Right", right_crop, right_candidates)
        ]:
            proc_img = preprocess_frame(crop_img)
            img_buf = io.BytesIO()
            proc_img.save(img_buf, format="PNG")
            
            # 使用 ddddocr 识别
            res = ocr.classification(img_buf.getvalue()).strip()
            
            # 清理非数字/运算符字符
            if region_name in ("Left", "Right"):
                # 只保留数字，统一模糊识别字符
                res_clean = re.sub(r'[^0-9]', '', res.replace('O', '0').replace('o', '0').replace('l', '1').replace('I', '1').replace('S', '5').replace('s', '5').replace('B', '8').replace('g', '9').replace('q', '9').replace('z', '2').replace('Z', '2'))
            else:
                # 运算符 region：匹配 + - * /
                res_clean = ""
                for char in res:
                    if char in "+-*/":
                        res_clean += char
                    elif char in ("x", "X", "×"):
                        res_clean += "*"
                    elif char in ("÷", ":"):
                        res_clean += "/"
                    elif char in ("一", "—", "–"):
                        res_clean += "-"
                    elif char in ("十", "t", "T"):
                        res_clean += "+"

            if res_clean:
                cand_list.append(res_clean)

    # 统计出现最高频的左数字、运算符、右数字
    from collections import Counter
    
    num_a = Counter(left_candidates).most_common(1)[0][0] if left_candidates else ""
    op = Counter(op_candidates).most_common(1)[0][0] if op_candidates else ""
    num_b = Counter(right_candidates).most_common(1)[0][0] if right_candidates else ""

    print(f"   🔍 跨帧区域统计结果 -> 左数字(A): '{num_a}' | 运算符: '{op}' | 右数字(B): '{num_b}'")

    # 拼接算式并求解
    if num_a and num_b:
        # 如果运算符没识别出来，默认加法或减法尝试
        if not op:
            op = "+"
        expr = f"{num_a}{op}{num_b}"
        try:
            val = int(eval(expr))
            print(f"   🧮 算式求解成功: {expr} = {val}")
            return str(val)
        except Exception as e:
            print(f"   ⚠️ 计算异常 ({expr}): {e}")

    # 如果区域切分没拿到结果，尝试全图逐帧识别
    all_text = []
    for frame in frames:
        proc_img = preprocess_frame(frame)
        img_buf = io.BytesIO()
        proc_img.save(img_buf, format="PNG")
        res = ocr.classification(img_buf.getvalue()).strip()
        # 清理常见错别字
        cleaned = re.sub(r'[^0-9+\-*/]', '', res.replace('x', '*').replace('X', '*').replace('O', '0').replace('o', '0').replace('l', '1'))
        if cleaned:
            all_text.append(cleaned)
            
    if all_text:
        most_common_full = Counter(all_text).most_common(1)[0][0]
        match = re.search(r'(\d+)\s*([+\-*/])\s*(\d+)', most_common_full)
        if match:
            a, o, b = match.groups()
            val = int(eval(f"{a}{o}{b}"))
            print(f"   🧮 全图统计求解: {a}{o}{b} = {val}")
            return str(val)

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
    基于 WebSocket 实时状态 (Verified / Incorrect — try again) 判断验证码答案正确性。
    只要有一次验证码获得 Verified 并且成功提交表单即视为续期成功。
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
            if attempt == 1:
                # ========== 首次：点击 Renew free 按钮打开弹窗 ==========
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
            else:
                # ========== 重试：点击 New Captcha / 刷新按钮获取新验证码 ==========
                print("   🔄 点击 [New Captcha] 刷新验证码...")
                refreshed = False
                refresh_selectors = [
                    "button[title='New Captcha']",
                    "[id^='captcha_refresh_']",
                    "button:has-text('New Captcha')",
                    "button:has-text('↺')",
                    "button:near(img[alt='Captcha'])",
                ]
                for sel in refresh_selectors:
                    try:
                        rbtn = page.locator(sel).first
                        if rbtn.is_visible(timeout=2000):
                            rbtn.click()
                            refreshed = True
                            print("   ✅ 已点击 [New Captcha] 刷新按钮")
                            time.sleep(2)
                            break
                    except Exception:
                        continue

                if not refreshed:
                    print("   ⚠️ 未能点击刷新按钮，尝试重新点击 Renew free...")
                    for selector in ["button:has-text('Renew free')", "button:has-text('Renew')"]:
                        try:
                            btn = page.locator(selector).first
                            if btn.is_visible(timeout=2000):
                                btn.click()
                                time.sleep(2)
                                break
                        except Exception:
                            continue

            # ========== 第2步：下载并识别验证码 ==========
            print("   ⏳ 等待验证码图片加载...")
            time.sleep(1)

            gif_bytes = download_captcha_gif(page)
            if not gif_bytes:
                print("   ⚠️ 未获取到验证码图片，准备重试...")
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
                print("   ⚠️ 验证码识别求解失败，准备重试...")
                continue

            print(f"   📝 最终计算答案: {answer}")

            # ========== 第3步：填入答案 ==========
            input_selectors = [
                "[id^='captcha_ans_']",
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
                        print(f"   ✅ 答案已填入: {answer}")
                        break
                except Exception:
                    continue

            if not input_filled:
                print("   ❌ 未找到验证码输入框，准备重试...")
                continue

            # ========== 第4步：点击 Confirm Renewal 按钮触发前端校验与提交 ==========
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
                        btn.click()
                        submitted = True
                        print(f"   ✅ 已点击 [Confirm Renewal] 按钮，正在触发 WebSocket 校验...")
                        break
                except Exception:
                    continue

            if not submitted:
                print("   ❌ 未找到提交按钮")
                continue

            # ========== 第5步：点击提交后轮询 WebSocket 返回状态 (Verified vs Incorrect) ==========
            verified = False
            is_incorrect = False
            poll_timeout = 6  # 最长轮询 6 秒

            start_time = time.time()
            while time.time() - start_time < poll_timeout:
                try:
                    # 1. 检查 token 隐藏字段
                    token_elem = page.locator("input[name='captcha_token'], [id^='captcha_token_']").first
                    token_val = token_elem.get_attribute("value") or "" if token_elem.count() > 0 else ""

                    # 2. 检查状态元素内部文本
                    status_text = ""
                    try:
                        status_text = page.locator("[id^='captcha_status_'], .captcha-field").first.inner_text()
                    except Exception:
                        pass

                    # 判定 Verified (正确)
                    if token_val.strip() or "verified" in status_text.lower():
                        verified = True
                        print("   🎉 校验结果：Verified — 验证码正确！")
                        break

                    # 判定触发重试的各种异常/错误状态 (Incorrect / Rate limited / Invalid / Timed out / Offline / Error / Please solve)
                    retry_signals = [
                        "incorrect", "try again", "invalid", "rate",
                        "offline", "error", "timed out", "please solve"
                    ]
                    status_lower = status_text.lower()
                    if any(sig in status_lower for sig in retry_signals):
                        is_incorrect = True
                        print(f"   ❌ 校验未通过，网页状态提示: '{status_text.strip()}'")
                        break
                except Exception:
                    pass
                time.sleep(0.3)

            if is_incorrect:
                print("   🔄 校验失败，等待 3 秒后进入下一轮重试（避免频繁请求触发风控）...")
                time.sleep(3)
                continue

            if not verified:
                print("   ⚠️ 校验超时，等待 3 秒后进入下一轮重试...")
                time.sleep(3)
                continue

            # ========== 第6步：验证通过，等待表单 POST 提交完成并确认结果 ==========
            print("   ⏳ 验证码正确，等待提交请求处理完成...")
            time.sleep(4)

            # 刷新页面验证最新状态
            print("   🔄 刷新页面确认最终天数...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"   ⚠️ 页面刷新异常: {e}")
            wait_for_cloudflare(page)
            time.sleep(2)

            page_text = page.locator("body").inner_text()

            # 检查是否触及 24 小时频次限制
            if "once every 24 hours" in page_text.lower():
                match_24h = re.search(r"Please try again in (\d+\s+\w+)", page_text)
                time_info = f"（请在 {match_24h.group(1)} 后重试）" if match_24h else ""
                print(f"   ⚠️ 验证码正确并已提交，但触发 24 小时限制: 24小时内只能续期一次 {time_info}")
                send_telegram_message(f"ℹ️ Openworld VPS 提示：验证码正确，但24小时内只能续期一次 {time_info}\n实例: {page.url}")
                return True

            match = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", page_text)
            if match:
                new_days = int(match.group(1))
                print(f"   📊 最新剩余天数: {new_days} 天")
                print(f"   ✅ 续期成功！天数已更新为 {new_days} 天")
            else:
                print("   ✅ 验证码已通过并提交，续期流程执行完毕！")

            return True

        except Exception as e:
            print(f"   ❌ 第 {attempt} 次尝试发生错误: {e}")
            continue

    print(f"   ❌ 连续 {max_attempts} 次尝试均未能通过验证码识别")
    return False


def parse_vps_expiry(page):
    """
    从 VPS 详情页解析 'Renews until' 时间（如 2026-09-06 10:50:30 UTC），
    转换为 GMT+8 时区时间并计算与当前时间的差距。
    返回 (expiry_dt_gmt8, remaining_hours)
    """
    try:
        html = page.content()
        # 1. 匹配 <span data-time="2026-09-06 10:50:30" ...>
        match = re.search(r'data-time="(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"', html)
        if not match:
            # 2. 备用匹配文本 2026-09-06 10:50:30 (UTC)
            match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*\((?:UTC|GMT)\)', html)

        if match:
            dt_str = match.group(1)
            dt_utc = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            gmt8_tz = timezone(timedelta(hours=8))
            dt_gmt8 = dt_utc.astimezone(gmt8_tz)
            now_gmt8 = datetime.now(gmt8_tz)
            remaining_hours = (dt_gmt8 - now_gmt8).total_seconds() / 3600.0
            return dt_gmt8, remaining_hours
    except Exception as e:
        print(f"   ⚠️ 解析 Renews until 到期时间异常: {e}")
    return None, None
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

    if not DISCORD_TOKEN:
        print("❌ 未找到 DISCORD_TOKEN 环境变量，请检查配置。")
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

        try:
            # ========== 登录 ==========
            success = login_with_discord_token(page, DISCORD_TOKEN)

            if not success:
                print("\n❌ 登录流程失败，脚本退出。")
                send_telegram_message("❌ Openworld VPS 续期失败：登录流程失败")
                browser.close()
                return

            # ========== 自动检测 VPS 列表 ==========
            target_vps_list = get_vps_urls(page)

            if not target_vps_list:
                print("\n❌ 未能从面板自动检测到任何 VPS 实例。")
                print("💡 请检查账号是否有活跃的 VPS 实例")
                save_screenshot(page, "no_vps_found")
                send_telegram_message("❌ Openworld VPS 续期失败：未在面板找到任何 VPS 实例")
                browser.close()
                return

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

                # ========== 1. 解析 Renews until 到期时间 (GMT+8) 并做 24h 频次限制检测 ==========
                expiry_dt, remaining_hours = parse_vps_expiry(page)
                days_left = 0

                if expiry_dt and remaining_hours is not None:
                    expiry_str = expiry_dt.strftime("%Y-%m-%d %H:%M:%S") + " (GMT+8)"
                    days_left = int(remaining_hours / 24.0)
                    print(f"📅 当前 VPS 到期时间 (Renews until): {expiry_str}")
                    print(f"🔍 距离到期剩余: {remaining_hours:.2f} 小时 ({remaining_hours / 24.0:.2f} 天)")

                    # 如果剩余时间 > 120 小时 (即 5.0 天)，说明 24 小时内刚刚成功续期过
                    if remaining_hours > 120.0:
                        wait_h = int(remaining_hours - 120.0)
                        wait_m = int(((remaining_hours - 120.0) % 1) * 60)
                        msg_cooldown = f"⏳ 24 小时内已成功续期过，当前处于冷却期\n到期时间: {expiry_str}\n请在 {wait_h} 小时 {wait_m} 分钟后重试"
                        print(f"   ⚠️ {msg_cooldown}")
                        send_telegram_message(f"ℹ️ Openworld VPS 无需续期\n实例: {target_url}\n{msg_cooldown}")
                        continue
                    else:
                        print(f"⚠️ 剩余 {remaining_hours / 24.0:.2f} 天 ≤ 5 天，开始执行续期...")
                else:
                    # 备用：从页面文本正则提取 "Renews in X days"
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
                        print("⚠️ 未能从页面提取剩余时间，将强制尝试续期")

                # ========== 2. 执行验证码续期 ==========
                print(f"\n{'=' * 50}")
                print("🔄 开始执行验证码续期")
                print(f"{'=' * 50}")

                renew_success = try_renew_captcha(page, initial_days=days_left)

                if renew_success:
                    # 重新解析页面上最新的 Renews until 到期时间
                    new_expiry_dt, _ = parse_vps_expiry(page)
                    if new_expiry_dt:
                        expiry_str = new_expiry_dt.strftime("%Y-%m-%d %H:%M:%S") + " (GMT+8)"
                    else:
                        expiry_time = datetime.now(timezone(timedelta(hours=8))) + timedelta(days=6)
                        expiry_str = expiry_time.strftime("%Y-%m-%d %H:%M:%S") + " (GMT+8)"

                    msg = f"✅ Openworld VPS 续期成功！\n实例: {target_url}\n到期时间已更新至: {expiry_str}"
                    print(f"✅ 续期成功！到期时间: {expiry_str}")
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

        finally:
            browser.close()
            print("\n🏁 脚本执行完毕")


if __name__ == "__main__":
    main()
