# -*- coding: utf-8 -*-
"""诊断：单日 alpha 扫描（用后即删）"""
import sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
from paper_validate_2018 import load_data, gh_price_batch
from scipy.optimize import minimize_scalar

df = load_data()
for day in ["2018-01-02", "2018-02-06", "2018-11-15"]:
    grp = df[df["日期"] == pd.to_datetime(day)]
    S0 = float(grp["标的ETF收盘价"].iloc[0])
    r = float(grp["无风险利率"].median())
    K = grp["执行价"].values.astype(float)
    T = grp["距离到期日时间（折算成年）"].values.astype(float)
    cp = grp["期权类型"].values
    vm = grp["mid"].values.astype(float)
    print(f"=== {day} n={len(grp)} S0={S0} r={r*100:.2f}% ===")
    for a in [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3]:
        def f(s):
            V = gh_price_batch(S0, K, T, cp, r, s, a, 0.0, 20)
            return float(np.mean((vm - V) ** 2))
        res = minimize_scalar(f, bounds=(0.05, 0.5), method="bounded")
        print(f"  alpha={a:.1f}: best sigma={res.x:.3f} MSE={res.fun:.6f}")
