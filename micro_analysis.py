#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SPY 期权微观数据分析
=====================
从760条真实期权数据中提取微观层面的交易信息

分析内容：
  1. 波动率微笑（IV Smile）
  2. 定价偏差排名（mispricing）
  3. Put-Call平价检验（套利机会）
  4. 成交量/持仓量集中度（资金在哪）
  5. 期限结构（IV随到期日变化）

使用方式：
  python micro_analysis.py
"""

import os
import sys
import numpy as np
import pandas as pd
from scipy.stats import norm
from datetime import datetime
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figures")
os.makedirs(FIG_DIR, exist_ok=True)


def bs_price(S, K, T, r, sigma, opt_type='call'):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0) if opt_type == 'call' else max(K - S, 0)
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    if opt_type == 'call':
        return S*norm.cdf(d1) - K*np.exp(-r*T)*norm.cdf(d2)
    else:
        return K*np.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)


def bs_iv(S, K, T, r, market_price, opt_type='call'):
    """二分法求隐含波动率"""
    if market_price <= 0:
        return np.nan
    lo, hi = 0.001, 5.0
    for _ in range(100):
        mid = (lo + hi) / 2
        p = bs_price(S, K, T, r, mid, opt_type)
        if abs(p - market_price) < 1e-4:
            return mid
        if p < market_price:
            lo = mid
        else:
            hi = mid
    return mid


def load_data():
    """加载校准数据"""
    # 优先加载带 _real 后缀的文件（真实数据），否则用最新的
    calib_files = sorted([f for f in os.listdir(DATA_DIR) if f.startswith("calibration_input_")])
    if not calib_files:
        print("No calibration data found. Run data_fetch.py first.")
        return None

    # 优先选 _real 文件
    real_files = [f for f in calib_files if "real" in f]
    calib_file = real_files[-1] if real_files else calib_files[-1]
    calib_path = os.path.join(DATA_DIR, calib_file)
    print(f"  Loading: {calib_file}")
    df = pd.read_csv(calib_path)

    # 统一列名：兼容旧版(K, V_market)和新版(strike, price/mid)
    if "strike" in df.columns and "K" not in df.columns:
        df["K"] = df["strike"]
    if "mid" in df.columns and "V_market" not in df.columns:
        df["V_market"] = df["mid"]
    elif "price" in df.columns and "V_market" not in df.columns:
        df["V_market"] = df["price"]

    # 如果没有 T_days，从T反算
    if "T_days" not in df.columns and "T" in df.columns:
        df["T_days"] = df["T"] * 365

    # 从underlying_info获取S0和r
    if "S0" not in df.columns or "r" not in df.columns:
        info_path = os.path.join(DATA_DIR, "underlying_info.csv")
        if os.path.exists(info_path):
            info = pd.read_csv(info_path)
            df["S0"] = info["S0"].iloc[0]
            df["r"] = info["r"].iloc[0]
        else:
            if "S0" not in df.columns:
                print("No S0 found in data")
                return None

    # chain_df直接用df本身（新版数据已含volume, open_interest等）
    # 在列名统一之后再复制，确保chain_df也有K列
    chain_df = df.copy() if "open_interest" in df.columns else None

    return df, chain_df


# ============================================================
# 分析1：波动率微笑
# ============================================================

def analyze_vol_smile(df, chain_df):
    """
    画出不同到期日的IV vs K/S0曲线
    看波动率微笑/倾斜的形状
    """
    print("\n" + "=" * 60)
    print("[1] Volatility Smile Analysis")
    print("=" * 60)

    S0 = df["S0"].iloc[0]
    r = df["r"].iloc[0]

    # 用chain_df如果有iv字段，否则自己算
    if chain_df is not None and "iv" in chain_df.columns and chain_df["iv"].sum() > 0:
        plot_df = chain_df.copy()
        plot_df["moneyness"] = plot_df["K"] / S0
        plot_df = plot_df[plot_df["iv"] > 0]
        plot_df = plot_df[plot_df["iv"] < 2]  # 去掉异常IV
        iv_source = "Yahoo"
    else:
        # 自己算IV
        plot_df = df.copy()
        plot_df["moneyness"] = plot_df["K"] / S0
        plot_df["iv"] = plot_df.apply(
            lambda row: bs_iv(S0, row["K"], row["T"], r, row["V_market"], row["type"]),
            axis=1
        )
        iv_source = "calculated"

    print(f"  IV source: {iv_source}")
    print(f"  Options with valid IV: {len(plot_df)}")

    # 按到期日分组
    if "expiry" in plot_df.columns:
        expiries = sorted(plot_df["expiry"].unique())
    elif "T_days" in plot_df.columns:
        expiries = sorted(plot_df["T_days"].unique())
    else:
        expiries = [1]

    fig, ax = plt.subplots(figsize=(10, 6))
    colors = plt.cm.viridis(np.linspace(0, 0.9, len(expiries)))

    for i, exp in enumerate(expiries):
        if "expiry" in plot_df.columns:
            sub = plot_df[plot_df["expiry"] == exp]
        else:
            sub = plot_df[plot_df["T_days"] == exp]

        calls = sub[sub["type"] == "call"]
        puts = sub[sub["type"] == "put"]

        if len(calls) > 0:
            ax.scatter(calls["moneyness"], calls["iv"], color=colors[i],
                      marker='o', s=20, alpha=0.6, label=f'Calls {exp}')
        if len(puts) > 0:
            ax.scatter(puts["moneyness"], puts["iv"], color=colors[i],
                      marker='x', s=20, alpha=0.6, label=f'Puts {exp}')

    ax.axvline(1.0, color='red', linestyle='--', alpha=0.5, label='ATM (K=S0)')
    ax.set_xlabel('Moneyness (K/S0)', fontsize=12)
    ax.set_ylabel('Implied Volatility', fontsize=12)
    ax.set_title(f'510300 Volatility Smile (S0={S0:.2f})', fontsize=14)
    ax.legend(fontsize=8, loc='upper right')
    ax.set_ylim(0, max(plot_df["iv"].quantile(0.95), 0.5))

    path = os.path.join(FIG_DIR, f'vol_smile_{datetime.now().strftime("%Y%m%d")}.png')
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Chart saved: {path}")

    # 统计IV分布
    print(f"\n  IV Statistics:")
    print(f"    Mean IV:   {plot_df['iv'].mean():.4f} ({plot_df['iv'].mean()*100:.1f}%)")
    print(f"    Median IV: {plot_df['iv'].median():.4f} ({plot_df['iv'].median()*100:.1f}%)")
    print(f"    Std IV:    {plot_df['iv'].std():.4f}")
    print(f"    Min IV:    {plot_df['iv'].min():.4f}")
    print(f"    Max IV:    {plot_df['iv'].max():.4f}")

    # Smile形状判断
    atm_iv = plot_df[(plot_df["moneyness"] > 0.98) & (plot_df["moneyness"] < 1.02)]["iv"].mean()
    otm_put_iv = plot_df[(plot_df["moneyness"] < 0.92) & (plot_df["type"] == "put")]["iv"].mean()
    otm_call_iv = plot_df[(plot_df["moneyness"] > 1.08) & (plot_df["type"] == "call")]["iv"].mean()

    print(f"\n  Smile Shape:")
    print(f"    ATM IV:         {atm_iv:.4f}" if not np.isnan(atm_iv) else "    ATM IV: N/A")
    print(f"    OTM Put IV:     {otm_put_iv:.4f}" if not np.isnan(otm_put_iv) else "    OTM Put IV: N/A")
    print(f"    OTM Call IV:    {otm_call_iv:.4f}" if not np.isnan(otm_call_iv) else "    OTM Call IV: N/A")

    if not np.isnan(otm_put_iv) and not np.isnan(atm_iv):
        skew = otm_put_iv - atm_iv
        print(f"    Skew (OTM Put - ATM): {skew:.4f}")
        if skew > 0.02:
            print(f"    -> Positive skew: market fears downside (typical for equity indices)")
        elif skew < -0.02:
            print(f"    -> Negative skew: market fears upside")
        else:
            print(f"    -> Relatively flat skew: no strong directional fear")


# ============================================================
# 分析2：定价偏差排名
# ============================================================

def analyze_mispricing(df):
    """
    用BS模型(σ=0.1692)定价，找偏差最大的期权
    """
    print("\n" + "=" * 60)
    print("[2] Mispricing Analysis")
    print("=" * 60)

    S0 = df["S0"].iloc[0]
    r = df["r"].iloc[0]
    # 尝试从校准结果读取sigma，没有就用默认值
    sigma = 0.15  # 默认值（510300的合理估计）
    calib_result_files = sorted([f for f in os.listdir(DATA_DIR) if f.startswith("calibration_result_")])
    if calib_result_files:
        try:
            result_df = pd.read_csv(os.path.join(DATA_DIR, calib_result_files[-1]))
            if "sigma" in result_df.columns:
                sigma = result_df["sigma"].iloc[0]
        except Exception:
            pass
    print(f"  Using sigma = {sigma:.4f}")

    df = df.copy()
    df["V_model"] = df.apply(
        lambda row: bs_price(S0, row["K"], row["T"], r, sigma, row["type"]),
        axis=1
    )
    df["mispricing"] = df["V_market"] - df["V_model"]
    df["mispricing_pct"] = df["mispricing"] / df["V_market"] * 100

    # 绝对偏差最大的20条
    df["abs_mispricing"] = df["mispricing"].abs()
    top_mispriced = df.nlargest(20, "abs_mispricing")[
        ["type", "K", "T_days", "V_market", "V_model", "mispricing", "mispricing_pct"]
    ]

    print(f"\n  Model: BS with sigma={sigma}")
    print(f"  S0={S0:.2f}, r={r*100:.1f}%")
    print(f"\n  Top 20 Most Mispriced Options:")
    print(f"  {'Type':<6} {'K':>8} {'T(days)':>8} {'Market':>10} {'Model':>10} {'Diff':>10} {'Diff%':>8}")
    print(f"  {'-'*66}")
    for _, row in top_mispriced.iterrows():
        print(f"  {row['type']:<6} {row['K']:>8.1f} {row['T_days']:>8.0f} "
              f"{row['V_market']:>10.2f} {row['V_model']:>10.2f} "
              f"{row['mispricing']:>+10.2f} {row['mispricing_pct']:>+7.1f}%")

    # 按类型统计
    print(f"\n  Mispricing by Type:")
    for opt_type in ["call", "put"]:
        sub = df[df["type"] == opt_type]
        print(f"    {opt_type.upper():>4}: mean={sub['mispricing'].mean():+.2f}, "
              f"median={sub['mispricing'].median():+.2f}, "
              f"std={sub['mispricing'].std():.2f}")

    # 按到期日统计
    if "T_days" in df.columns:
        print(f"\n  Mispricing by Expiration:")
        for exp in sorted(df["T_days"].unique()):
            sub = df[df["T_days"] == exp]
            print(f"    T={exp:>3.0f}d: mean={sub['mispricing'].mean():+.2f}, "
                  f"n={len(sub)}")

    # 保存完整排名
    path = os.path.join(DATA_DIR, f"mispricing_{datetime.now().strftime('%Y%m%d')}.csv")
    df.sort_values("abs_mispricing", ascending=False).to_csv(path, index=False)
    print(f"\n  Full ranking saved: {path}")


# ============================================================
# 分析3：Put-Call平价检验
# ============================================================

def analyze_put_call_parity(df):
    """
    Put-Call Parity: C - P = S0 - K*e^(-rT)
    找违反平价的套利机会
    """
    print("\n" + "=" * 60)
    print("[3] Put-Call Parity Check")
    print("=" * 60)

    S0 = df["S0"].iloc[0]
    r = df["r"].iloc[0]

    # 匹配相同K和T的call和put
    calls = df[df["type"] == "call"][["K", "T", "T_days", "V_market"]].rename(
        columns={"V_market": "C"})
    puts = df[df["type"] == "put"][["K", "T", "T_days", "V_market"]].rename(
        columns={"V_market": "P"})

    merged = pd.merge(calls, puts, on=["K", "T", "T_days"])
    merged["parity_rhs"] = S0 - merged["K"] * np.exp(-r * merged["T"])
    merged["C_minus_P"] = merged["C"] - merged["P"]
    merged["violation"] = merged["C_minus_P"] - merged["parity_rhs"]
    merged["violation_pct"] = merged["violation"] / merged["C"] * 100

    print(f"  Matched pairs: {len(merged)}")
    print(f"  S0 = {S0:.2f}, r = {r*100:.1f}%")

    if len(merged) == 0:
        print("  No matched pairs found")
        return

    print(f"\n  Parity Violation Statistics:")
    print(f"    Mean violation:   {merged['violation'].mean():+.2f}")
    print(f"    Median violation: {merged['violation'].median():+.2f}")
    print(f"    Std:              {merged['violation'].std():.2f}")
    print(f"    Max violation:    {merged['violation'].max():+.2f}")
    print(f"    Min violation:    {merged['violation'].min():+.2f}")

    # 套利机会：|violation| > 交易成本（假设每张期权$1.5手续费+bid-ask spread）
    threshold = 3.0  # $3 per share (roughly $300 per contract)
    arbitrage = merged[merged["violation"].abs() > threshold]

    print(f"\n  Potential Arbitrage (|violation| > ${threshold}):")
    print(f"    Count: {len(arbitrage)}")
    if len(arbitrage) > 0:
        print(f"\n    {'K':>8} {'T(d)':>6} {'C':>8} {'P':>8} {'C-P':>8} {'Theoretical':>12} {'Violation':>10}")
        print(f"    {'-'*64}")
        for _, row in arbitrage.head(15).iterrows():
            print(f"    {row['K']:>8.1f} {row['T_days']:>6.0f} {row['C']:>8.2f} {row['P']:>8.2f} "
                  f"{row['C_minus_P']:>8.2f} {row['parity_rhs']:>12.2f} {row['violation']:>+10.2f}")

        if merged["violation"].mean() > threshold:
            print(f"\n    -> Overall C-P > theoretical: calls relatively overpriced")
        elif merged["violation"].mean() < -threshold:
            print(f"\n    -> Overall C-P < theoretical: puts relatively overpriced")
    else:
        print(f"    No significant arbitrage opportunities found")
        print(f"    (Market is efficiently priced at this threshold)")


# ============================================================
# 分析4：成交量/持仓量集中度
# ============================================================

def analyze_volume_concentration(chain_df):
    """
    分析资金在哪些行权价和到期日上集中
    """
    print("\n" + "=" * 60)
    print("[4] Volume & Open Interest Analysis")
    print("=" * 60)

    if chain_df is None:
        print("  No chain data available")
        return

    df = chain_df.copy()

    # 成交量最高的期权
    if "volume" in df.columns and df["volume"].sum() > 0:
        print(f"\n  Top 15 Most Traded Options:")
        top_vol = df.nlargest(15, "volume")[
            ["type", "K", "T_days", "volume", "open_interest", "V_market"]
        ]
        print(f"  {'Type':<6} {'K':>8} {'T(d)':>6} {'Volume':>10} {'OI':>10} {'Price':>8}")
        print(f"  {'-'*52}")
        for _, row in top_vol.iterrows():
            print(f"  {row['type']:<6} {row['K']:>8.1f} {row['T_days']:>6.0f} "
                  f"{row['volume']:>10.0f} {row.get('open_interest', 0):>10.0f} "
                  f"{row['V_market']:>8.2f}")
    else:
        print("  No volume data available")
        return

    # 按行权价汇总
    print(f"\n  Volume by Strike (Top 10):")
    vol_by_K = df.groupby("K").agg({"volume": "sum", "open_interest": "sum"}).sort_values("volume", ascending=False)
    print(f"  {'K':>8} {'Total Vol':>12} {'Total OI':>12}")
    print(f"  {'-'*34}")
    for K, row in vol_by_K.head(10).iterrows():
        print(f"  {K:>8.1f} {row['volume']:>12.0f} {row['open_interest']:>12.0f}")

    # 按到期日汇总
    print(f"\n  Volume by Expiration:")
    vol_by_exp = df.groupby("T_days").agg({"volume": "sum", "open_interest": "sum"}).sort_index()
    print(f"  {'T(days)':>8} {'Total Vol':>12} {'Total OI':>12}")
    print(f"  {'-'*34}")
    for T, row in vol_by_exp.iterrows():
        print(f"  {T:>8.0f} {row['volume']:>12.0f} {row['open_interest']:>12.0f}")

    # Call vs Put 总量
    print(f"\n  Call vs Put:")
    for opt_type in ["call", "put"]:
        sub = df[df["type"] == opt_type]
        total_vol = sub["volume"].sum()
        total_oi = sub["open_interest"].sum() if "open_interest" in sub.columns else 0
        print(f"    {opt_type.upper():>4}: volume={total_vol:>10.0f}, OI={total_oi:>10.0f}")

    # Put/Call ratio
    total_call_vol = df[df["type"] == "call"]["volume"].sum()
    total_put_vol = df[df["type"] == "put"]["volume"].sum()
    if total_call_vol > 0:
        pcr = total_put_vol / total_call_vol
        print(f"\n  Put/Call Volume Ratio: {pcr:.3f}")
        if pcr > 1.0:
            print(f"    -> PCR > 1: puts more traded than calls (bearish sentiment)")
        elif pcr > 0.7:
            print(f"    -> PCR 0.7-1.0: balanced trading")
        else:
            print(f"    -> PCR < 0.7: calls dominate (bullish sentiment)")

    # 画图
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # Volume by K
    ax = axes[0]
    vol_by_K_plot = df.groupby("K")["volume"].sum().sort_index()
    ax.bar(vol_by_K_plot.index, vol_by_K_plot.values, width=5, alpha=0.7)
    ax.set_xlabel('Strike Price', fontsize=12)
    ax.set_ylabel('Total Volume', fontsize=12)
    ax.set_title('Volume Distribution by Strike', fontsize=13)

    # OI by K
    ax = axes[1]
    if "open_interest" in df.columns:
        oi_by_K = df.groupby("K")["open_interest"].sum().sort_index()
        ax.bar(oi_by_K.index, oi_by_K.values, width=5, alpha=0.7, color='orange')
        ax.set_xlabel('Strike Price', fontsize=12)
        ax.set_ylabel('Total Open Interest', fontsize=12)
        ax.set_title('Open Interest Distribution by Strike', fontsize=13)

    path = os.path.join(FIG_DIR, f'volume_concentration_{datetime.now().strftime("%Y%m%d")}.png')
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"\n  Chart saved: {path}")


# ============================================================
# 分析5：期限结构
# ============================================================

def analyze_term_structure(df):
    """
    分析IV随到期日的变化
    """
    print("\n" + "=" * 60)
    print("[5] Term Structure Analysis")
    print("=" * 60)

    S0 = df["S0"].iloc[0]
    r = df["r"].iloc[0]

    # 计算每个到期日的ATM IV
    df = df.copy()
    df["moneyness"] = df["K"] / S0

    # ATM定义：moneyness在0.97-1.03之间
    atm = df[(df["moneyness"] > 0.97) & (df["moneyness"] < 1.03)].copy()

    if len(atm) == 0:
        print("  No ATM options found")
        return

    atm["iv"] = atm.apply(
        lambda row: bs_iv(S0, row["K"], row["T"], r, row["V_market"], row["type"]),
        axis=1
    )
    atm = atm.dropna(subset=["iv"])

    print(f"\n  ATM IV by Expiration:")
    print(f"  {'T(days)':>8} {'IV':>10} {'IV(%)':>8} {'n':>4}")
    print(f"  {'-'*32}")
    for T in sorted(atm["T_days"].unique()):
        sub = atm[atm["T_days"] == T]
        print(f"  {T:>8.0f} {sub['iv'].mean():>10.4f} {sub['iv'].mean()*100:>7.1f}% {len(sub):>4}")

    # 期限结构形状
    iv_by_T = atm.groupby("T_days")["iv"].mean().sort_index()
    if len(iv_by_T) >= 2:
        slope = iv_by_T.iloc[-1] - iv_by_T.iloc[0]
        print(f"\n  Term Structure Slope: {slope:+.4f}")
        if slope > 0.01:
            print(f"    -> Upward sloping: long-term IV > short-term IV")
            print(f"    -> Market expects volatility to increase (typical in calm periods)")
        elif slope < -0.01:
            print(f"    -> Downward sloping (inverted): short-term IV > long-term IV")
            print(f"    -> Market expects near-term event / fear")
        else:
            print(f"    -> Flat term structure")


# ============================================================
# 主函数
# ============================================================

def main():
    print("=" * 60)
    print("510300 Options Micro Analysis")
    print("=" * 60)

    result = load_data()
    if result is None:
        return

    df, chain_df = result

    S0 = df["S0"].iloc[0]
    r = df["r"].iloc[0]
    print(f"S0 = {S0:.2f}")
    print(f"r = {r*100:.1f}%")
    print(f"Options: {len(df)}")
    if chain_df is not None:
        print(f"Chain data: {len(chain_df)}")

    # 5项分析
    analyze_vol_smile(df, chain_df)
    analyze_mispricing(df)
    analyze_put_call_parity(df)
    analyze_volume_concentration(chain_df)
    analyze_term_structure(df)

    print("\n" + "=" * 60)
    print("Analysis Complete!")
    print(f"Charts: {FIG_DIR}")
    print(f"Data: {DATA_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
