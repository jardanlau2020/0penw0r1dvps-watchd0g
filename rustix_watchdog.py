#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rustix watchdog v8：瀏覽器導航 /api/client 行埋 PoW → 同 page fetch POST。

v7 教訓：只開 homepage 得 gate-1 cookies，冇行埋 jsc-post-v3 PoW，
所以 fetch 攞到嘅係 challenge page。v5-diag E2 又見過
ERR_HTTP2_PROTOCOL_ERROR（E1/E5 嘅 Failed to fetch 係 page 停咗喺
neterror，fetch 變跨域）。

v8 處方：
  1. chromium --disable-http2（h1.1，斷 ERR_HTTP2_PROTOCOL_ERROR 源頭）
  2. CDP Network.setExtraHTTPHeaders 注 Authorization（GET 都帶 key）
  3. 瀏覽器導航去 {PANEL}/api/client → gate-1 → PoW challenge →
     自動 reload → JSON 出現喺 page
  4. 等 page 由 challenge 變 JSON（poll）
  5. POST power start 用同 page fetch（同 origin、同 cookies、同指紋）

Exit codes:
  0 = 心跳健康 / TEST 模式（結果 TG 報，唔染紅 job）
  2 = offline 但自動重啟成功（TG 已報）
  3 = offline 但重啟鏈路失敗（TG 已報，需人手撳 Start）
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
POW_WAIT = int(os.environ.get("POW_WAIT", "90"))


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


def cdp_auth(sb):
    """CDP 注 Authorization + Accept 到所有後續請求。"""
    sb.driver.execute_cdp_cmd("Network.enable", {})
    sb.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
        "headers": {"Authorization": f"Bearer {PTERO_KEY}",
                    "Accept": "application/json"}})


def nav_json(sb, path):
    """導航去 API path，等 PoW cycle 完成，parse <pre> 入面嘅 JSON。"""
    sb.driver.get(f"{PANEL}{path}")
    deadline = time.time() + POW_WAIT
    t0 = time.time()
    last = ""
    while time.time() < deadline:
        src = sb.driver.page_source or ""
        if "jsc-post" not in src and "FsGtA7" not in src and "<script" not in src[:2000]:
            m = re.search(r"<pre[^>]*>(.*)</pre>", src, re.S)
            raw = htmlmod.unescape((m.group(1) if m else src).strip())
            if raw.startswith("{"):
                try:
                    d = json.loads(raw)
                    print(f"[nav] JSON ready @+{int(time.time()-t0)}s "
                          f"cookies={sorted(c['name'] for c in sb.driver.get_cookies())}",
                          flush=True)
                    return 200, d, None
                except json.JSONDecodeError:
                    pass
        cur = src[:60].replace("\n", " ")
        if cur != last:
            print(f"[nav] +{int(time.time()-t0)}s still-challenge page: {cur}", flush=True)
            last = cur
        time.sleep(2.5)
    return 0, None, f"PoW {POW_WAIT}s 內未變 JSON（最後：{(sb.driver.page_source or '')[:120]}）"


def bfetch(sb, path, method="GET", payload=None):
    """同 page 內 fetch（同 origin、同 cookies、同指紋）。"""
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


def bxhr(sb, path, payload):
    """XHR 後備（fetch 為乜死咗嘅話）。"""
    js = """
    const [url, key, body] = arguments;
    const cb = arguments[arguments.length - 1];
    const x = new XMLHttpRequest();
    x.open('POST', url, true);
    x.setRequestHeader('Authorization', 'Bearer ' + key);
    x.setRequestHeader('Content-Type', 'application/json');
    x.withCredentials = true;
    x.onload = () => cb({st: x.status, t: (x.responseText || '').slice(0, 3000)});
    x.onerror = () => cb({st: 0, t: 'XHR_ERR'});
    x.send(body);
    """
    try:
        res = sb.driver.execute_async_script(
            js, f"{PANEL}{path}", PTERO_KEY, json.dumps(payload))
    except Exception as e:
        return 0, None, f"exec fail {e!r}"
    if not isinstance(res, dict):
        return 0, None, f"weird res {res!r}"
    st, txt = res.get("st", 0), res.get("t", "")
    if st == 200:
        try:
            return st, json.loads(txt), None
        except Exception:
            return st, None, None
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
    with SB(uc=False, headless=False, chromium_arg="--disable-http2") as sb:
        cdp_auth(sb)
        st, data, err = nav_json(sb, "/api/client")
        print(f"[chain] nav /api/client: st={st} err={err}", flush=True)
        if st != 200 or not isinstance(data, dict):
            fail(mode, f"/api/client 導航後攞唔到 JSON：{err}", TEST_RESTART)

    server = find_server(data or {})
    if not server:
        fail(mode, "server list 空（key 冇 servers？）", TEST_RESTART)
    sid = server.get("identifier") or (server.get("uuid", "") or "").split("-")[0]
    cur = server.get("current_state") or server.get("status") or "?"
    print(f"[chain] server={sid} state={cur} name={server.get('name')!r}", flush=True)

    with SB(uc=False, headless=False, chromium_arg="--disable-http2") as sb:
        # 再導航一次行 PoW（每個 SB session cookies 由 0 開始），然後同 page POST
        cdp_auth(sb)
        st0, d0, err0 = nav_json(sb, "/api/client")
        if st0 != 200:
            fail(mode, f"二次導航失敗：{err0}", TEST_RESTART)
        st2, _, err2 = bfetch(sb, f"/api/client/servers/{sid}/power",
                              "POST", {"signal": "start"})
        print(f"[chain] POST power start (fetch): st={st2} err={err2}", flush=True)
        if st2 == 0 and "Failed to fetch" in str(err2 or ""):
            st2, _, err2 = bxhr(sb, f"/api/client/servers/{sid}/power", {"signal": "start"})
            print(f"[chain] POST power start (xhr): st={st2} err={err2}", flush=True)

    if st2 not in (200, 202, 204):
        fail(mode, f"POST start st={st2} {err2}", TEST_RESTART)

    if TEST_RESTART:
        detail = f"TEST 全通：PoW✓ nav-JSON✓ server={sid} state={cur} POST start={st2}"
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
