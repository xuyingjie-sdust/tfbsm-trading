#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络诊断脚本：找出为什么拉不到数据
运行：python diagnose_network.py
"""

import sys
import ssl

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

print("=" * 60)
print("网络诊断")
print("=" * 60)

# ============================================================
# 1. 基本网络连通性测试
# ============================================================

print("\n[1] 测试基本网络连通性...\n")

import socket

test_sites = [
    ("push2his.eastmoney.com", 443, "东方财富数据API"),
    ("push2.eastmoney.com", 443, "东方财富实时API"),
    ("datacenter-web.eastmoney.com", 443, "东方财富数据中心"),
    ("hq.sinajs.cn", 443, "新浪行情"),
    ("qt.gtimg.cn", 443, "腾讯行情"),
    ("www.baidu.com", 443, "百度（对照组）"),
]

for host, port, name in test_sites:
    try:
        sock = socket.create_connection((host, port), timeout=5)
        sock.close()
        print(f"  ✅ {name:15s} {host}:{port} 连通")
    except Exception as e:
        print(f"  ❌ {name:15s} {host}:{port} 失败: {e}")

# ============================================================
# 2. 测试requests能否正常访问
# ============================================================

print("\n[2] 测试requests库访问...\n")

import requests

test_urls = [
    ("https://www.baidu.com", "百度（对照组）"),
    ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.510300&fields1=f1&fields2=f51,f52,f53&klt=101&fqt=1&beg=20250101&end=20250601", "东方财富K线"),
    ("https://qt.gtimg.cn/q=sh510300", "腾讯行情"),
]

for url, name in test_urls:
    try:
        resp = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        status = resp.status_code
        length = len(resp.text)
        print(f"  ✅ {name:15s} 状态码={status} 返回{length}字符")
        if length < 500:
            print(f"     内容: {resp.text[:200]}")
    except Exception as e:
        print(f"  ❌ {name:15s} 失败: {type(e).__name__}: {e}")

# ============================================================
# 3. 测试SSL证书
# ============================================================

print("\n[3] 测试SSL证书...\n")

import urllib.request

try:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.510300&fields1=f1&fields2=f51,f52,f53&klt=101&fqt=1&beg=20250101&end=20250601",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp = urllib.request.urlopen(req, timeout=10, context=ctx)
    data = resp.read().decode()
    print(f"  ✅ urllib (跳过SSL验证) 成功，返回{len(data)}字符")
except Exception as e:
    print(f"  ❌ urllib (跳过SSL验证) 失败: {type(e).__name__}: {e}")

# ============================================================
# 4. 测试requests跳过SSL验证
# ============================================================

print("\n[4] 测试requests跳过SSL验证...\n")

try:
    resp = requests.get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={
            "secid": "1.510300",
            "fields1": "f1",
            "fields2": "f51,f52,f53",
            "klt": "101",
            "fqt": "1",
            "beg": "20250101",
            "end": "20250601",
        },
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        },
        timeout=10,
        verify=False,  # 跳过SSL验证
    )
    print(f"  ✅ requests (verify=False) 状态码={resp.status_code} 返回{len(resp.text)}字符")
    if len(resp.text) < 500:
        print(f"     内容: {resp.text[:200]}")
except Exception as e:
    print(f"  ❌ requests (verify=False) 失败: {type(e).__name__}: {e}")

# ============================================================
# 5. 检查系统代理设置
# ============================================================

print("\n[5] 检查系统代理...\n")

import os

proxy_vars = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"]

found_proxy = False
for var in proxy_vars:
    val = os.environ.get(var)
    if val:
        print(f"  ⚠️ {var} = {val}")
        found_proxy = True

if not found_proxy:
    print("  未检测到环境变量代理设置")

# Windows系统代理
try:
    import winreg
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
    )
    proxy_enable = winreg.QueryValueEx(key, "ProxyEnable")[0]
    if proxy_enable:
        proxy_server = winreg.QueryValueEx(key, "ProxyServer")[0]
        print(f"  ⚠️ Windows系统代理已开启: {proxy_server}")
    else:
        print("  Windows系统代理未开启")
    winreg.CloseKey(key)
except Exception:
    print("  (无法读取Windows代理设置，可能不是Windows)")

# ============================================================
# 6. 测试腾讯行情接口（另一个数据源）
# ============================================================

print("\n[6] 测试腾讯行情接口...\n")

try:
    resp = requests.get(
        "https://qt.gtimg.cn/q=sh510300",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    print(f"  ✅ 腾讯行情 状态码={resp.status_code}")
    print(f"     内容: {resp.text[:200]}")
except Exception as e:
    print(f"  ❌ 腾讯行情 失败: {type(e).__name__}: {e}")

# ============================================================
# 7. 测试curl_cffi（模拟浏览器TLS指纹）
# ============================================================

print("\n[7] 测试curl_cffi（浏览器TLS指纹模拟）...\n")

try:
    from curl_cffi import requests as cffi_requests

    resp = cffi_requests.get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={
            "secid": "1.510300",
            "fields1": "f1",
            "fields2": "f51,f52,f53",
            "klt": "101",
            "fqt": "1",
            "beg": "20250101",
            "end": "20250601",
        },
        impersonate="chrome",
        timeout=10,
    )
    print(f"  ✅ curl_cffi (Chrome指纹) 状态码={resp.status_code} 返回{len(resp.text)}字符")
    if len(resp.text) < 500:
        print(f"     内容: {resp.text[:200]}")
except ImportError:
    print("  ⚠️ curl_cffi未安装（akshare自带，应该已装上）")
    print("     如果akshare已安装，curl_cffi应该也存在")
except Exception as e:
    print(f"  ❌ curl_cffi 失败: {type(e).__name__}: {e}")

# ============================================================
# 总结
# ============================================================

print("\n" + "=" * 60)
print("诊断完成。请把以上全部输出截图发给我。")
print("=" * 60)
