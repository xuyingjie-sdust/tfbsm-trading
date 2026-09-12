#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从用户粘贴的T型报价数据生成校准输入CSV
数据来源：东方财富客户端 510300期权 2026-08-26到期
"""

import os
import pandas as pd
import numpy as np
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ============================================================
# 用户粘贴的数据（T型报价）
# ============================================================

S0 = 4.6930
T_DAYS = 20  # 剩余天数
T = T_DAYS / 365.0
r = 0.02  # 无风险利率2%

# 行权价
strikes = [4.20, 4.30, 4.40, 4.50, 4.60, 4.70, 4.80, 4.90, 5.00, 5.25, 5.50, 5.75, 6.00]

# 看涨期权（购）：买量, 买价, 最新价, 卖价, 卖量, 持仓量
calls_data = [
    # (bid, ask, last, volume, open_interest)
    (0.4924, 0.4948, 0.4958, 3,    660),    # K=4.20
    (0.3954, 0.3983, 0.3955, 476,  2061),   # K=4.30
    (0.3013, 0.3035, 0.3002, 24,   4549),   # K=4.40
    (0.2136, 0.2142, 0.2139, 16,   12313),  # K=4.50
    (0.1369, 0.1372, 0.1375, 2,    38069),  # K=4.60
    (0.0778, 0.0780, 0.0781, 19,   56960),  # K=4.70
    (0.0390, 0.0393, 0.0392, 30,   72254),  # K=4.80
    (0.0179, 0.0181, 0.0181, 27,   60991),  # K=4.90
    (0.0085, 0.0086, 0.0085, 10,   51221),  # K=5.00
    (0.0022, 0.0023, 0.0022, 74,   33139),  # K=5.25
    (0.0016, 0.0017, 0.0016, 62,   19470),  # K=5.50
    (0.0011, 0.0012, 0.0011, 214,  10787),  # K=5.75
    (0.0007, 0.0008, 0.0007, 75,   10621),  # K=6.00
]

# 看跌期权（沽）：买量, 买价, 最新价, 卖价, 卖量, 持仓量
puts_data = [
    # (bid, ask, last, volume, open_interest)
    (0.0032, 0.0034, 0.0032, 88,   19469),  # K=4.20
    (0.0065, 0.0066, 0.0065, 3,    22729),  # K=4.30
    (0.0118, 0.0120, 0.0120, 12,   24066),  # K=4.40
    (0.0235, 0.0238, 0.0239, 105,  31894),  # K=4.50
    (0.0466, 0.0467, 0.0467, 36,   51177),  # K=4.60
    (0.0875, 0.0877, 0.0872, 20,   52673),  # K=4.70
    (0.1479, 0.1486, 0.1480, 10,   32171),  # K=4.80
    (0.2262, 0.2278, 0.2297, 30,   10810),  # K=4.90
    (0.3167, 0.3179, 0.3183, 22,   6789),   # K=5.00
    (0.5587, 0.5616, 0.5618, 31,   2121),   # K=5.25
    (0.8080, 0.8110, 0.7956, 21,   728),    # K=5.50
    (1.0577, 1.0611, 1.0451, 1,    1971),   # K=5.75
    (1.3075, 1.3110, 1.2956, 1,    3940),   # K=6.00
]

# ============================================================
# 生成校准输入DataFrame
# ============================================================

rows = []

# 看涨
for i, K in enumerate(strikes):
    bid, ask, last, vol, oi = calls_data[i]
    mid = (bid + ask) / 2
    rows.append({
        'type': 'call',
        'strike': K,
        'bid': bid,
        'ask': ask,
        'mid': round(mid, 4),
        'last': last,
        'volume': vol,
        'open_interest': oi,
        'T': round(T, 6),
        'T_days': T_DAYS,
        'S0': S0,
        'r': r,
        'expiry': '2026-08-26',
    })

# 看跌
for i, K in enumerate(strikes):
    bid, ask, last, vol, oi = puts_data[i]
    mid = (bid + ask) / 2
    rows.append({
        'type': 'put',
        'strike': K,
        'bid': bid,
        'ask': ask,
        'mid': round(mid, 4),
        'last': last,
        'volume': vol,
        'open_interest': oi,
        'T': round(T, 6),
        'T_days': T_DAYS,
        'S0': S0,
        'r': r,
        'expiry': '2026-08-26',
    })

df = pd.DataFrame(rows)

# 过滤掉流动性极差的合约（持仓量<100 或 买卖价差过大）
df['spread'] = df['ask'] - df['bid']
df['spread_pct'] = df['spread'] / df['mid']

# 标记流动性
df['liquidity'] = 'good'
df.loc[df['open_interest'] < 500, 'liquidity'] = 'low'
df.loc[df['spread_pct'] > 0.15, 'liquidity'] = 'wide_spread'

# 保存
output_file = os.path.join(DATA_DIR, 'calibration_input_20260806_real.csv')
df.to_csv(output_file, index=False, encoding='utf-8-sig')

print("=" * 60)
print("✅ 校准输入数据生成完成！")
print("=" * 60)
print(f"  标的: 510300 (沪深300ETF)")
print(f"  S0 = {S0}")
print(f"  到期日: 2026-08-26 ({T_DAYS}天)")
print(f"  r = {r*100:.1f}%")
print(f"  期权总数: {len(df)} 条 (看涨{len(calls_data)} + 看跌{len(puts_data)})")
print(f"  文件: {output_file}")
print()

# 打印摘要
print("看涨期权（购）:")
print(f"  {'行权价':>8s}  {'买价':>8s}  {'卖价':>8s}  {'中间价':>8s}  {'持仓':>8s}  {'流动性':>8s}")
for i, K in enumerate(strikes):
    row = df[df['type']=='call'].iloc[i]
    print(f"  {K:8.2f}  {row['bid']:8.4f}  {row['ask']:8.4f}  {row['mid']:8.4f}  {row['open_interest']:8d}  {row['liquidity']:>8s}")

print()
print("看跌期权（沽）:")
print(f"  {'行权价':>8s}  {'买价':>8s}  {'卖价':>8s}  {'中间价':>8s}  {'持仓':>8s}  {'流动性':>8s}")
for i, K in enumerate(strikes):
    row = df[df['type']=='put'].iloc[i]
    print(f"  {K:8.2f}  {row['bid']:8.4f}  {row['ask']:8.4f}  {row['mid']:8.4f}  {row['open_interest']:8d}  {row['liquidity']:>8s}")

print()
print("数据说明:")
print(f"  - 中间价 = (买价+卖价)/2，校准时优先用这个")
print(f"  - 流动性标记: good=正常, low=持仓<500, wide_spread=价差>15%")
print(f"  - 低流动性合约校准时可酌情剔除")

# 同时生成一个给calibration.py直接用的简化版
calib_simple = df[['type', 'strike', 'mid', 'T', 'S0', 'r']].copy()
calib_simple.columns = ['type', 'K', 'price', 'T', 'S0', 'r']
calib_simple_file = os.path.join(DATA_DIR, 'calibration_input_simple.csv')
calib_simple.to_csv(calib_simple_file, index=False, encoding='utf-8-sig')
print(f"\n简化版（给calibration.py用）: {calib_simple_file}")
