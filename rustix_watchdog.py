#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rustix watchdog v7：三招齊發破 Mitelis cookie-綁指紋。

v6 教訓：瀏覽器攞嘅 mit_* cookies 交 curl_cffi 用唔到（server 視為冇效
重新派 Gate1）→ cookie 綁瀏覽器指紋（UA/TLS）。v7 策略：
  B = 瀏覽器內 fetch（同瀏覽器、同指紋、同 cookies）— 首選
  C = CDP setExtraHTTPHeaders + navigation（GET only，POST 無得）
  A = curl_cffi 明文 Cookie header + 瀏覽器 UA（盡量貼近）

Exit codes:
  0 = 心跳健康 / TEST 模式（結果由 TG 報，唔令 job 紅）
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


def gate_pass(sb):
    """喺開住嘅瀏覽器度等 Mitelis 閘過（mit_* cookies 落齊）。"""
    sb.open(PANEL)
    deadline = time.time() + 100
    while time.time() < deadline:
        try:
            cs = sb.driver.get_cookies()
        except Exception as e:
            print("[gate] get_cookies err:", repr(e)[:100], flush=True)
            sb.sleep(3)
            continue
        names = sorted(c["name"] for c in cs)
        mit = [n for n in names if n.startswith("mit")]
        if mit:
            print(f"[gate] cookies={names}", flush=True)
            sb.sleep(6)  # 等最後一隻 cookie 落齊
            try:
                cs2 = sb.driver.get_cookies()
                if len(cs2) >= len(cs):
                    cs = cs2
                    print(f"[gate] final={sorted(c['name'] for c in cs)}", flush=True)
            except Exception:
                pass
            return {c["name"]: c["value"] for c in cs}
        sb.sleep(3)
    return {}


def bfetch(sb, path, method="GET", payload=None):
    """策略 B：瀏覽器內 fetch（execute_async_script）。"""
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


def cnav(sb, path):
    """策略 C：CDP 加 Authorization header + navigation（GET only）。"""
    try:
        sb.driver.execute_cdp_cmd("Network.enable", {})
        sb.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
            "headers": {"Authorization": f"Bearer {PTERO_KEY}",
                        "Accept": "application/json"}})
    except Exception as e:
        return 0, None, f"cdp fail {e!r}"
    try:
        sb.driver.get(f"{PANEL}{path}")
    except Exception as e:
        return 0, None, f"nav fail {e!r}"
    src = sb.driver.page_source or ""
    m = re.search(r"<pre[^>]*>(.*)</pre>", src, re.S)
    raw = m.group(1) if m else src
    try:
        return 200, json.loads(raw), None
    except Exception:
        return 0, None, f"nav non-json: {raw[:150]}"


def acurl(cookies, ua, path, method="GET", payload=None):
    """策略 A：curl_cffi 明文 Cookie + 瀏覽器 UA。"""
    from curl_cffi import requests as cffi
    s = cffi.Session()
    h = {"Authorization": f"Bearer {PTERO_KEY}",
         "Accept": "application/json",
         "User-Agent": ua,
         "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
    if payload is not None:
        h["Content-Type"] = "application/json"
    r = s.request(method, f"{PANEL}{path}", impersonate="chrome",
                  headers=h, timeout=30,
                  data=json.dumps(payload) if payload is not None else None)
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        try:
            return r.status_code, (r.json() if r.text else {}), None
        except Exception:
            return r.status_code, None, "json parse fail"
    return r.status_code, None, f"非JSON st={r.status_code} ct={ct} {r.text[:120]}"


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

    from seleniumbase import SB
    with SB(uc=False, headless=False) as sb:
        cookies = gate_pass(sb)
        if not cookies:
            detail = "過閘失敗：100s 內未見 mit_* cookies"
            print(f"restart chain: ok=False detail={detail}", flush=True)
            tg(f"❌ Rustix 重啟鏈（{mode}）：{detail}")
            sys.exit(0 if TEST_RESTART else 3)

        ua = sb.driver.execute_script("return navigator.userAgent")
        print(f"[gate] UA={ua}", flush=True)

        # 揀 strategy：B → C(+A POST) → A
        st, data, err = bfetch(sb, "/api/client")
        print(f"[strat B] GET st={st} err={err}", flush=True)
        if st == 200 and isinstance(data, dict):
            get_fn = lambda p: bfetch(sb, p)
            post_fn = lambda p, pl: bfetch(sb, p, "POST", pl)
            strat = "B"
        else:
            st, data, err = cnav(sb, "/api/client")
            print(f"[strat C] GET st={st} err={err}", flush=True)
            if st == 200 and isinstance(data, dict):
                get_fn = lambda p: cnav(sb, p)
                post_fn = lambda p, pl: acurl(cookies, ua, p, "POST", pl)
                strat = "C+A"
            else:
                st, data, err = acurl(cookies, ua, "/api/client")
                print(f"[strat A] GET st={st} err={err}", flush=True)
                if st == 200 and isinstance(data, dict):
                    get_fn = lambda p: acurl(cookies, ua, p)
                    post_fn = lambda p, pl: acurl(cookies, ua, p, "POST", pl)
                    strat = "A"
                else:
                    detail = f"三招全斷：B/C/A 都攞唔到 /api/client"
                    print(f"restart chain: ok=False detail={detail}", flush=True)
                    tg(f"❌ Rustix 重啟鏈（{mode}）：{detail}")
                    sys.exit(0 if TEST_RESTART else 3)
        print(f"[strat] 揀用 {strat}", flush=True)

    server = find_server(data or {})
    if not server:
        detail = "server list 空（key 冇 servers？）"
        print(f"restart chain: ok=False detail={detail}", flush=True)
        tg(f"❌ Rustix 重啟鏈（{mode}）：{detail}")
        sys.exit(0 if TEST_RESTART else 3)
    sid = server.get("identifier") or server.get("uuid", "").split("-")[0]
    cur = server.get("current_state") or server.get("status")
    print(f"[chain] server={sid} state={cur}", flush=True)

    st2, _, err2 = post_fn(f"/api/client/servers/{sid}/power", {"signal": "start"})
    print(f"[chain] POST power start: st={st2} err={err2}", flush=True)
    if st2 not in (200, 202, 204):
        detail = f"POST start st={st2} {err2}"
        print(f"restart chain: ok=False detail={detail}", flush=True)
        tg(f"❌ Rustix 重啟鏈（{mode}）：{detail}")
        sys.exit(0 if TEST_RESTART else 3)

    if TEST_RESTART:
        detail = f"TEST 全通（{strat}）：閘✓ API✓ server={sid} state={cur} POST start={st2}"
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
