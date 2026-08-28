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
from PIL import Image
import numpy as np
from playwright.sync_api import sync_playwright

# ================= 配置区 =================
# 从 GitHub Secrets 环境变量获取 Discord Token
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")

# TG 通知（可选）
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")

# 目标 VPS 控制面板页面 (直达该实例页面)
TARGET_URL = "https://openworld.eu.org/vps/6f981ff7-9723-44c1-b759-bc0ab67b2b9b"

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
    """保存调试截图"""
    try:
        path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
        page.screenshot(path=path)
        print(f"📸 截图已保存: {path}")
    except Exception as e:
        print(f"⚠️ 截图保存失败: {e}")


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
    # 尝试继续，后续访问 TARGET_URL 会验证
    return True


def composite_gif_frames(gif_bytes: bytes) -> Image.Image:
    """
    将多帧 GIF 的所有帧合成为一张图片。
    采用「取每像素最暗值」策略，确保分布在不同帧上的数字和运算符全部保留。
    """
    gif = Image.open(io.BytesIO(gif_bytes))
    frames = []
    try:
        while True:
            # 转为 RGBA 以统一处理
            frame = gif.convert("RGBA")
            frames.append(np.array(frame))
            gif.seek(gif.tell() + 1)
    except EOFError:
        pass

    print(f"   📊 GIF 共 {len(frames)} 帧，尺寸: {frames[0].shape[1]}x{frames[0].shape[0]}")

    if len(frames) == 1:
        return Image.fromarray(frames[0])

    # 策略：取每个像素位置上所有帧中最暗的值（最小RGB）
    # 这样所有帧上的深色文字都会被保留到合成图上
    stacked = np.stack(frames, axis=0)  # (N, H, W, 4)
    composited = np.min(stacked, axis=0)  # (H, W, 4)

    return Image.fromarray(composited.astype(np.uint8))


def preprocess_captcha_image(img: Image.Image) -> Image.Image:
    """
    预处理验证码图片以提升 OCR 识别率：
    - 转灰度
    - 二值化（去除噪点干扰线）
    - 放大（提升小字识别率）
    """
    # 转灰度
    gray = img.convert("L")

    # 二值化：低于阈值的为黑色（文字），高于的为白色（背景）
    threshold = 180
    binary = gray.point(lambda p: 0 if p < threshold else 255, "L")

    # 放大2倍提升识别率
    w, h = binary.size
    binary = binary.resize((w * 2, h * 2), Image.LANCZOS)

    return binary


def solve_captcha_expression(ocr_text: str) -> str:
    """
    从 OCR 识别结果中解析数学算式并计算答案。
    验证码通常是简单的加减乘运算，如 "3+5", "12-7", "4x2" 等。
    返回计算结果的字符串，如果无法解析则返回原文。
    """
    # 清理 OCR 输出
    text = ocr_text.strip()
    # 常见 OCR 误识别修正
    text = text.replace(" ", "")   # 去空格
    text = text.replace("=", "")   # 去等号
    text = text.replace("?", "")   # 去问号
    text = text.replace("O", "0")  # 字母O → 数字0
    text = text.replace("o", "0")
    text = text.replace("l", "1")  # 小写L → 数字1
    text = text.replace("I", "1")  # 大写I → 数字1
    text = text.replace("×", "*")  # 乘号
    text = text.replace("x", "*")  # 小写x → 乘号
    text = text.replace("X", "*")
    text = text.replace("÷", "/")  # 除号
    text = text.replace("一", "-")  # 中文横线 → 减号

    print(f"   🧮 清理后算式: '{text}'")

    # 尝试匹配 "数字 运算符 数字" 的模式
    match = re.match(r'^(\d+)\s*([+\-*/])\s*(\d+)$', text)
    if match:
        a = int(match.group(1))
        op = match.group(2)
        b = int(match.group(3))
        if op == '+':
            result = a + b
        elif op == '-':
            result = a - b
        elif op == '*':
            result = a * b
        elif op == '/':
            result = a // b  # 整除
        else:
            return text
        print(f"   🧮 计算: {a} {op} {b} = {result}")
        return str(result)

    # 如果正则不匹配，尝试用 eval 安全计算
    try:
        # 只允许数字和基本运算符
        safe_text = re.sub(r'[^0-9+\-*/]', '', text)
        if safe_text and re.match(r'^\d+[+\-*/]\d+$', safe_text):
            result = eval(safe_text)
            print(f"   🧮 eval 计算: {safe_text} = {result}")
            return str(int(result))
    except Exception:
        pass

    print(f"   ⚠️ 无法解析为算式，将原样提交: '{text}'")
    return text


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


