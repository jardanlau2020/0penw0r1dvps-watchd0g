#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rustix watchdog v6：心跳檢查 + 普通瀏覽器行 PoW 攞 cookies + curl_cffi 打 API。

v5 教訓：UC mode 斷 CDP 連線（反偵測），get_cookies() 永遠 []，
execute_script 全死。但 Chrome 行 PoW 過閘本身冇問題（呢隻牆係
jsc-post-v3 JS PoW 型，唔係 driver 指紋型）。所以 v6：
  1. 普通 mode（uc=False）+ xvfb → CDP 正常 → get_cookies() 得
  2. 瀏覽器開 rustix.me → PoW 自動行 → mit_* cookies 落齊
  3. curl_cffi（chrome124 TLS 指紋）帶 cookies + ptlc key 打 API
     （沙盒 + runner 已實測 curl_cffi chrome124 過 Gate1 無問題）

Exit codes:
  0 = 心跳健康 / TEST 模式鏈路全通
  2 = offline 但自動重啟成功（TG 已報）
  3 = offline 但重啟鏈路失敗（TG 已報，需人手撳 Start）
  4 = 連 status API 都讀唔到
"""
import json
import os
import re
import sys
import time

import requests

STATUS_URL = os.environ.get("STATUS_URL", "https://weather.2088x.com/__status")
STALE_LIMIT = int(os.environ.get("STALE_LIMIT", "1200"))
PTERO_KEY = os.environ.get("RUSTIX_PTERO_KEY", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
PANEL = os.environ.get("PANEL_URL", "https://rustix.me")
UUID_PREFIX = os.environ.get("RUSTIX_UUID_PREFIX", "e9fb06d1")
TEST_RESTART = os.environ.get("TEST_RESTART", "") in ("1", "true", "True")
IMPERSONATE = os.environ.get("CURL_IMPERSONATE", "chrome124")


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


def get_hb_age():
    try:
        r = requests.get(STATUS_URL, timeout=30)
        d = r.json()
        p = [x for x in d.get("platforms", []) if x.get("key") == "rustix"]
        if not p:
            return None
        p = p[0]
        hb = p.get("hb")
        if hb:
            return int((time.time() * 1000 - hb) / 1000)
        return int(p.get("hbAgo", 999999))
    except Exception as e:
        print("status err:", e, flush=True)
        return None


def browser_pass_gate():
    """普通 mode 瀏覽器行 PoW 過閘，回傳 (cookies_dict, reason)。"""
    from seleniumbase import SB

    with SB(uc=False, headless=False) as sb:
        sb.open(PANEL)
        # 抹 navigator.webdriver（普通 mode 唯一嘅 webdriver 痕跡）
        try:
            sb.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',"
                           "{get:()=>undefined})"})
        except Exception as e:
            print("[gate] anti-webdriver inject fail:", e, flush=True)

        deadline = time.time() + 100
        best = {}
        while time.time() < deadline:
            try:
                cs = sb.driver.get_cookies()
            except Exception as e:
                print("[gate] get_cookies err:", repr(e)[:120], flush=True)
                sb.sleep(3)
                continue
            names = sorted(c["name"] for c in cs)
            mit = [n for n in names if n.startswith("mit")]
            if mit and len(names) > len(best):
                best = {c["name"]: c["value"] for c in cs}
                print(f"[gate] cookies={names}", flush=True)
            # 有 mit_ 開頭嘅 session/PoW cookies 就再等一輪睇齊唔齊
            if mit:
                sb.sleep(8)
                try:
                    cs2 = sb.driver.get_cookies()
                    cur = {c["name"]: c["value"] for c in cs2}
                    if len(cur) >= len(best):
                        best = cur
                        print(f"[gate] final={sorted(cur.keys())}", flush=True)
                except Exception:
                    pass
                return best, None
            sb.sleep(3)
        return best, f"100s 內未見 mit_* cookies（最後 cookies={names}）"


def api_call(cookies, path, method="GET", payload=None):
    """curl_cffi chrome124 指紋 + 瀏覽器 cookies 打 panel API。"""
    from curl_cffi import requests as cffi
    s = cffi.Session()
    for k, v in cookies.items():
        s.cookies.set(k, v, domain="rustix.me", path="/")
    h = {"Authorization": f"Bearer {PTERO_KEY}",
         "Accept": "application/json"}
    if payload is not None:
        h["Content-Type"] = "application/json"
    r = s.request(method, f"{PANEL}{path}", impersonate=IMPERSONATE,
                  headers=h, timeout=30, data=json.dumps(payload) if payload is not None else None)
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        try:
            return r.status_code, (r.json() if r.text else {}), None
        except Exception:
            return r.status_code, None, f"JSON parse fail st={r.status_code}"
    return r.status_code, None, f"非 JSON（st={r.status_code} ct={ct} body={r.text[:150]})"


def find_server(data):
    servers = data.get("data", []) if isinstance(data, dict) else []
    for sv in servers:
        a = sv.get("attributes", {})
        if a.get("uuid", "").startswith(UUID_PREFIX) or a.get("identifier", "") == UUID_PREFIX:
            return a
    return servers[0].get("attributes", {}) if servers else None


def main():
    hb = get_hb_age()
    if hb is None:
        tg("❌ Rustix watchdog：weather __status 讀唔到，睇 GHA log")
        print("RUSTIX_STATUS_UNREADABLE", flush=True)
        sys.exit(4)
    print(f"heartbeat age: {hb}s (limit {STALE_LIMIT}s)", flush=True)
    if hb <= STALE_LIMIT and not TEST_RESTART:
        print(f"✅ RUSTIX_OK heartbeat={hb}s", flush=True)
        sys.exit(0)
    mode = "TEST" if TEST_RESTART else f"OFFLINE({hb}s)"
    print(f"mode={mode} → 行重啟鏈", flush=True)

    cookies, reason = browser_pass_gate()
    if not cookies:
        detail = f"過閘失敗：{reason}"
        print(f"restart chain: ok=False detail={detail}", flush=True)
        tg(f"❌ Rustix 重啟鏈（{mode}）：{detail}")
        sys.exit(2 if TEST_RESTART else 3)
    print(f"[chain] cookies ready: {sorted(cookies.keys())}", flush=True)

    st, data, err = api_call(cookies, "/api/client")
    print(f"[chain] list servers: st={st} err={err}", flush=True)
    if st != 200 or err:
        detail = f"/api/client st={st} {err}"
        print(f"restart chain: ok=False detail={detail}", flush=True)
        tg(f"❌ Rustix 重啟鏈（{mode}）：{detail}")
        sys.exit(2 if TEST_RESTART else 3)

    server = find_server(data or {})
    if not server:
        detail = "server list 空（key 冇 servers？）"
        print(f"restart chain: ok=False detail={detail}", flush=True)
        tg(f"❌ Rustix 重啟鏈（{mode}）：{detail}")
        sys.exit(2 if TEST_RESTART else 3)

    sid = server.get("identifier") or server.get("uuid", "").split("-")[0]
    cur = server.get("current_state") or server.get("status")
    print(f"[chain] server={sid} state={cur}", flush=True)

    st2, _, err2 = api_call(cookies, f"/api/client/servers/{sid}/power",
                            method="POST", payload={"signal": "start"})
    print(f"[chain] POST power start: st={st2} err={err2}", flush=True)
    if st2 not in (200, 202, 204):
        detail = f"POST start st={st2} {err2}"
        print(f"restart chain: ok=False detail={detail}", flush=True)
        tg(f"❌ Rustix 重啟鏈（{mode}）：{detail}")
        sys.exit(2 if TEST_RESTART else 3)

    if TEST_RESTART:
        detail = f"TEST 全通：閘過✓ API✓ server={sid} state={cur} POST start=204"
        print(f"restart chain: ok=True detail={detail}", flush=True)
        tg(f"🧪 Rustix 自動重啟鏈路測試：✅ {detail}")
        sys.exit(0)

    print("[chain] 等 75s 複查心跳 ...", flush=True)
    time.sleep(75)
    hb2 = get_hb_age()
    if hb2 is not None and hb2 < 60:
        print(f"restart chain: ok=True hb={hb2}s", flush=True)
        tg(f"🤖 Rustix 自動重啟成功：offline {hb}s → 心跳回復 {hb2}s（POST start 已撳）")
        sys.exit(2)
    print(f"restart chain: partial POST ok 但心跳未回（hb2={hb2}）", flush=True)
    tg(f"⚠️ Rustix POST start 已發但心跳 75s 未回（hb2={hb2}s），再等下一輪 watchdog")
    sys.exit(2)


if __name__ == "__main__":
    main()
