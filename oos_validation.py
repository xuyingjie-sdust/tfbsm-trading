# -*- coding: utf-8 -*-
"""样本外验证实验（用后即删）
训练集: 2026-08-06 真实期权链（dual 文件，过滤低流动性）
测试集: 2026-08-13 快照#36 中 2026-08-26 到期的合约（同一到期日，7 天后截面）
问题: TFBSM(α<1) 在样本外是否真优于 BS(α=1)？
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

# ---------- 训练集: 8-6 dual 数据（与 calibration_gh.py 相同处理） ----------
df = pd.read_csv(os.path.join(BASE, "data", "calibration_input_20260806_dual.csv"))
df_good = df[(df["spread_pct"] < 0.15) & (df["open_interest"] >= 500)].copy()
tr_K = df_good["strike"].values
tr_T = df_good["T"].values
tr_cp = df_good["type"].values
tr_mid = df_good["mid"].values
tr_S0 = df_good["S0"].iloc[0]
r = df_good["r"].iloc[0]
print(f"训练集(8-6): {len(df_good)} 条, S0={tr_S0}")

def rel_obj(K, T, cp, mid, S0, a, s):
    errs = []
    for k, t, c, m in zip(K, T, cp, mid):
        V = tfbsm_price_gh(S0, k, t, r, s, a, c)
        errs.append(((m - V) / max(m, 1e-10)) ** 2)
    return float(np.mean(errs))

# TFBSM 校准（相对误差，网格+NM）
best = (1e10, (1.0, 0.19))
for a in np.arange(0.5, 1.001, 0.02):
    for s in np.arange(0.05, 0.501, 0.01):
        m = rel_obj(tr_K, tr_T, tr_cp, tr_mid, tr_S0, a, s)
        if m < best[0]:
            best = (m, (a, s))
res = minimize(lambda p: rel_obj(tr_K, tr_T, tr_cp, tr_mid, tr_S0, p[0], p[1]),
               x0=list(best[1]), method="Nelder-Mead",
               options={"xatol": 1e-4, "fatol": 1e-10, "maxiter": 300})
a_tr, s_tr = float(res.x[0]), float(res.x[1])
m_tr = rel_obj(tr_K, tr_T, tr_cp, tr_mid, tr_S0, a_tr, s_tr)
# BS 基准
res_bs = minimize_scalar(lambda s: rel_obj(tr_K, tr_T, tr_cp, tr_mid, tr_S0, 1.0, s),
                         bounds=(0.05, 0.6), method="bounded")
s_bs = res_bs.x
m_bs_tr = res_bs.fun
print(f"样本内: TFBSM alpha={a_tr:.4f} sigma={s_tr:.4f} 相对MSE={m_tr:.6f} | "
      f"BS sigma={s_bs:.4f} 相对MSE={m_bs_tr:.6f} | 改善={(m_bs_tr-m_tr)/m_bs_tr*100:+.2f}%")

# ---------- 测试集: 8-13 快照#36 中 2026-08-26 到期 ----------
conn = sqlite3.connect(os.path.join(BASE, "data", "trading_system.db"))
conn.row_factory = sqlite3.Row
snap_id = conn.execute("SELECT MAX(id) FROM snapshots").fetchone()[0]
snap = dict(conn.execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone())
rows = [dict(x) for x in conn.execute(
    "SELECT * FROM option_quotes WHERE snapshot_id=?", (snap_id,)).fetchall()]
conn.close()
te_S0 = snap["S0"]
te = [q for q in rows if q["expire_date"] == "2026-08-26" and q["market_price"] > 0.005]
te_K = np.array([q["K"] for q in te])
te_T = np.array([q["T"] for q in te])
te_cp = [q["cp"] for q in te]
te_mid = np.array([q["market_price"] for q in te])
print(f"\n测试集(8-13): {len(te)} 条, S0={te_S0}, T={te_T[0]*365:.0f}天到期")

def errs_on_test(a, s):
    """返回测试集上 (相对MSE, 绝对MAE, 相对MAE)"""
    rel2, abs1, rel1 = [], [], []
    for k, t, c, m in zip(te_K, te_T, te_cp, te_mid):
        V = tfbsm_price_gh(te_S0, k, t, r, s, a, c)
        rel2.append(((m - V) / max(m, 1e-10)) ** 2)
        abs1.append(abs(m - V))
        rel1.append(abs(m - V) / max(m, 1e-10))
    return np.mean(rel2), np.mean(abs1), np.mean(rel1)

print("\n=== 样本外验证（8-6 参数 → 8-13 市场） ===")
r2_t, a1_t, r1_t = errs_on_test(a_tr, s_tr)
r2_b, a1_b, r1_b = errs_on_test(1.0, s_bs)
print(f"TFBSM(alpha={a_tr:.3f}, sigma={s_tr:.3f}): 相对MSE={r2_t:.6f} 绝对MAE={a1_t:.5f} 相对MAE={r1_t:.3f}")
print(f"BS    (alpha=1.0, sigma={s_bs:.3f}): 相对MSE={r2_b:.6f} 绝对MAE={a1_b:.5f} 相对MAE={r1_b:.3f}")
print(f"样本外相对MSE改善: {(r2_b-r2_t)/r2_b*100:+.2f}% | 绝对MAE改善: {(a1_b-a1_t)/a1_b*100:+.2f}%")

# 逐合约对比
print("\n逐合约样本外误差（TFBSM vs BS，绝对误差）：")
print(f"{'cp':>4} {'K':>6} {'市场价':>8} {'TFBSM':>8} {'BS':>8} {'TFBSM_Err':>10} {'BS_Err':>10}")
for k, t, c, m in zip(te_K, te_T, te_cp, te_mid):
    Vt = tfbsm_price_gh(te_S0, k, t, r, s_tr, a_tr, c)
    Vb = bs_price(te_S0, k, t, r, s_bs, c)
    print(f"{c:>4} {k:>6} {m:>8.4f} {Vt:>8.4f} {Vb:>8.4f} {m-Vt:>10.4f} {m-Vb:>10.4f}")