def try_renew_captcha(page, max_attempts=3) -> bool:
    """
    尝试执行验证码续期流程，最多重试 max_attempts 次。
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
            # 首次尝试时点击 Renew free 按钮
            if attempt == 1:
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
                    save_screenshot(page, "renew_no_button")
                    return False
                time.sleep(3)
            else:
                # 重试时刷新验证码（点击刷新按钮）
                print("   🔄 刷新验证码...")
                try:
                    # 尝试找刷新按钮（第二张截图中可见有个刷新图标 ↻）
                    refresh_selectors = [
                        "button:near(img[alt='Captcha'])",
                        "button:has(svg):near(img[alt='Captcha'])",
                        ".modal-panel button:has(svg)",
                    ]
                    for sel in refresh_selectors:
                        try:
                            rbtn = page.locator(sel).first
                            if rbtn.is_visible(timeout=2000):
                                rbtn.click()
                                print("   ✅ 已点击验证码刷新按钮")
                                time.sleep(2)
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

            # ========== 下载并处理验证码 GIF ==========
            print("   ⏳ 等待验证码图片加载...")
            time.sleep(1)

            gif_bytes = download_captcha_gif(page)
            if not gif_bytes:
                save_screenshot(page, f"renew_no_captcha_{attempt}")
                continue

            # 保存原始 GIF（调试用）
            gif_path = os.path.join(SCREENSHOT_DIR, f"captcha_raw_{attempt}.gif")
            try:
                with open(gif_path, "wb") as f:
                    f.write(gif_bytes)
                print(f"   💾 原始验证码已保存: {gif_path}")
            except Exception:
                pass

            # 合成多帧
            try:
                composited = composite_gif_frames(gif_bytes)
            except Exception as e:
                print(f"   ⚠️ GIF 合成失败，使用原图: {e}")
                composited = Image.open(io.BytesIO(gif_bytes)).convert("RGBA")

            # 保存合成图（调试用）
            composited_path = os.path.join(SCREENSHOT_DIR, f"captcha_composited_{attempt}.png")
            try:
                composited.save(composited_path)
                print(f"   💾 合成验证码已保存: {composited_path}")
            except Exception:
                pass

            # 预处理并 OCR
            processed = preprocess_captcha_image(composited)

            # 保存预处理图（调试用）
            processed_path = os.path.join(SCREENSHOT_DIR, f"captcha_processed_{attempt}.png")
            try:
                processed.save(processed_path)
                print(f"   💾 预处理验证码已保存: {processed_path}")
            except Exception:
                pass

            # OCR 识别
            img_buffer = io.BytesIO()
            processed.save(img_buffer, format="PNG")
            ocr_text = ocr.classification(img_buffer.getvalue())
            print(f"   📝 OCR 识别原始结果: '{ocr_text}'")

            if not ocr_text or len(ocr_text) < 2:
                print("   ⚠️ OCR 识别结果为空或太短，重试...")
                continue

            # 计算算式结果
            answer = solve_captcha_expression(ocr_text)
            print(f"   📝 最终答案: {answer}")

            # ========== 填入并提交 ==========
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
                save_screenshot(page, f"renew_no_input_{attempt}")
                continue

            # 提交
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
                        print(f"   ✅ 已点击提交按钮 (选择器: {selector})")
                        break
                except Exception:
                    continue

            if not submitted:
                print("   ❌ 未找到提交按钮")
                save_screenshot(page, f"renew_no_submit_{attempt}")
                continue

            # 等待结果
            time.sleep(5)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(2)

            save_screenshot(page, f"renew_result_{attempt}")

            # 检查是否成功：验证码弹窗消失 = 成功
            page_text = page.locator("body").inner_text()
            if "Please solve the captcha" in page_text or "Verification" in page_text.upper():
                print(f"   ⚠️ 验证码可能填错，弹窗仍在，准备重试...")
                continue

            print("   ✅ 续期流程执行完毕！")
            return True

        except Exception as e:
            print(f"   ❌ 第 {attempt} 次尝试发生错误: {e}")
            save_screenshot(page, f"renew_error_{attempt}")
            continue

    print(f"   ❌ {max_attempts} 次尝试均失败")
    return False


def main():
    print("#" * 50)
    print("   Openworld VPS 自动续期脚本")
    print("#" * 50)

    if not DISCORD_TOKEN:
        print("❌ 未找到 DISCORD_TOKEN 环境变量，请检查配置。")
        sys.exit(1)

    headless_mode = os.environ.get("HEADLESS", "true").lower() == "true"
    print(f"🖥️  运行模式: {'无头' if headless_mode else '有头'}")
    print(f"🎯 目标 URL: {TARGET_URL}")

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

            # ========== 导航到目标 VPS 页面 ==========
            print(f"\n{'=' * 50}")
            print(f"📌 导航到目标 VPS 页面: {TARGET_URL}")
            print(f"{'=' * 50}")

            try:
                page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
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
                save_screenshot(page, "redirect_to_login")
                send_telegram_message("❌ Openworld VPS 续期失败：登录后仍被重定向到登录页")
                browser.close()
                return

            if "/vps/" not in current_url:
                print(f"⚠️ 当前页面可能不是 VPS 详情页: {current_url}")
                save_screenshot(page, "not_vps_page")

            print("✅ 已成功到达目标 VPS 页面")
            save_screenshot(page, "vps_page_loaded")

            # ========== 检查剩余天数 ==========
            page_text = page.locator("body").inner_text()

            # 匹配 "Renews in X days" 或类似文本
            match = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", page_text)

            if match:
                days_left = int(match.group(1))
                print(f"🔍 当前 VPS 剩余续期时间: {days_left} 天")

                if days_left > RENEW_THRESHOLD_DAYS:
                    msg = f"⏳ 剩余 {days_left} 天 > {RENEW_THRESHOLD_DAYS} 天阈值，跳过续期"
                    print(msg)
                    send_telegram_message(f"ℹ️ Openworld VPS 无需续期\n剩余时间: {days_left} 天")
                    browser.close()
                    return
                else:
                    print(f"⚠️ 剩余 {days_left} 天 ≤ {RENEW_THRESHOLD_DAYS} 天，开始执行续期...")
            else:
                print("⚠️ 未能从页面提取剩余天数，将强制尝试续期")
                # 打印部分页面文本以便调试
                print(f"   页面文本片段: {page_text[:500]}")

            # ========== 执行续期 ==========
            print(f"\n{'=' * 50}")
            print("🔄 开始执行验证码续期")
            print(f"{'=' * 50}")

            renew_success = try_renew_captcha(page)

            if renew_success:
                # 重新读取页面状态
                time.sleep(3)
                new_page_text = page.locator("body").inner_text()
                new_match = re.search(r"[Rr]enews?\s+in\s+(\d+)\s+days?", new_page_text)

                if new_match:
                    new_days = int(new_match.group(1))
                    msg = f"✅ Openworld VPS 续期成功！\n新的剩余时间: {new_days} 天"
                    print(f"✅ 续期成功！新的剩余天数: {new_days}")
                else:
                    msg = "✅ Openworld VPS 续期流程已执行完毕\n请手动确认结果"
                    print("✅ 续期流程执行完毕，但未能确认新的剩余天数")

                send_telegram_message(msg)
            else:
                print("❌ 续期失败")
                send_telegram_message("❌ Openworld VPS 续期失败：验证码续期流程出错")

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
