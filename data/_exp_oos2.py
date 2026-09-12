# -*- coding: utf-8 -*-
"""样本外验证 v2（用后即删）：隔离 σ 过期效应
设计:
  训练集: 2026-08-06 真实期权链（47条） -> 校准结构参数 alpha（σ 自由）
  测试集: 2026-08-13 全截面（82条，3个到期月）-> 重新校准 σ（alpha 用 8-6 固定值）
  对比: TFBSM(alpha_8-6, sigma_8-13) vs BS(alpha=1, sigma_8-13)
问题: alpha 作为时不变结构参数，样本外是否真有价值？
"""
import sys
import os
import numpy as np
import sqlite3
import pandas as pd
from scipy.optimize import minimize_scalar, minimize

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pricing
from pricing import tfbsm_price_gh, bs_price

# ---------- 训练集: 8-6 校准 alpha ----------
df = pd.read_csv(os.path.join(BASE, "data", "calibration_input_20260806_dual.csv"))
df_good = df[(df["spread_pct"] < 0.15) & (df["open_interest"] >= 500)].copy()
tr_K, tr_T, tr_cp, tr_mid = (df_good["strike"].values, df_good["T"].values,
                             df_good["type"].values, df_good["mid"].values)
tr_S0, r = df_good["S0"].iloc[0], df_good["r"].iloc[0]

def rel_mse(K, T, cp, mid, S0, a, s):
    errs = []
    for k, t, c, m in zip(K, T, cp, mid):
        V = tfbsm_price_gh(S0, k, t, r, s, a, c)
        errs.append(((m - V) / max(m, 1e-10)) ** 2)
    return float(np.mean(errs))

best = (1e10, (1.0, 0.19))
for a in np.arange(0.5, 1.001, 0.02):
    for s in np.arange(0.05, 0.501, 0.01):
        m = rel_mse(tr_K, tr_T, tr_cp, tr_mid, tr_S0, a, s)
        if m < best[0]:
            best = (m, (a, s))
res = minimize(lambda p: rel_mse(tr_K, tr_T, tr_cp, tr_mid, tr_S0, p[0], p[1]),
               x0=list(best[1]), method="Nelder-Mead",
               options={"xatol": 1e-4, "fatol": 1e-10, "maxiter": 300})
a_tr, s_tr = float(res.x[0]), float(res.x[1])
print(f"训练(8-6): alpha*={a_tr:.4f} (σ={s_tr:.4f})")

# ---------- 测试集: 8-13 全截面（含流动性过滤） ----------
conn = sqlite3.connect(os.path.join(BASE, "data", "trading_system.db"))
conn.row_factory = sqlite3.Row
snap_id = conn.execute("SELECT MAX(id) FROM snapshots").fetchone()[0]
snap = dict(conn.execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone())
rows = [dict(x) for x in conn.execute(
    "SELECT * FROM option_quotes WHERE snapshot_id=?", (snap_id,)).fetchall()]
conn.close()
te_S0 = snap["S0"]
# 流动性过滤: 价格>0.005 且 中间价>=内在价值（剔除套利违约数据）
te = []
for q in rows:
    if q["market_price"] <= 0.005:
        continue
    intr = max(te_S0 - q["K"], 0) if q["cp"] == "call" else max(q["K"] - te_S0, 0)
    if q["market_price"] < intr - 0.002:  # 明显低于内在价值 = 坏数据
        continue
    te.append(q)
te_K = np.array([q["K"] for q in te])
te_T = np.array([q["T"] for q in te])
te_cp = [q["cp"] for q in te]
te_mid = np.array([q["market_price"] for q in te])
print(f"测试(8-13): {len(te)} 条 (剔除 {len(rows)-len(te)} 条坏数据)")

# ---------- 测试集上重校准 σ（α 固定） ----------
def sigma_calib(a):
    res2 = minimize_scalar(lambda s: rel_mse(te_K, te_T, te_cp, te_mid, te_S0, a, s),
                           bounds=(0.05, 0.6), method="bounded")
    return res2.x, res2.fun

s_bs_te, m_bs_te = sigma_calib(1.0)
s_tf_te, m_tf_te = sigma_calib(a_tr)
print(f"\nσ 重校准(8-13): BS sigma={s_bs_te:.4f} relMSE={m_bs_te:.6f} | "
      f"TFBSM(α={a_tr:.3f}) sigma={s_tf_te:.4f} relMSE={m_tf_te:.6f}")
print(f"样本外改善(α固定, σ重校准): {(m_bs_te-m_tf_te)/m_bs_te*100:+.2f}%")

# ---------- α 敏感度：测试集上扫描 α（σ 各自最优） ----------
print("\n=== α 敏感度（8-13 测试集，σ 各自重校准） ===")
for a in [1.0, 0.95, 0.90, 0.85, 0.80, 0.75, 0.70]:
    s_opt, m_opt = sigma_calib(a)
    print(f"alpha={a:.2f} -> sigma={s_opt:.4f}, relMSE={m_opt:.6f}")

# ---------- 市场非 BS 的证据：微笑/斜偏幅度 ----------
print("\n=== 市场非 BS 的证据（8-13 全截面） ===")
from collections import defaultdict
iv_by_k = defaultdict(list)
for q in te:
    iv = pricing.bs_iv(te_S0, q["K"], q["T"], r, q["market_price"], q["cp"])
    if 0.02 < iv < 1.5:
        iv_by_k[round(q["K"], 2)].append(iv)
print("各行权价反解 IV（均值）:")
for k in sorted(iv_by_k):
    ivs = iv_by_k[k]
    print(f"  K={k}: IV={np.mean(ivs)*100:.1f}% (n={len(ivs)})")
