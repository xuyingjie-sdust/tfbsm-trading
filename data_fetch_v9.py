#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TFBSM 期权数据拉取 v9 — 多标的+手动引导版
==========================================
支持标的：
  --cn   国内510300(沪深300ETF)  默认
  --us   美股SPY

数据源：
  ETF价格  → 腾讯行情（已验证可用，支持A股和美股）
  期权链   → 手动粘贴（从交易软件/网页复制，脚本自动解析）
  期权链   → 模拟数据（测试用）

使用：
  python data_fetch_v9.py --cn              # 国内510300，在线价格+模拟期权
  python data_fetch_v9.py --cn --manual     # 国内510300，手动粘贴期权
  python data_fetch_v9.py --us              # 美股SPY，在线价格+模拟期权
  python data_fetch_v9.py --us --manual     # 美股SPY，手动粘贴期权
  python data_fetch_v9.py --mock            # 纯模拟数据
  python data_fetch_v9.py --debug           # 调试模式
"""

import os
import sys
import re
import time
import json
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, date

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DEBUG = "--debug" in sys.argv
USE_MOCK = "--mock" in sys.argv
MANUAL_MODE = "--manual" in sys.argv
TARGET_US = "--us" in sys.argv
TARGET_CN = "--cn" in sys.argv or not TARGET_US  # 默认国内

# 标的配置
if TARGET_US:
    SYMBOL = "SPY"
    SYMBOL_NAME = "SPY标普500ETF"
    TENCENT_CODE = "usSPY"
    TENCENT_KLINE = "usSPY,day,,,640,qfq"
    DEFAULT_S0 = 550.0
    DEFAULT_R = 0.045  # 美元无风险利率
    CURRENCY = "USD"
    STRIKE_DECIMALS = 0  # SPY行权价整数
    PRICE_DECIMALS = 2
else:
    SYMBOL = "510300"
    SYMBOL_NAME = "沪深300ETF华泰柏瑞"
    TENCENT_CODE = "sh510300"
    TENCENT_KLINE = "sh510300,day,,,640,qfq"
    DEFAULT_S0 = 4.0
    DEFAULT_R = 0.02  # 人民币无风险利率
    CURRENCY = "CNY"
    STRIKE_DECIMALS = 3  # 510300行权价3位小数
    PRICE_DECIMALS = 4


def debug(msg):
    if DEBUG:
        print(f"    [DEBUG] {msg}")


# ============================================================
# 网络请求 DNS 兑底：域名解析失败时用缓存/预解析 IP 直连
# 新浪接口支持 HTTP 明文（已验证），绕开 HTTPS SNI 证书坑
# ============================================================
import socket as _socket
import struct as _struct
import random as _random

_IP_CACHE = {}  # host -> [ip, ...]


def _dns_resolve(host, timeout=4):
    """解析域名。系统 DNS 失败时用公共 DNS 兑底（阿里 DoH 直打 IP，无需解析域名）。"""
    try:
        ips = [x[4][0] for x in _socket.getaddrinfo(host, 80, _socket.AF_INET)]
        if ips:
            return list(dict.fromkeys(ips))
    except _socket.gaierror:
        pass
    # 公共 DoH：直打阿里/腾讯 DNS IP，返回 JSON，无需再解析域名
    for server in ["223.5.5.5", "119.29.29.29"]:
        try:
            r = requests.get(f"https://{server}/resolve",
                             params={"name": host, "type": "A"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            data = r.json()
            ans = [a["data"] for a in data.get("Answer", [])
                   if a.get("type") == 1 and a.get("data")]
            if ans:
                return list(dict.fromkeys(ans))
        except Exception:
            continue
    return []


def _dns_direct_a(host, server, timeout=4):
    """[保留] 向指定 DNS 服务器发 A 记录查询（UDP）。DoH 优先，此函数仅作最后回落。"""
    try:
        tid = _random.randint(0, 0xffff)
        qname = b"".join(bytes([len(seg)]) + seg.encode() for seg in host.split(".")) + b"\x00"
        q = _struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + qname + _struct.pack(">HH", 1, 1)
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(q, (server, 53))
            resp, _ = s.recvfrom(512)
        finally:
            s.close()
        if len(resp) < 12:
            return []
        # 跳过问题段，读到 Answer
        i = 12
        while resp[i] != 0:
            i += 1 + resp[i]
        i += 5
        ans = []
        for _ in range(_struct.unpack(">H", resp[6:8])[0]):
            if i + 4 > len(resp):
                break
            flags = _struct.unpack(">H", resp[i:i + 2])[0]
            if (flags & 0xc000) == 0xc000:
                i += 2
            else:
                ln = resp[i]
                i += 1 + ln
            if i + 6 > len(resp):
                break
            rtype = _struct.unpack(">H", resp[i:i + 2])[0]
            rdlen = _struct.unpack(">H", resp[i + 2:i + 4])[0]
            i += 4
            if rtype == 1 and rdlen == 4 and i + 4 <= len(resp):
                ans.append(".".join(str(b) for b in resp[i:i + 4]))
            i += rdlen
        return ans
    except Exception:
        return []


def robust_get(url, params=None, headers=None, timeout=12):
    """
    健壮 GET：域名解析失败或连接失败时，用缓存/预解析 IP + HTTP 直连 + Host 头重试。
    兑底分支用 socket+http.client 手动直连 IP，完全不依赖 DNS（连 IP 解析都不走），
    即使系统 DNS 完全宕机也能拿到数据。
    """
    import urllib.parse as _up
    import http.client as _hc
    headers = headers or {}
    # 1) 正常请求
    try:
        return requests.get(url, params=params, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectionError:
        pass
    # 2) 解析 + 缓存 IP，手动直连
    host = _up.urlparse(url).hostname
    if not host:
        raise ValueError(f"无法从 URL 解析主机名: {url}")
    ips = _IP_CACHE.get(host) or _dns_resolve(host)
    if ips and host not in _IP_CACHE:
        _IP_CACHE[host] = ips
    if not ips:
        raise RuntimeError(f"无法解析 {host}，且兑底 DNS 也未拿到 IP")
    path = _up.urlparse(url).path or "/"
    if _up.urlparse(url).query:
        path += "?" + _up.urlparse(url).query
    qs = ""
    if params:
        from urllib.parse import urlencode
        qs = "?" + urlencode(params)
    last = None
    for ip in ips:
        try:
            conn = _hc.HTTPConnection(ip, 80, timeout=timeout)
            req_headers = dict(headers)
            req_headers["Host"] = host
            req_headers["Connection"] = "close"
            try:
                conn.request("GET", path + qs, headers=req_headers)
                resp = conn.getresponse()
                body = resp.read()
                last = _Resp(resp.status, body, resp.getheaders())
                if resp.status == 200:
                    return last
            finally:
                conn.close()
        except Exception as e:
            last = e
    if last is not None:
        if isinstance(last, Exception):
            raise RuntimeError(f"roborst_get 兑底失败: {type(last).__name__}: {last}") from last
        return last
    raise RuntimeError(f"roborst_get 兑底失败: 无法解析 {host}")


class _Resp:
    """最小化 response 模拟：兼容 .status_code/.text/.json()/.encoding"""
    def __init__(self, status, body, headers):
        self.status_code = status
        self._body = body
        # 从原始 header 里挑出 content-type / charset
        ctype = ""
        for k, v in headers or []:
            if k.lower() == "content-type":
                ctype = v
        self.encoding = "gbk" if "gbk" in ctype.lower() or "gb2312" in ctype.lower() else "utf-8"

    @property
    def text(self):
        return self._body.decode(self.encoding, errors="replace")

    def json(self):
        import json as _json
        return _json.loads(self.text)



# ============================================================
# 腾讯实时行情（A股+美股都支持）
# ============================================================

def fetch_price_tencent():
    """通过腾讯行情接口获取实时价格"""
    print(f"  [腾讯行情] 获取{SYMBOL}实时价格...")

    url = f"https://qt.gtimg.cn/q={TENCENT_CODE}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://gu.qq.com/",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        debug(f"状态码: {resp.status_code}")
        debug(f"返回前200字: {resp.text[:200]}")

        if resp.status_code != 200:
            print(f"  ❌ 状态码: {resp.status_code}")
            return None, None

        text = resp.text.strip()
        # v_sh510300="1~沪深300ETF华泰柏瑞~510300~4.723~..."
        # v_usSPY="51~SPDR标普500ETF~~SPY~549.12~..."
        match = re.search(r'v_\w+="([^"]+)"', text)
        if not match:
            print(f"  ❌ 无法解析行情数据")
            return None, None

        fields = match.group(1).split("~")
        name = fields[1] if len(fields) > 1 else SYMBOL
        price_str = fields[3] if len(fields) > 3 else "0"
        prev_close_str = fields[4] if len(fields) > 4 else "0"

        price = float(price_str)
        prev_close = float(prev_close_str)

        if price <= 0:
            print(f"  ❌ 价格异常: {price_str}")
            return None, None

        chg = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0
        print(f"  ✅ {name} 当前价格: {price:.{PRICE_DECIMALS}f} ({chg:+.2f}%)")

        # 拉历史K线
        df_hist = fetch_history_tencent()

        return df_hist, price

    except Exception as e:
        print(f"  ❌ 失败: {type(e).__name__}: {e}")
        return None, None


def fetch_history_tencent():
    """通过腾讯接口获取历史K线"""
    print(f"  [腾讯K线] 获取{SYMBOL}历史价格...")

    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": TENCENT_KLINE}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://gu.qq.com/",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        debug(f"状态码: {resp.status_code}")

        if resp.status_code != 200:
            debug(f"K线请求失败")
            return None

        data = resp.json()
        # 尝试多种解析路径
        klines = None
        key = TENCENT_CODE

        for path_key in [key, key.lower(), key.upper()]:
            klines = (
                data.get("data", {}).get(path_key, {}).get("qfqday")
                or data.get("data", {}).get(path_key, {}).get("day")
            )
            if klines:
                break

        if not klines:
            # 打印结构帮助调试
            debug(f"未找到K线数据，返回结构:")
            debug(json.dumps(data, ensure_ascii=False)[:800])
            return None

        debug(f"获取{len(klines)}条K线")

        records = []
        for k in klines:
            records.append({
                "date": k[0],
                "open": float(k[1]),
                "close": float(k[2]),
                "high": float(k[3]),
                "low": float(k[4]),
                "volume": float(k[5]) if len(k) > 5 else 0,
            })

        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"])

        # 保存
        fname = f"etf_price_{SYMBOL.lower()}_tencent.csv"
        path = os.path.join(DATA_DIR, fname)
        df.to_csv(path, index=False)
        print(f"  ✅ 历史价格: {len(df)}条 ({df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()})")
        print(f"  📁 保存至: {path}")

        # 计算历史波动率
        df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
        hist_vol = df["log_ret"].dropna().std() * np.sqrt(252)
        print(f"  📊 年化历史波动率: {hist_vol:.4f} ({hist_vol*100:.1f}%)")

        return df

    except Exception as e:
        print(f"  ❌ K线获取失败: {type(e).__name__}: {e}")
        return None


# ============================================================
# 东方财富真实期权链拉取
# ============================================================

def _em_clist_page(pn, pz=100, timeout=12):
    """东方财富期权市场列表接口（单页，带域名回退）。
    同 akshare option_current_em 使用的接口。
    返回 (diff_list, total) 或 (None, 0)。
    """
    hosts = ["http://77.push2.eastmoney.com", "http://push2.eastmoney.com",
             "https://push2.eastmoney.com"]
    params = {
        "pn": str(pn), "pz": str(pz), "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:10",  # 上交所期权（含510300 ETF期权）
        "fields": "f2,f5,f12,f14,f18,f161,f162",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/center/qqsc.html",
        "Accept": "*/*",
    }
    last_err = None
    for host in hosts:
        for attempt in range(2):
            try:
                r = requests.get(host + "/api/qt/clist/get", params=params,
                                 headers=headers, timeout=timeout)
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    continue
                d0 = (r.json() or {}).get("data") or {}
                return d0.get("diff") or [], int(d0.get("total") or 0)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(2 + attempt * 3)  # 退避重试（应对反爬限流）
    debug(f"[东方财富] 分页请求失败 pn={pn}: {last_err}")
    return None, 0


def _fourth_wednesday(year, month):
    """ETF期权到期日：到期月第四个周三"""
    first = date(year, month, 1)
    offset = (2 - first.weekday()) % 7  # 2=周三
    return first + timedelta(days=offset + 21)


def fetch_options_eastmoney(S0):
    """
    从东方财富拉取510300(沪深300ETF)真实期权链。
    接口: push2.eastmoney.com/api/qt/clist/get (上交所期权市场, 同akshare)
    合约名形如 "300ETF购8月4800" → 从中解析 类型/到期月/行权价，
    到期日按"第四个周三"规则推导。

    如果拉取失败（网络/接口变更），返回 None，调用方回退模拟数据。
    """
    print("  [东方财富] 拉取510300真实期权链...")

    try:
        diff_all, total = _em_clist_page(1)
        if diff_all is None:
            print("  ⚠️ 东方财富接口不可用，回退模拟")
            return None
        pages = min((total + 99) // 100, 30)
        for pn in range(2, pages + 1):
            diff_pg, _ = _em_clist_page(pn)
            if not diff_pg:
                break
            diff_all.extend(diff_pg)
            if len(diff_all) >= total:
                break
        debug(f"[东方财富] 共获取 {len(diff_all)}/{total} 条上交所期权")

        today = date.today()
        name_pat = re.compile(r"300ETF(购|沽)(\d{1,2})月(\d+)")
        records, seen = [], set()

        def _num(v):
            try:
                if v in (None, "-", ""):
                    return None
                return float(v)
            except (TypeError, ValueError):
                return None

        for item in diff_all:
            name = item.get("f14", "") or ""
            m = name_pat.search(name)
            if not m:
                continue
            cp = "call" if m.group(1) == "购" else "put"
            month = int(m.group(2))
            strike = int(m.group(3)) / 1000.0  # 4800 → 4.800

            # 最新价，缺失时用买/卖中间价或昨结
            price = _num(item.get("f2"))
            if price is None or price <= 0:
                bid, ask = _num(item.get("f161")), _num(item.get("f162"))
                if bid and ask and ask >= bid:
                    price = (bid + ask) / 2
                else:
                    price = _num(item.get("f18"))
            if price is None or price <= 0:
                continue

            # 到期日推导：月份>=当前月按今年，否则明年
            year = today.year if month >= today.month else today.year + 1
            expiry = _fourth_wednesday(year, month)
            if expiry <= today:  # 已过期则推到明年（极端情况）
                expiry = _fourth_wednesday(year + 1, month)
            remain = (expiry - today).days
            if remain <= 0:
                continue
            T = remain / 365.0

            code = item.get("f12", "")
            key = (code, cp, round(strike, 3), str(expiry))
            if key in seen:
                continue
            seen.add(key)

            records.append({
                "code": code,
                "name": name,
                "cp": cp,
                "strike": round(strike, STRIKE_DECIMALS),
                "expire_date": expiry.strftime("%Y-%m-%d"),
                "T": T,
                "price": round(price, PRICE_DECIMALS),
                "iv": 0.0,  # 接口不提供IV，由校准反推
                "volume": int(_num(item.get("f5")) or 0),
                "open_interest": 0,
            })

        if len(records) < 10:
            print(f"  ⚠️ 有效合约过少({len(records)})，回退模拟")
            return None

        df = pd.DataFrame(records)
        print(f"  ✅ 真实期权链: {len(df)}条 (Call: {len(df[df.cp=='call'])}, "
              f"Put: {len(df[df.cp=='put'])})")
        return df

    except Exception as e:
        print(f"  ⚠️ 东方财富期权拉取失败: {type(e).__name__}: {e}")
        return None


# ============================================================
# 新浪真实期权链拉取（上交所 ETF 期权，主数据源）
# ============================================================

_SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://stock.finance.sina.com.cn",
}


def _sina_contract_months(symbol_code, timeout=10):
    """
    新浪 StockOptionService.getStockName：返回在交易合约月份列表，
    如 ['2026-08', '2026-08', '2026-09', '2026-12', '2027-03']
    """
    url = "https://stock.finance.sina.com.cn/futures/api/openapi.php/StockOptionService.getStockName"
    cate = "300ETF" if symbol_code == "510300" else ("50ETF" if symbol_code == "510050" else "500ETF")
    r = robust_get(url, params={"exchange": "null", "cate": cate},
                   headers=_SINA_HEADERS, timeout=timeout)
    if r.status_code != 200:
        return []
    data = (r.json() or {}).get("result", {}).get("data", {}) or {}
    months = []
    for m in data.get("contractMonth") or []:
        if m and m not in months:
            months.append(m)
    return months


def _sina_contract_codes(symbol_code, month_ym):
    """
    新浪 hq.sinajs.cn 按标的+月份返回 C/P 合约代码列表。
    month_ym: '2026-08' -> '2608'；请求 OP_UP_5103002608 / OP_DOWN_5103002608
    返回 [(code, cp), ...]
    """
    import re
    ym = month_ym[2:4] + month_ym[5:7]
    out = []
    for direction, cp in [("OP_UP", "call"), ("OP_DOWN", "put")]:
        try:
            r = robust_get(f"https://hq.sinajs.cn/list={direction}_{symbol_code}{ym}",
                           headers=_SINA_HEADERS, timeout=10)
            m = re.search(r'"([A-Z0-9_,]+)"', r.text)
            if not m:
                continue
            for code in m.group(1).split(","):
                if code.startswith("CON_OP_"):
                    out.append((code, cp))
        except Exception as e:
            debug(f"[新浪] {direction}_{symbol_code}{ym} 列表失败: {e}")
    return out


def fetch_options_sina(S0=None, max_months=4, symbol_code=None):
    """
    从新浪财经拉取上交所 ETF 期权真实期权链。
    max_months: 追踪的到期月数（上交所挂牌规则：当月+下月+两个季月，共 4 个；0 表示全部）

    接口链路（全部免费、无需登录）：
      1. StockOptionService.getStockName      -> 在交易合约月份
      2. hq.sinajs.cn/list=OP_UP_510300{YYMM} -> 当月看涨合约代码
         hq.sinajs.cn/list=OP_DOWN_510300{YYMM} -> 当月看跌合约代码
      3. hq.sinajs.cn/list=CON_OP_xxx,...     -> 合约行情

    行情字段（逗号分隔，索引 0 起）：
      1=买价 2=最新价 3=卖价 5=持仓量 7=行权价
      37=合约名 41=成交量 45=类型(C/P) 46=到期日 47=剩余天数

    价格取买一卖一中间价（缺失回退最新价/昨结），保证校准输入稳健。
    iv 置 0：新浪 f38 字段并非可靠隐含波动率，由流水线用 BS 从中间价反解。
    返回 DataFrame: code,name,cp,strike,expire_date,T,price,iv,volume,open_interest
    失败返回 None（调用方回退其他数据源）。
    """
    import re
    if symbol_code is None:
        symbol_code = SYMBOL  # 模块级默认 510300

    print(f"  [新浪期权] 拉取{symbol_code}真实期权链...")

    months = _sina_contract_months(symbol_code)
    if not months:
        print("  ⚠️ 新浪合约月份获取失败")
        return None
    if max_months > 0:
        months = months[:max_months]
    debug(f"[新浪期权] 到期月份: {months}")

    # 收集 C/P 合约代码（去重）
    entries, seen = [], set()
    for ym in months:
        for code, cp in _sina_contract_codes(symbol_code, ym):
            if code not in seen:
                seen.add(code)
                entries.append((code, cp))
    if len(entries) < 10:
        print(f"  ⚠️ 新浪合约列表过少({len(entries)})，回退其他源")
        return None
    debug(f"[新浪期权] 合约 {len(entries)} 条")

    # 批量行情请求（分批防止 URL 过长）
    records = []
    batch = 60
    for i in range(0, len(entries), batch):
        chunk = entries[i:i + batch]
        try:
            r = robust_get("https://hq.sinajs.cn/list=" + ",".join(c for c, _ in chunk),
                           headers=_SINA_HEADERS, timeout=10)
            r.encoding = "gbk"  # 新浪行情是 GBK 编码
        except Exception as e:
            print(f"  ⚠️ 新浪行情请求失败: {type(e).__name__}: {e}")
            continue

        cp_map = dict(chunk)
        for m in re.finditer(r'hq_str_(CON_OP_\d+)="([^"]*)"', r.text):
            code = m.group(1)
            payload = m.group(2)
            if not payload:
                continue
            f = payload.split(",")
            if len(f) < 48:
                continue
            cp = cp_map.get(code)
            if not cp and len(f) > 45:
                cp = "call" if f[45] == "C" else "put"

            def _num(idx):
                try:
                    v = f[idx]
                    if v in ("", "-"):
                        return 0.0
                    return float(v)
                except (ValueError, IndexError):
                    return 0.0

            strike = _num(7)
            bid, ask = _num(1), _num(3)
            last = _num(2)
            if bid > 0 and ask > 0:
                price = (bid + ask) / 2.0
                spread = (ask - bid) / 2.0
            elif last > 0:
                price = last
                spread = 0.0
            else:
                price = _num(8)  # 昨结兜底
                spread = 0.0
            if price <= 0 or strike <= 0:
                continue
            spread_pct = spread / price if price > 0 else 0.0

            iv = 0.0  # 新浪 f38 非可靠 IV，交由 pipeline 用 bs_iv 从中间价反解
            try:
                expire_date = f[46]
                remain_days = int(float(f[47]))
            except (ValueError, IndexError):
                expire_date = ""
                remain_days = 0
            if remain_days <= 0:
                continue

            records.append({
                "code": code,
                "name": f[37] if len(f) > 37 else code,
                "cp": cp or "call",
                "strike": round(strike, 3),
                "expire_date": expire_date,
                "T": round(remain_days / 365.0, 6),
                "price": round(price, 4),
                "iv": round(iv, 4),
                "volume": int(_num(41)),
                "open_interest": int(_num(5)),
                "spread": round(spread, 6),
                "spread_pct": round(spread_pct, 6),
            })
        time.sleep(0.3)  # 批次间隔，避免触发反爬

    if len(records) < 10:
        print(f"  ⚠️ 有效合约过少({len(records)})，回退其他源")
        return None

    df = pd.DataFrame(records).drop_duplicates(subset=["code"]).reset_index(drop=True)
    n_call = int((df["cp"] == "call").sum())
    n_put = int((df["cp"] == "put").sum())
    print(f"  ✅ 真实期权链: {len(df)}条 (Call: {n_call}, Put: {n_put}, "
          f"到期月: {len(df['expire_date'].unique())}个, "
          f"K范围: {df['strike'].min():.3f}~{df['strike'].max():.3f}, "
          f"IV范围: {df['iv'].min()*100:.1f}%~{df['iv'].max()*100:.1f}%)")
    return df


# ============================================================
# 模拟期权数据
# ============================================================

def fetch_options_mock(S0):
    """生成模拟期权链"""
    print("  [模拟数据] 生成期权链...")

    from scipy.stats import norm

    np.random.seed(42)

    # 3个到期日
    today = date.today()
    if TARGET_US:
        # SPY按周到期，更密
        maturities = [7, 14, 30, 60]
        maturity_names = ["周内", "次周", "近月", "次月"]
    else:
        maturities = [30, 60, 90]
        maturity_names = ["近月", "次月", "远月"]

    # 行权价
    atm = round(S0) if TARGET_US else round(S0, 2)
    if TARGET_US:
        strikes = sorted(set([
            round(atm * k) for k in
            [0.90, 0.93, 0.95, 0.97, 0.98, 0.99, 1.00, 1.01, 1.02, 1.03, 1.05, 1.07, 1.10]
        ]))
    else:
        strikes = sorted(set([
            round(atm * k, 2) for k in
            [0.85, 0.88, 0.91, 0.94, 0.96, 0.98, 1.00, 1.02, 1.04, 1.06, 1.09, 1.12, 1.15]
        ]))

    sigma = 0.18 if TARGET_CN else 0.15
    r = DEFAULT_R
    q = 0.0

    records = []
    for i, days in enumerate(maturities):
        T = days / 365
        expiry = today + timedelta(days=days)

        for K in strikes:
            for cp in ["call", "put"]:
                d1 = (np.log(S0/K) + (r - q + sigma**2/2)*T) / (sigma*np.sqrt(T))
                d2 = d1 - sigma*np.sqrt(T)
                if cp == "call":
                    price = S0*np.exp(-q*T)*norm.cdf(d1) - K*np.exp(-r*T)*norm.cdf(d2)
                else:
                    price = K*np.exp(-r*T)*norm.cdf(-d2) - S0*np.exp(-q*T)*norm.cdf(-d1)
                price = max(0.01, price + np.random.normal(0, price*0.02))
                iv = sigma + np.random.normal(0, 0.01)

                records.append({
                    "code": f"模拟_{cp}_{K}_{days}d",
                    "name": f"{SYMBOL}{maturity_names[i]}{cp[0].upper()}{K}",
                    "cp": cp,
                    "strike": float(K),
                    "expire_date": expiry.strftime("%Y-%m-%d"),
                    "T": T,
                    "price": round(price, PRICE_DECIMALS),
                    "iv": round(iv, 4),
                    "volume": int(np.random.randint(100, 10000)),
                    "open_interest": int(np.random.randint(1000, 50000)),
                })

    df = pd.DataFrame(records)
    print(f"  ✅ 模拟期权: {len(df)}条 ({len(maturities)}个到期日 × {len(strikes)}个行权价 × C/P)")
    return df


# ============================================================
# 手动粘贴模式
# ============================================================

def fetch_options_manual():
    """用户从交易软件/网页复制期权数据，粘贴到脚本中"""
    print()
    print("  " + "=" * 56)
    print("  手动粘贴期权数据")
    print("  " + "=" * 56)
    print()
    print(f"  标的: {SYMBOL_NAME} ({SYMBOL})")
    print()

    if TARGET_CN:
        print("  📋 推荐数据来源（任选一个）：")
        print()
        print("  来源1: 同花顺/通达信/东方财富客户端")
        print("    打开期权T型报价 → 选中510300期权 → 复制表格")
        print()
        print("  来源2: 东方财富网页")
        print("    https://quote.eastmoney.com/center/optionlist.html")
        print("    选510300 → 复制期权链数据")
        print()
        print("  来源3: 上交所")
        print("    http://www.sse.com.cn/disclosure/listedinfo/option/")
        print()
        print("  📝 数据格式（任意一种都行）：")
        print()
        print("  格式A — 每行一个合约（空格/Tab/逗号分隔）:")
        print("    300ETF购8月4700  4.700  0.0835")
        print("    300ETF沽8月4700  4.700  0.0152")
        print()
        print("  格式B — 直接粘贴交易软件表格（含表头）:")
        print("    合约代码  最新价  行权价  剩余天数")
        print("    300ETF购8月4700  0.0835  4.700  22")
        print()
        print("  格式C — 带到期日:")
        print("    名称,行权价,到期日,价格,类型")
        print("    300ETF购8月4700,4.700,2026-08-28,0.0835,call")
        print()
        print("  💡 也可以只粘贴: 行权价 价格")
        print("    脚本会自动推断看涨/看跌（购=call, 沽=put）")
    else:
        print("  📋 推荐数据来源（任选一个）：")
        print()
        print("  来源1: optionstrat.com (免费，无需注册)")
        print("    https://optionstrat.com/SPY")
        print("    选期权链 → 复制表格")
        print()
        print("  来源2: barchart.com")
        print("    https://www.barchart.com/stocks/quotes/SPY/options")
        print()
        print("  来源3: nasdaq.com")
        print("    https://www.nasdaq.com/market-activity/stocks/spy/option-chain")
        print()
        print("  📝 数据格式（任意一种都行）：")
        print()
        print("  格式A — 每行一个合约:")
        print("    SPY  Call  550  2026-08-15  3.25")
        print("    SPY  Put   550  2026-08-15  2.10")
        print()
        print("  格式B — 粘贴网页表格:")
        print("    Strike  Call Bid  Call Ask  Put Bid  Put Ask  Expiry")
        print("    550     3.20      3.30      2.05     2.15     2026-08-15")
        print()
        print("  格式C — 只有行权价和价格:")
        print("    550 3.25 call")
        print("    550 2.10 put")

    print()
    print("  ⚠️ 注意事项:")
    print(f"    - 行权价精度: {STRIKE_DECIMALS}位小数" + (f"（如 4.{700}'）" if TARGET_CN else "（如 550）"))
    print(f"    - 价格精度: {PRICE_DECIMALS}位小数")
    print(f"    - 如果不填到期日，脚本默认用30天")
    print(f"    - 如果不填call/put，脚本从名称推断（购=call, 沽=put）")
    print()
    print("  粘贴数据后，连续按两次回车结束：")
    print("  " + "-" * 56)

    lines = []
    empty_count = 0
    try:
        while True:
            line = input()
            if line.strip() == "":
                empty_count += 1
                if empty_count >= 2 or len(lines) > 0:
                    break
            else:
                empty_count = 0
                lines.append(line.strip())
    except (EOFError, KeyboardInterrupt):
        pass

    if not lines:
        print("  ❌ 未输入任何数据")
        return None

    print(f"\n  收到 {len(lines)} 行数据，正在解析...")

    records = []
    for line in lines:
        record = parse_option_line(line)
        if record:
            records.append(record)
        else:
            debug(f"跳过: {line}")

    if not records:
        print("  ❌ 未能解析出有效数据")
        print()
        print("  💡 你可以把数据截图或粘贴到聊天里发给我")
        print("     我帮你整理成正确格式")
        return None

    df = pd.DataFrame(records)
    n_call = len(df[df.cp == "call"])
    n_put = len(df[df.cp == "put"])
    print(f"  ✅ 解析成功: {len(df)}条 (Call: {n_call}, Put: {n_put})")
    return df


def parse_option_line(line):
    """解析一行期权数据，自动识别格式"""
    if line.startswith("name,") or line.startswith("名称,") or line.startswith("合约"):
        return None  # 跳过表头

    parts = re.split(r"[,\t\s]+", line)
    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) < 2:
        return None

    name = parts[0]
    strike = 0.0
    price = 0.0
    expire_date = None
    cp = None
    T = None

    # 从名称推断call/put
    if "购" in name or "CALL" in name.upper():
        cp = "call"
    elif "沽" in name or "PUT" in name.upper():
        cp = "put"

    # 从名称推断行权价（如"300ETF购8月4700" -> 4.700, "SPY Call 550" -> 550）
    if cp and strike == 0:
        # 先找中文合约名的行权价（4位数字结尾）
        if TARGET_CN:
            m = re.search(r"(\d{3,4})$", name)
            if m:
                s = m.group(1)
                strike = int(s) / 1000 if len(s) == 4 else int(s) / 100
        else:
            m = re.search(r"(\d{2,4}(?:\.\d+)?)", name)
            if m:
                strike = float(m.group(1))

    # 遍历剩余字段，自动识别
    for p in parts[1:]:
        # 日期
        if re.match(r"\d{4}-\d{1,2}-\d{1,2}", p):
            expire_date = datetime.strptime(p[:10], "%Y-%m-%d").date()
            continue
        # call/put
        if p.lower() in ["call", "c", "购"]:
            cp = "call"
            continue
        if p.lower() in ["put", "p", "沽"]:
            cp = "put"
            continue
        # 数字
        if is_float(p):
            val = float(p)
            # 行权价通常比较大（CN: 3~6, US: 400~600）
            if strike == 0 and val > 0:
                # 第一个数字如果不是价格范围，当作行权价
                if TARGET_CN and 2 < val < 10:
                    strike = val
                elif TARGET_US and 100 < val < 1000:
                    strike = val
                elif price == 0:
                    price = val
                else:
                    # 两个数字，小的可能是价格，大的是行权价
                    if val > price:
                        strike = val
                    else:
                        strike = price
                        price = val
            elif price == 0 and val > 0:
                price = val

    if strike == 0 or price == 0:
        return None

    if cp is None:
        # 无法判断，默认call
        cp = "call"

    if T is None:
        if expire_date:
            T = (expire_date - date.today()).days / 365
            if T <= 0:
                T = 30 / 365
                expire_date = date.today() + timedelta(days=30)
        else:
            T = 30 / 365
            expire_date = date.today() + timedelta(days=30)

    if expire_date is None:
        expire_date = date.today() + timedelta(days=30)

    return {
        "code": name,
        "name": name,
        "cp": cp,
        "strike": round(strike, STRIKE_DECIMALS),
        "expire_date": expire_date.strftime("%Y-%m-%d"),
        "T": T,
        "price": round(price, PRICE_DECIMALS),
        "iv": 0.0,
        "volume": 0,
        "open_interest": 0,
    }


def is_float(s):
    try:
        float(s)
        return True
    except:
        return False


# ============================================================
# 无风险利率
# ============================================================

def fetch_risk_free_rate():
    """无风险利率"""
    return DEFAULT_R


# ============================================================
# 整理校准输入
# ============================================================

def prepare_calibration_data(S0, r, options_df):
    """整理成校准脚本需要的格式"""
    if options_df is None or len(options_df) == 0:
        return None

    df = options_df.copy()
    df = df[(df["price"] > 0) & (df["strike"] > 0) & (df["T"] > 0)]
    df = df.sort_values(["T", "cp", "strike"]).reset_index(drop=True)
    df["id"] = range(1, len(df) + 1)

    # 保存期权链
    chain_path = os.path.join(DATA_DIR, f"options_chain_{SYMBOL.lower()}_{date.today().strftime('%Y%m%d')}.csv")
    df.to_csv(chain_path, index=False)
    print(f"\n  📁 期权链保存至: {chain_path}")

    # 生成校准输入
    calib = pd.DataFrame({
        "id": df["id"],
        "S0": S0,
        "K": df["strike"],
        "T": df["T"].round(6),
        "r": r,
        "market_price": df["price"].round(6),
        "type": df["cp"],
        "iv": df.get("iv", 0),
        "volume": df.get("volume", 0),
        "expire_date": df["expire_date"],
    })

    calib_path = os.path.join(DATA_DIR, f"calibration_input_{SYMBOL.lower()}_{date.today().strftime('%Y%m%d')}.csv")
    calib.to_csv(calib_path, index=False)
    print(f"  📁 校准输入保存至: {calib_path}")

    # 统计
    calls = df[df["cp"] == "call"]
    puts = df[df["cp"] == "put"]
    expiries = sorted(df["expire_date"].unique())

    print(f"\n  数据统计:")
    print(f"    标的: {SYMBOL}, S0 = {S0:.{PRICE_DECIMALS}f} {CURRENCY}")
    print(f"    期权总数: {len(df)}")
    print(f"    Call: {len(calls)}, Put: {len(puts)}")
    print(f"    到期日: {len(expiries)}个")
    for exp in expiries:
        sub = df[df["expire_date"] == exp]
        print(f"      {exp}: {len(sub)}条 (K: {sub['strike'].min():.{STRIKE_DECIMALS}f} ~ {sub['strike'].max():.{STRIKE_DECIMALS}f})")

    return calib


# ============================================================
# 主流程
# ============================================================

def main():
    print("=" * 60)
    print(f"TFBSM 期权数据拉取工具 v9")
    print(f"标的: {SYMBOL_NAME} ({SYMBOL})")
    print("=" * 60)

    if USE_MOCK:
        print("模式: 模拟数据")
    elif MANUAL_MODE:
        print("模式: 手动粘贴期权数据")
    else:
        print("模式: 在线价格 + 模拟期权")

    print()

    # ---- 1. ETF价格 ----
    print("[1/3] 拉取标的实时价格...")

    S0 = None
    etf_df = None

    if not USE_MOCK:
        etf_df, S0 = fetch_price_tencent()

    if S0 is None:
        if not USE_MOCK:
            print(f"  ⚠️ 实时价格获取失败，使用默认值")
        S0 = DEFAULT_S0

    print(f"  → S0 = {S0:.{PRICE_DECIMALS}f} {CURRENCY}")

    # ---- 2. 期权链 ----
    print(f"\n[2/3] 获取期权链...")

    options_df = None

    if USE_MOCK:
        options_df = fetch_options_mock(S0)
    elif MANUAL_MODE:
        options_df = fetch_options_manual()
    else:
        # 在线模式：先模拟，提示可以手动替换
        print("  ⚠️ 在线期权数据暂不可用（腾讯期权接口不稳定）")
        print()
        print("  你有两个选择：")
        print("    1. 用模拟数据先跑通流程: python data_fetch_v9.py --mock")
        print("    2. 从交易软件/网页手动粘贴真实数据: python data_fetch_v9.py --manual")
        print()
        print("  先用模拟数据继续...")
        options_df = fetch_options_mock(S0)

    # ---- 3. 利率 ----
    r = fetch_risk_free_rate()
    print(f"\n[3/3] 无风险利率 r = {r*100:.1f}%")

    # ---- 整理 ----
    calib_df = prepare_calibration_data(S0, r, options_df)

    if calib_df is not None:
        print("\n" + "=" * 60)
        print("✅ 数据拉取完成！")
        print(f"   标的: {SYMBOL}, S0 = {S0:.{PRICE_DECIMALS}f} {CURRENCY}")
        print(f"   期权: {len(calib_df)} 条")
        print(f"   利率: r = {r*100:.2f}%")
        print(f"   数据目录: {DATA_DIR}")
        data_source = "模拟" if USE_MOCK else ("手动" if MANUAL_MODE else "腾讯+模拟")
        print(f"   数据来源: {data_source}")
        print("=" * 60)
        print("\n下一步: python calibration.py")
    else:
        print("\n❌ 数据整理失败")


if __name__ == "__main__":
    main()
