#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合并两个到期日的510300期权数据 → 生成双到期日校准输入文件
到期日1: 2026-08-26 (20天)  — 近月
到期日2: 2026-09-23 (48天)  — 远月（9月第四个周三）
"""

import os
import pandas as pd
import numpy as np
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

S0 = 4.6930
r = 0.02

# ============================================================
# 到期日1: 2026-08-26 (20天)
# ============================================================
T1_DAYS = 20
T1 = T1_DAYS / 365.0
EXPIRY1 = "2026-08-26"

strikes_1 = [4.20, 4.30, 4.40, 4.50, 4.60, 4.70, 4.80, 4.90, 5.00, 5.25, 5.50, 5.75, 6.00]

# 看涨（购）: bid, ask, last, volume, open_interest
calls_1 = [
    (0.4924, 0.4948, 0.4958, 3,   660),
    (0.3954, 0.3983, 0.3955, 476, 2061),
    (0.3013, 0.3035, 0.3002, 24,  4549),
    (0.2136, 0.2142, 0.2139, 16,  12313),
    (0.1369, 0.1372, 0.1375, 2,   38069),
    (0.0778, 0.0780, 0.0781, 19,  56960),
    (0.0390, 0.0393, 0.0392, 30,  72254),
    (0.0179, 0.0181, 0.0180, 10,  60991),
    (0.0085, 0.0086, 0.0085, 74,  51211),
    (0.0022, 0.0023, 0.0022, 62,  33139),
    (0.0016, 0.0017, 0.0016, 214, 10787),
    (0.0007, 0.0008, 0.0007, 75,  10621),
    (0.0005, 0.0006, 0.0005, 0,   5911),
]

# 看跌（沽）: bid, ask, last, volume, open_interest
puts_1 = [
    (0.0032, 0.0034, 0.0032, 88,  19469),
    (0.0065, 0.0066, 0.0065, 3,   22729),
    (0.0118, 0.0120, 0.0120, 12,  24066),
    (0.0235, 0.0238, 0.0239, 105, 31894),
    (0.0466, 0.0467, 0.0467, 36,  51177),
    (0.0875, 0.0877, 0.0872, 20,  52673),
    (0.1479, 0.1486, 0.1480, 10,  32171),
    (0.2262, 0.2278, 0.2297, 30,  10810),
    (0.3167, 0.3179, 0.3183, 22,  6789),
    (0.5587, 0.5616, 0.5618, 31,  2121),
    (0.8080, 0.8110, 0.7956, 21,  728),
    (1.0577, 1.0611, 1.0451, 1,   1971),
    (1.3075, 1.3110, 1.2956, 1,   3940),
]

# ============================================================
# 到期日2: 2026-09-23 (48天)
# ============================================================
T2_DAYS = 48
T2 = T2_DAYS / 365.0
EXPIRY2 = "2026-09-23"

strikes_2 = [4.00, 4.10, 4.20, 4.30, 4.40, 4.50, 4.60, 4.70, 4.80, 4.90, 5.00, 5.25, 5.50, 5.75, 6.00]

# 看涨（购）: bid, ask, last, volume, open_interest
calls_2 = [
    (0.6500, 0.7077, 0.6711, 2,  1503),
    (0.5666, 0.6499, 0.5888, 1,  373),
    (0.4700, 0.5088, 0.4991, 3,  2511),
    (0.3900, 0.4300, 0.4000, 1,  1937),
    (0.2985, 0.3400, 0.3076, 1,  3042),
    (0.2300, 0.2388, 0.2310, 1,  4771),
    (0.1661, 0.1766, 0.1652, 3,  7638),
    (0.1148, 0.1220, 0.1178, 2,  13345),
    (0.0779, 0.0785, 0.0777, 6,  18026),
    (0.0493, 0.0540, 0.0512, 3,  26078),
    (0.0310, 0.0348, 0.0319, 56, 41641),
    (0.0115, 0.0129, 0.0117, 1,  36535),
    (0.0050, 0.0066, 0.0058, 13, 20411),
    (0.0043, 0.0044, 0.0043, 95, 24790),
    (0.0037, 0.0048, 0.0037, 19, 36420),
]

# 看跌（沽）: bid, ask, last, volume, open_interest
puts_2 = [
    (0.0057, 0.0061, 0.0058, 10, 33575),
    (0.0090, 0.0094, 0.0091, 1,  30332),
    (0.0138, 0.0151, 0.0149, 55, 19298),
    (0.0228, 0.0250, 0.0245, 13, 21727),
    (0.0377, 0.0401, 0.0370, 1,  15942),
    (0.0595, 0.0628, 0.0617, 1,  17593),
    (0.0945, 0.0999, 0.0967, 1,  14686),
    (0.1380, 0.1480, 0.1452, 3,  11587),
    (0.2009, 0.2056, 0.2068, 10, 7492),
    (0.2585, 0.2836, 0.2816, 1,  6573),
    (0.3031, 0.3700, 0.3591, 10, 9339),
    (0.5001, 0.8195, 0.5599, 10, 7442),
    (0.5932, 1.0645, 0.8322, 10, 4954),
    (0.8408, 1.3121, 1.0683, 10, 1112),
    (1.2888, 1.5610, 1.3111, 1,  1190),
]

# ============================================================
# 组装DataFrame
# ============================================================
rows = []

def add_rows(strikes, calls, puts, T, T_days, expiry, label):
    for i, K in enumerate(strikes):
        bid, ask, last, vol, oi = calls[i]
        mid = (bid + ask) / 2
        spread = ask - bid
        spread_pct = spread / mid if mid > 0 else 0
        liq = "good" if oi >= 500 and spread_pct < 0.15 else "low"
        rows.append({
            "type": "call", "strike": K, "bid": bid, "ask": ask, "mid": mid,
            "last": last, "volume": vol, "open_interest": oi,
            "T": T, "T_days": T_days, "S0": S0, "r": r,
            "expiry": expiry, "expiry_label": label,
            "spread": spread, "spread_pct": spread_pct, "liquidity": liq,
        })
    for i, K in enumerate(strikes):
        bid, ask, last, vol, oi = puts[i]
        mid = (bid + ask) / 2
        spread = ask - bid
        spread_pct = spread / mid if mid > 0 else 0
        liq = "good" if oi >= 500 and spread_pct < 0.15 else "low"
        rows.append({
            "type": "put", "strike": K, "bid": bid, "ask": ask, "mid": mid,
            "last": last, "volume": vol, "open_interest": oi,
            "T": T, "T_days": T_days, "S0": S0, "r": r,
            "expiry": expiry, "expiry_label": label,
            "spread": spread, "spread_pct": spread_pct, "liquidity": liq,
        })

add_rows(strikes_1, calls_1, puts_1, T1, T1_DAYS, EXPIRY1, "近月(20天)")
add_rows(strikes_2, calls_2, puts_2, T2, T2_DAYS, EXPIRY2, "远月(53天)")

df = pd.DataFrame(rows)

# 保存
filename = f"calibration_input_20260806_dual.csv"
filepath = os.path.join(DATA_DIR, filename)
df.to_csv(filepath, index=False, encoding="utf-8-sig")

print("=" * 60)
print("✅ 双到期日校准数据生成完成！")
print("=" * 60)
print(f"  标的: 510300, S0 = {S0}")
print(f"  到期日1: {EXPIRY1} ({T1_DAYS}天) — {len(strikes_1)}×2 = {len(strikes_1)*2}条")
print(f"  到期日2: {EXPIRY2} ({T2_DAYS}天) — {len(strikes_2)}×2 = {len(strikes_2)*2}条")
print(f"  合计: {len(df)} 条")
print(f"  利率: r = {r*100:.1f}%")
print(f"  文件: {filepath}")

# 打印摘要
print("\n--- 近月 (20天) ---")
sub1 = df[df["expiry"] == EXPIRY1]
for t in ["call", "put"]:
    sub = sub1[sub1["type"] == t]
    print(f"  {t:4s}: {len(sub)}条, K范围 {sub['strike'].min():.2f}~{sub['strike'].max():.2f}, "
          f"价格范围 {sub['mid'].min():.4f}~{sub['mid'].max():.4f}")

print("\n--- 远月 (53天) ---")
sub2 = df[df["expiry"] == EXPIRY2]
for t in ["call", "put"]:
    sub = sub2[sub2["type"] == t]
    print(f"  {t:4s}: {len(sub)}条, K范围 {sub['strike'].min():.2f}~{sub['strike'].max():.2f}, "
          f"价格范围 {sub['mid'].min():.4f}~{sub['mid'].max():.4f}")

# 低流动性标记
low_liq = df[df["liquidity"] == "low"]
if len(low_liq) > 0:
    print(f"\n⚠️ 低流动性合约({len(low_liq)}条):")
    for _, row in low_liq.iterrows():
        print(f"  {row['type']:4s} K={row['strike']:.2f} {row['expiry_label']}: "
              f"OI={row['open_interest']:.0f}, 价差={row['spread_pct']*100:.1f}%")
