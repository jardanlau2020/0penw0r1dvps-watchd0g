#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hCaptcha 可行性探测（B 方案第一轮：只验「能不能拿到 token」）。

范围严格限定：
  1. 打开 https://discord.com/login
  2. 点 hCaptcha 勾选框 → 等挑战 → 交给 hcaptcha-challenger 解
  3. 打印结果就退出

不做：不填账密、不提交登录、不碰 OAuth、不碰 Clerk、不碰续期、不读任何账号数据。

上游：QIN2DIM/hcaptcha-challenger（2588★ / 450 fork，main 最新 commit 2026-08-15）
  - 只作为 pip 依赖调用。**该库是 GPL-3.0-or-later，源码一行都不许复制进本仓库。**
  - 它是 async-only（AgentV 要 playwright 的 async Page），本脚本因此是 async 的；
    不要试图塞进 apprenew.py 的同步流程。

环境变量：
  GEMINI_API_KEY   必填。AgentConfig 直接读这个名字（SecretStr 字段）
  CHROME_PROXY     可选。住宅出口，如 http://127.0.0.1:7890
  PROBE_HEADLESS   可选，默认 true
  PROBE_TIMEOUT    可选，整体硬超时秒数，默认 300

退出码：
  0  拿到 token（CaptchaResponse.c 里有长度 >= 50 的串）
  1  跑完但没拿到 → B 方案不可行，回 A
  2  环境问题（缺 key / 缺依赖 / 浏览器起不来）
"""
from __future__ import annotations

import asyncio
import os
import sys

SITE = "https://discord.com/login"
SHOT_DIR = "screenshots"


def die(code: int, msg: str) -> None:
    print(msg, flush=True)
    raise SystemExit(code)


def has_long_token(obj, minlen: int = 50) -> bool:
    """在 model_dump() 的结果里找长度 >= minlen 的字符串，判断 token 是否真的拿到了。"""
    if isinstance(obj, str):
        return len(obj) >= minlen
    if isinstance(obj, dict):
        return any(has_long_token(v, minlen) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(has_long_token(v, minlen) for v in obj)
    return False


def shape_of(obj, depth: int = 0):
    """把结果压成「结构 + 长度」，避免把 token 原文打进日志。"""
    if isinstance(obj, str):
        return f"str(len={len(obj)})"
    if isinstance(obj, dict):
        if depth >= 2:
            return f"dict({len(obj)} keys)"
        return {k: shape_of(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [shape_of(v, depth + 1) for v in obj[:3]] + ([f"...共 {len(obj)}"] if len(obj) > 3 else [])
    return obj


async def probe() -> int:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        die(2, "❌ 缺 GEMINI_API_KEY：hCaptcha 解算必须有多模态模型 key（AgentConfig 直接读这个变量）")

    try:
        from patchright.async_api import async_playwright
    except Exception as e:  # noqa: BLE001
        die(2, f"❌ 缺 patchright：{e}（pip install -r requirements-probe.txt）")

    try:
        from hcaptcha_challenger import AgentV, AgentConfig
    except Exception as e:  # noqa: BLE001
        die(2, f"❌ 缺 hcaptcha-challenger：{e}（pip install -r requirements-probe.txt）")

    headless = os.environ.get("PROBE_HEADLESS", "true").strip().lower() not in ("0", "false", "no", "off")
    proxy = os.environ.get("CHROME_PROXY", "").strip()
    os.makedirs(SHOT_DIR, exist_ok=True)

    print(f"⚙️  headless={headless} proxy={proxy or '直连'} key=已设置({len(key)} 字符)", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            proxy={"server": proxy} if proxy else None,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/130.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = await ctx.new_page()

        print(f"🌐 打开 {SITE}", flush=True)
        await page.goto(SITE, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        print(f"   当前 URL: {page.url}", flush=True)

        try:
            await page.screenshot(path=f"{SHOT_DIR}/probe_before.png")
            print("   📸 probe_before.png", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ 截图失败: {e}", flush=True)

        agent = AgentV(page=page, agent_config=AgentConfig())

        print("🖱  点击 hCaptcha 勾选框", flush=True)
        await agent.robotic_arm.click_checkbox()

        print("⏳ 等待挑战出现（最长 120s）", flush=True)
        await agent.wait_for_challenge()

        try:
            await page.screenshot(path=f"{SHOT_DIR}/probe_after.png")
            print("   📸 probe_after.png", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️ 截图失败: {e}", flush=True)

        crs = list(getattr(agent, "cr_list", []) or [])
        print(f"📦 cr_list 长度={len(crs)}", flush=True)

        got = False
        for i, cr in enumerate(crs, 1):
            try:
                dump = cr.model_dump()
            except Exception:  # noqa: BLE001
                dump = {"repr": repr(cr)[:200]}
            print(f"   [{i}] {shape_of(dump)}", flush=True)
            if has_long_token(dump):
                got = True

        await browser.close()

    if got:
        print("✅ 拿到 hCaptcha token —— B 方案第一关通过，下一步才谈 Discord 账密登录", flush=True)
        return 0
    print("❌ 没拿到 token —— B 方案不可行，回 A（老板每 7 天导一次完整 cookie 串）", flush=True)
    return 1


def main() -> int:
    limit = int(os.environ.get("PROBE_TIMEOUT", "300") or 300)
    try:
        return asyncio.run(asyncio.wait_for(probe(), timeout=limit))
    except asyncio.TimeoutError:
        print(f"⏱️ 整体超时 {limit}s，判定没拿到", flush=True)
        return 1
    except SystemExit as e:
        return int(e.code or 0)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"❌ 探测异常：{type(e).__name__}: {e}", flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
