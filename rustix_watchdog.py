#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rustix watchdog v9：無 CDP headers 導航行 PoW → 過閘後 fetch 帶 key。

v8 教訓：
  1. CDP setExtraHTTPHeaders 注 Authorization 改變 server 見到嘅指紋
     → 被視為新 identity 派新挑戰 → 死循環
  2. jsc-post-v3 係真 PoW，headless Chrome 求解隨時 >90s → POW_WAIT=300
v9 流程：
  1. headed Chrome（xvfb）導航 /api/client（唔帶任何 header）
  2. 挑戰 page 自動行 PoW（可能幾分鐘）→ poll 等 page 變 JSON
     （Ptero 對無 key 請求返 401 JSON「資源不存在」——即係過咗閘！）
  3. 過閘後同 page fetch 帶 Authorization：GET servers → POST power start

Exit codes:
  0 = 心跳健康 / TEST 模式（結果 TG 報，唔染紅 job）
  2 = offline 但自動重啟成功 / POST 已發（TG 已報）
  3 = offline 但重啟鏈路失敗（TG 已報，需人手撞 Start）
  4 = 連 status API 都讀唔到
"""
import html as htmlmod
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
POW_WAIT = int(os.environ.get("POW_WAIT", "300"))


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


def nav_wait_pow(sb, path):
    """導航 path，等 PoW cycle 完成。回 (status, json_or_None, err)。

    過閘證據：page source 出現 JSON（body 或 <pre>）。
    Ptero 無 key 會返 401 JSON —— 都算過咗閘（response 係後端嘅）。
    """
    sb.driver.get(f"{PANEL}{path}")
    deadline = time.time() + POW_WAIT
    t0 = time.time()
    last_cookies = ""
    while time.time() < deadline:
        try:
            src = sb.driver.page_source or ""
            cookies = sorted(c["name"] for c in sb.driver.get_cookies())
        except Exception as e:
            print(f"[nav] +{int(time.time()-t0)}s driver err {e!r}", flush=True)
            time.sleep(3)
            continue
        if cookies != last_cookies:
            print(f"[nav] +{int(time.time()-t0)}s cookies={cookies}", flush=True)
            last_cookies = cookies
        # 挑戰頁特徵：src 含 jsc-post / FsGtA7 挑戰 script
        if "jsc-post" in src or "FsGtA7" in src:
            time.sleep(3)
            continue
        # 非 JSON page（neterror 等）
        m = re.search(r"<pre[^>]*>(.*)</pre>", src, re.S)
        raw = htmlmod.unescape((m.group(1) if m else src).strip())
        if raw.startswith("{"):
            try:
                d = json.loads(raw)
                print(f"[nav] +{int(time.time()-t0)}s JSON ready "
                      f"(keys={list(d)[:4]})", flush=True)
                return 200, d, None
            except json.JSONDecodeError:
                pass
        if "net::ERR" in src or "ERR_" in src[:3000]:
            return 0, None, f"chrome neterror: {src[:150]}"
        time.sleep(3)
    return 0, None, (f"PoW {POW_WAIT}s 未完成（最後 cookies={last_cookies} "
                     f"page={src[:100]}）")


def bfetch(sb, path, method="GET", payload=None):
    """過咗閘嘅 page 內 fetch，帶 Authorization。"""
    js = """
    const [url, method, body, key] = arguments;
    const cb = arguments[arguments.length - 1];
    const h = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json'};
    if (body) h['Content-Type'] = 'application/json';
    fetch(url, {method: method, headers: h, credentials: 'include',
                body: body || undefined})
      .then(async r => { cb({st: r.status, t: (await r.text()).slice(0, 300000)}); })
      .catch(e => cb({st: 0, t: 'ERR:' + String(e)}));
    """
    try:
        res = sb.driver.execute_async_script(
            js, f"{PANEL}{path}", method,
            json.dumps(payload) if payload is not None else "", PTERO_KEY)
    except Exception as e:
        return 0, None, f"exec fail {e!r}"
    if not isinstance(res, dict):
        return 0, None, f"weird res {res!r}"
    st, txt = res.get("st", 0), res.get("t", "")
    if st == 200:
        try:
            return st, json.loads(txt), None
        except Exception:
            return st, None, f"json fail: {txt[:150]}"
    return st, None, (txt or "")[:200]


def find_server(data):
    servers = data.get("data", []) if isinstance(data, dict) else []
    for sv in servers:
        a = sv.get("attributes", {}) if isinstance(sv, dict) else {}
        if a.get("uuid", "").startswith(UUID_PREFIX) or a.get("identifier", "") == UUID_PREFIX:
            return a
    if servers:
        first = servers[0]
        return first.get("attributes", {}) if isinstance(first, dict) else {}
    return None


def fail(mode, detail, test):
    print(f"restart chain: ok=False detail={detail}", flush=True)
    tg(f"❌ Rustix 重啟鏈（{mode}）：{detail}")
    sys.exit(0 if test else 3)


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

    from seleniumbase import SB
    with SB(uc=False, headless=False) as sb:
        ua = sb.driver.execute_script("return navigator.userAgent")
        print(f"[gate] UA={ua}", flush=True)
        st, d, err = nav_wait_pow(sb, "/api/client")
        print(f"[chain] nav /api/client: st={st} err={err}", flush=True)
        if st != 200:
            fail(mode, f"導航行 PoW 失敗：{err}", TEST_RESTART)
        # 過咗閘（唔理 401 定 200，總之攞到後端 JSON）
        st2, data, err2 = bfetch(sb, "/api/client")
        print(f"[chain] fetch /api/client: st={st2} err={err2}", flush=True)
        if st2 != 200 or not isinstance(data, dict):
            fail(mode, f"fetch /api/client st={st2} {err2}", TEST_RESTART)
        server = find_server(data)
        if not server:
            fail(mode, "server list 空（key 冇 servers？）", TEST_RESTART)
        sid = server.get("identifier") or (server.get("uuid", "") or "").split("-")[0]
        cur = server.get("current_state") or server.get("status") or "?"
        print(f"[chain] server={sid} state={cur} name={server.get('name')!r}", flush=True)

        st3, _, err3 = bfetch(sb, f"/api/client/servers/{sid}/power",
                              "POST", {"signal": "start"})
        print(f"[chain] POST power start: st={st3} err={err3}", flush=True)

    if st3 not in (200, 202, 204):
        fail(mode, f"POST start st={st3} {err3}", TEST_RESTART)

    if TEST_RESTART:
        detail = f"TEST 全通：PoW✓ fetch✓ server={sid} state={cur} POST={st3}"
        print(f"restart chain: ok=True detail={detail}", flush=True)
        tg(f"🧪 Rustix 自動重啟鏈路測試：✅ {detail}")
        sys.exit(0)

    print("[chain] 等 75s 複查心跳 ...", flush=True)
    time.sleep(75)
    hb2 = get_hb_age()
    if hb2 is not None and hb2 < 60:
        print(f"restart chain: ok=True hb={hb2}s", flush=True)
        tg(f"🤖 Rustix 自動重啟成功：offline {hb}s → 心跳回復 {hb2}s")
        sys.exit(2)
    print(f"restart chain: partial POST ok 但心跳未回（hb2={hb2}）", flush=True)
    tg(f"⚠️ Rustix POST start 已發但心跳 75s 未回（hb2={hb2}s），等下一輪 watchdog")
    sys.exit(2)


if __name__ == "__main__":
    main()
