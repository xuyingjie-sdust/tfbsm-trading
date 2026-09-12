# -*- coding: utf-8 -*-
"""诊断快照#51：vega 加权下 α 敏感度扫描（用后即删）"""
import sys
import os
import sqlite3
import numpy as np
from scipy.optimize import minimize_scalar

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import pricing

conn = sqlite3.connect(os.path.join(BASE, "data", "trading_system.db"))
conn.row_factory = sqlite3.Row
snap = dict(conn.execute("SELECT * FROM snapshots WHERE id=51").fetchone())
rows = [dict(r) for r in conn.execute("SELECT * FROM option_quotes WHERE snapshot_id=51").fetchall()]
conn.close()

S0, r, q = snap["S0"], snap["r"], snap["q"] or 0.0
K = np.array([x["K"] for x in rows])
T = np.array([x["T"] for x in rows])
cp = [x["cp"] for x in rows]
vm = np.array([x["market_price"] for x in rows])
sp = np.array([x.get("spread_pct") or 0.0 for x in rows])
print(f"快照#51: {len(rows)} 条, S0={S0}, q={q*100:.2f}%")
print(f"spread>15%: {(sp > 0.15).sum()} 条")

# vega 权重（与 pipeline 相同：σ_prior=0.18）
from scipy.stats import norm as _norm
vega = np.array([S0 * np.sqrt(t) * _norm.pdf(
    (np.log(S0 / k) + (r - q + 0.5 * 0.18 ** 2) * t) / (0.18 * np.sqrt(t))) for k, t in zip(K, T)])
w = 1.0 / np.maximum(vega, 1e-4) ** 2
w = w / w.mean()

mask = sp <= 0.15
Ks, Ts, cs, ms, ws = K[mask], T[mask], [cp[i] for i in range(len(cp)) if mask[i]], vm[mask], w[mask]
print(f"流动性过滤后: {len(Ks)} 条\n")

print("alpha 敏感度（vega 加权，σ 各自最优）:")
for a in [1.0, 0.98, 0.96, 0.94, 0.92, 0.90, 0.88, 0.86, 0.84, 0.80]:
    def f(s, aa=a):
        errs = []
        for k, t, c, m, ww in zip(Ks, Ts, cs, ms, ws):
            V = pricing.tfbsm_price_gh(S0, k, t, r, s, aa, c, 32, q)
            errs.append(ww * (m - V) ** 2)
        return float(np.mean(errs))
    res = minimize_scalar(f, bounds=(0.05, 0.5), method="bounded")
    print(f"  alpha={a:.2f} -> sigma={res.x:.4f}, loss={res.fun:.8f}")

# 对比: 昨晚 #46 的数据
conn = sqlite3.connect(os.path.join(BASE, "data", "trading_system.db"))
conn.row_factory = sqlite3.Row
snap46 = dict(conn.execute("SELECT * FROM snapshots WHERE id=46").fetchone())
rows46 = [dict(r) for r in conn.execute("SELECT * FROM option_quotes WHERE snapshot_id=46").fetchall()]
conn.close()
S0b, rb, qb = snap46["S0"], snap46["r"], snap46["q"] or 0.0
Kb = np.array([x["K"] for x in rows46]); Tb = np.array([x["T"] for x in rows46])
cpb = [x["cp"] for x in rows46]; vmb = np.array([x["market_price"] for x in rows46])
print(f"\n对比 快照#46（昨晚）: {len(rows46)} 条, S0={S0b}, q={qb*100:.2f}%")
for a in [1.0, 0.96, 0.90, 0.86]:
    def f2(s, aa=a):
        errs = []
        for k, t, c, m in zip(Kb, Tb, cpb, vmb):
            V = pricing.tfbsm_price_gh(S0b, k, t, rb, s, aa, c, 32, qb)
            errs.append((m - V) ** 2)
        return float(np.mean(errs))
    res = minimize_scalar(f2, bounds=(0.05, 0.5), method="bounded")
    print(f"  alpha={a:.2f} -> sigma={res.x:.4f}, 绝对MSE={res.fun:.8f}")
