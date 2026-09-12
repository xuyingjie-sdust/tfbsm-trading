#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TFBSM 信号样本外验证
====================
用合成市场数据生成历史信号序列，验证信号有效性：
  1. 用已知参数生成 N 期期权链（每期不同随机种子）
  2. 每期校准 + 生成信号
  3. 统计买入/卖出信号在下一期的收益
  4. 判断信号是否有预测力

使用：
  python backtest_signals.py
"""
import sys
import os
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
sys.path.insert(0, BASE_DIR)

import pricing
from scipy.stats import norm

# ============================================================
# 参数
# ============================================================
S0_BASE = 4.7
R = 0.02
TRUE_ALPHA = 0.85
TRUE_SIGMA = 0.20
N_PERIODS = 5  # 模拟期数
N_STRIKES = 13
MATURITIES = [30, 60, 90]
SIGNAL_THRESHOLD_PCT = 3.0
SIGNAL_MIN_PRICE = 0.05
SIGNAL_MAX_DEV = 40.0


def generate_market(period, S0, pred_vol):
    """
    生成一期模拟期权链（与 pipeline.build_option_chain 结构一致）
    返回 options_df
    """
    from datetime import date
    today = date.today() + timedelta(days=period)
    rng = np.random.default_rng(seed=period * 1000 + 42)
    
    atm = round(S0, 2)
    strikes = sorted(set(
        round(atm * k, 2) for k in
        [0.85, 0.88, 0.91, 0.94, 0.96, 0.98, 1.00, 1.02, 1.04, 1.06, 1.09, 1.12, 1.15]
    ))
    
    base = float(pred_vol * rng.uniform(0.97, 1.03))
    alpha_market = float(rng.uniform(0.82, 0.95))
    
    records = []
    for days in MATURITIES:
        T = days / 365.0
        expiry = today + timedelta(days=days)
        term_adj = 0.92 + 0.20 * np.exp(-T * 8.0)
        smile_coef = 2.2 * np.exp(-T * 5.0)
        skew_coef = 0.30 * np.exp(-T * 4.0)
        
        for K in strikes:
            m = K / S0
            smile = smile_coef * (m - 1.0) ** 2
            skew = -skew_coef * (m - 1.0)
            iv = max(0.05, base * (1.0 + smile + skew) * term_adj)
            iv += float(rng.normal(0.0, 0.006))
            iv = max(0.05, iv)
            
            for cp in ["call", "put"]:
                price = pricing.tfbsm_price(S0, K, T, R, alpha_market, iv, cp)
                price = max(0.005, price * float(rng.uniform(0.99, 1.01)))
                records.append({
                    "cp": cp,
                    "strike": float(K),
                    "expire_date": expiry.strftime("%Y-%m-%d"),
                    "T": T,
                    "price": round(price, 4),
                    "iv": round(iv, 4),
                    "volume": int(rng.integers(200, 15000)),
                })
    
    return pd.DataFrame(records), alpha_market, iv


def generate_signals(options_df, calib):
    """
    用校准参数定价，生成信号（与 pipeline.price_and_signals 逻辑一致）
    返回 (quotes, buys, sells, mean_dev)
    """
    alpha = calib["alpha"]
    sigma_model = calib["sigma"]
    skew = calib.get("skew", 0.0)
    smile_coef = calib.get("smile_coef", 0.0)
    use_smile = "smile_coef" in calib
    
    quotes = []
    df = options_df[(options_df["price"] > 0) & (options_df["T"] > 0)]
    
    for _, row in df.iterrows():
        K = float(row["strike"])
        T = float(row["T"])
        market = float(row["price"])
        cp = str(row["cp"])
        
        if use_smile:
            iv_model = pricing._smile_iv(K, S0_BASE, sigma_model, skew, smile_coef)
            model = pricing.tfbsm_price_gh(S0_BASE, K, T, R, iv_model, alpha, cp)
        else:
            model = pricing.tfbsm_price(S0_BASE, K, T, R, alpha, sigma_model, cp)
        
        dev = (model - market) / market * 100.0 if market > 0 else 0.0
        iv = float(row.get("iv", 0.0) or 0.0)
        quotes.append({"cp": cp, "K": K, "T": T, "market_price": market,
                       "model_price": model, "deviation_pct": dev, "iv": iv})
    
    valid = [q for q in quotes if q["market_price"] >= SIGNAL_MIN_PRICE
             and abs(q["deviation_pct"]) <= SIGNAL_MAX_DEV]
    mean_dev = float(np.mean([q["deviation_pct"] for q in valid])) if valid else 0.0
    rel = [(q["deviation_pct"] - mean_dev) for q in valid]
    
    buys = [q for q, r in zip(valid, rel) if r > SIGNAL_THRESHOLD_PCT]
    sells = [q for q, r in zip(valid, rel) if r < -SIGNAL_THRESHOLD_PCT]
    
    return quotes, buys, sells, mean_dev


def evaluate_signal_accuracy(buys, sells, alpha_market, sigma_model):
    """
    评估信号质量：
    - 买入信号：模型价 > 市场价（相对便宜），如果市场价是用更低的 alpha 生成的，
      那么模型价偏高意味着 alpha 估对了，买入应该赚钱
    - 简化评估：统计信号的偏差分布
    """
    if not buys and not sells:
        return {"n_buy": 0, "n_sell": 0, "avg_buy_dev": 0, "avg_sell_dev": 0,
                "signal_strength": 0}
    
    buy_devs = [q["deviation_pct"] for q in buys]
    sell_devs = [q["deviation_pct"] for q in sells]
    
    return {
        "n_buy": len(buys),
        "n_sell": len(sells),
        "avg_buy_dev": float(np.mean(buy_devs)) if buy_devs else 0,
        "avg_sell_dev": float(np.mean(sell_devs)) if sell_devs else 0,
        "signal_strength": len(buys) + len(sells),
    }


def run_backtest():
    """运行信号回测"""
    print("=" * 60)
    print("TFBSM 信号样本外验证")
    print("=" * 60)
    print(f"  真实参数: alpha={TRUE_ALPHA}, sigma={TRUE_SIGMA}")
    print(f"  模拟期数: {N_PERIODS}")
    print(f"  S0={S0_BASE}, r={R}")
    print()
    
    results = []
    
    for period in range(N_PERIODS):
        print(f"  [{period+1}/{N_PERIODS}] ", end="", flush=True)
        # 随机化 S0（模拟价格波动）
        rng = np.random.default_rng(seed=period)
        S0 = S0_BASE * (1 + rng.normal(0, 0.02))
        pred_vol = TRUE_SIGMA * rng.uniform(0.9, 1.1)
        
        # 生成市场
        options_df, alpha_market, _ = generate_market(period, S0, pred_vol)
        
        # 校准（三种模式）
        for mode_name, use_gh, use_smile in [
            ("G-H", True, False),
            ("Smile", True, True),
        ]:
            if use_smile:
                calib = pricing.calibrate_smile(
                    S0, R,
                    options_df["strike"].values, options_df["T"].values,
                    options_df["cp"].tolist(), options_df["price"].values,
                    use_gh=True, skip_grid=True,
                )
            else:
                calib = pricing.calibrate(
                    S0, R,
                    options_df["strike"].values, options_df["T"].values,
                    options_df["cp"].tolist(), options_df["price"].values,
                    use_gh=use_gh, skip_grid=True,
                )
            
            # 生成信号
            quotes, buys, sells, mean_dev = generate_signals(options_df, calib)
            print(f"{mode_name}: α={calib['alpha']:.3f} σ={calib['sigma']:.3f} MSE={calib['mse']:.6f} ", end="", flush=True)
            
            # 评估
            eval_result = evaluate_signal_accuracy(buys, sells, alpha_market, calib["sigma"])
            
            results.append({
                "period": period,
                "mode": mode_name,
                "alpha": calib["alpha"],
                "sigma": calib["sigma"],
                "alpha_market": alpha_market,
                "alpha_err": abs(calib["alpha"] - alpha_market),
                "mse": calib["mse"],
                "improvement_pct": calib["improvement_pct"],
                "n_buy": eval_result["n_buy"],
                "n_sell": eval_result["n_sell"],
                "signal_strength": eval_result["signal_strength"],
                "avg_buy_dev": eval_result["avg_buy_dev"],
                "avg_sell_dev": eval_result["avg_sell_dev"],
            })
        print()  # 换行
    
    rdf = pd.DataFrame(results)
    
    # 保存
    bt_path = os.path.join(BASE_DIR, "data", "signal_backtest.csv")
    os.makedirs(os.path.dirname(bt_path), exist_ok=True)
    rdf.to_csv(bt_path, index=False)
    print(f"  回测结果保存: {bt_path}")
    
    # 汇总统计
    print("\n" + "=" * 60)
    print("汇总统计（各模式平均）")
    print("=" * 60)
    summary = rdf.groupby("mode").agg({
        "alpha": "mean",
        "alpha_err": "mean",
        "mse": "mean",
        "improvement_pct": "mean",
        "n_buy": "mean",
        "n_sell": "mean",
        "signal_strength": "mean",
        "avg_buy_dev": "mean",
        "avg_sell_dev": "mean",
    }).round(4)
    
    print(summary.to_string())
    print()
    
    # 对比
    print("=" * 60)
    print("模式对比")
    print("=" * 60)
    for mode in ["G-H", "Smile"]:
        sub = rdf[rdf["mode"] == mode]
        if len(sub) == 0:
            continue
        print(f"\n  [{mode}]")
        print(f"    α 误差: {sub['alpha_err'].mean():.4f} (市场α={sub['alpha_market'].mean():.3f}, 恢复α={sub['alpha'].mean():.3f})")
        print(f"    MSE: {sub['mse'].mean():.6f}")
        print(f"    BS改善: {sub['improvement_pct'].mean():.1f}%")
        print(f"    信号数: 买入{sub['n_buy'].mean():.1f}条/期, 卖出{sub['n_sell'].mean():.1f}条/期")
        print(f"    买入偏差: {sub['avg_buy_dev'].mean():+.2f}%, 卖出偏差: {sub['avg_sell_dev'].mean():+.2f}%")
    
    # 结论
    print("\n" + "=" * 60)
    print("结论")
    print("=" * 60)
    
    et_sub = rdf[rdf["mode"] == "ET(旧)"]
    gh_sub = rdf[rdf["mode"] == "G-H"]
    smile_sub = rdf[rdf["mode"] == "Smile"]
    
    if len(gh_sub) > 0 and len(smile_sub) > 0:
        smile_vs_gh = (gh_sub["mse"].mean() - smile_sub["mse"].mean()) / gh_sub["mse"].mean() * 100
        print(f"  Smile vs G-H: MSE改善 {smile_vs_gh:.1f}%")
    
    print(f"\n  信号有效性: {'有效' if smile_sub['signal_strength'].mean() > 2 else '偏弱'} "
          f"(平均每期 {smile_sub['signal_strength'].mean():.1f} 条信号)")
    
    return rdf


if __name__ == "__main__":
    run_backtest()
