#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rustix watchdog FINAL：心跳檢查 + TG 預警（冇自動重啟）。

戰史（v1-v11 全部陣亡，死因各異）：
  v1-v4: 瀏覽器內 fetch → Failed to fetch（UC mode 斷 CDP）
  v5-diag E2: 導航 /api/client → ERR_HTTP2_PROTOCOL_ERROR（185KB 係 neterror 頁）
  v6: cookies 攞到但 curl_cffi 帶 cookies 打 API → server 唔認（cookie 綁指紋）
  v7: 三招（B browser-fetch / C CDP+nav / A curl_cffi+Cookie header）全斷
  v8/v9: PoW 300s 唔 reload —— UA=HeadlessChrome/152 被鎖
  v10: 真 headed Chrome 過咗 gate-1（cookies 落齊）但 PoW 後 reload 撞 neterror
  v11: --disable-http2 都救唔到，neterror 碼喺 JS 注入攞唔到
  sandbox 重演: 連 homepage 都鎖死 120s —— CDP 指紋偵測
定讞：Mitelis jsc-post-v3 係刻意反自動化牆（autoscript/headless/CDP 三重偵測），
     對得住 Openworld 先例：唔繞過，轉型 watchdog 只做預警。

職能：
  1. 每輪查 weather.2088x.com/__status 嘅 rustix 心跳
  2. 心跳新鮮（≤STALE_LIMIT）→ 靜默 exit 0
  3. 心跳過期 → TG 通知用戶人手撳 Start（附 panel URL）
  4. 每日一條摘要（北京 12:00 輪詢發）
Exit codes: 0=健康/已通知 2=offline 已 TG 通知 4=status API 死
"""
import os
import sys
import time

import requests

STATUS_URL = os.environ.get("STATUS_URL", "https://weather.2088x.com/__status")
STALE_LIMIT = int(os.environ.get("STALE_LIMIT", "1200"))
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
PANEL = os.environ.get("PANEL_URL", "https://rustix.me")
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "4"))  # UTC hour = 北京 12:00


def tg(msg):
    print(f"[TG] {msg}", flush=True)
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg}, timeout=15)
    except Exception as e:
        print("TG error:", e, flush=True)


def get_hb():
    """回 (age_seconds, raw_platform_dict)。"""
    try:
        r = requests.get(STATUS_URL, timeout=30)
        d = r.json()
        p = [x for x in d.get("platforms", []) if x.get("key") == "rustix"]
        if not p:
            return None, None
        p = p[0]
        hb = p.get("hb")
        age = int((time.time() * 1000 - hb) / 1000) if hb else int(p.get("hbAgo", 999999))
        return age, p
    except Exception as e:
        print("status err:", e, flush=True)
        return None, None


def main():
    from datetime import datetime, timezone, timedelta
    bj = timezone(timedelta(hours=8))
    now = datetime.now(bj)

    age, plat = get_hb()
    if age is None:
        tg("❌ Rustix watchdog：weather __status 讀唔到（Worker 死？）睇 GHA log")
        print("RUSTIX_STATUS_UNREADABLE", flush=True)
        sys.exit(4)

    print(f"heartbeat age: {age}s (limit {STALE_LIMIT}s)", flush=True)

    if age <= STALE_LIMIT:
        if now.hour == 12 and now.minute < 30:
            temp = (plat or {}).get("extra", {}).get("temperature", "?")
            print(f"[digest] 北京 12 點摘要：心跳 {age}s 溫度 {temp}", flush=True)
            tg(f"📊 Rustix 每日摘要：心跳 {age}s ✅ 溫度 {temp}°C")
        print(f"✅ RUSTIX_OK heartbeat={age}s", flush=True)
        sys.exit(0)

    # offline：通知人手撳 Start（唯一可行動作）
    temp = (plat or {}).get("extra", {}).get("temperature", "?")
    msg = (f"🔴 Rustix offline {age//60} 分鐘（{temp}°C）\n"
           f"Mitelis 反爬牆鎖死自動重啟（autoscript+headless+CDP 三重偵測，v1-v11 全陣亡）\n"
           f"請人手撳 Start：{PANEL} → server → Start\n"
           f"（每 30 分鐘一輪，offline 期間會再報）")
    tg(msg)
    print("RUSTIX_OFFLINE_NOTIFIED", flush=True)
    sys.exit(2)


if __name__ == "__main__":
    main()
