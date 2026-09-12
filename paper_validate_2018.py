# -*- coding: utf-8 -*-
"""
2018 年 50ETF 期权论文结论验证（上交所官方数据）
================================================
三个实验：
  1. 复现论文逐日校准（GH 20 节点、绝对 MSE、(开+收)/2 中间价、α∈[0.3,1]，无 q）
  2. 加入每日 C/P parity 反解的净持有成本 q 后重新校准
  3. OOS 检验（GLM 方法 B）：第 T 天 α* 固定 → 第 T+1 天重校准 σ，对比 BS

用法：python paper_validate_2018.py [--quick 10]   # quick 模式只跑前 N 天
"""
import sys
import os
import glob
import time
import numpy as np
import pandas as pd
from scipy.optimize import minimize, minimize_scalar
from scipy.stats import norm
from math import gamma as gamma_func
from numpy.polynomial.hermite import hermgauss as _hg

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))

DATA_GLOB = r"C:\Users\xuyin\Desktop\_50etf_2018_tmp\**\SSE_O510050*.csv"
QUICK = None
if "--quick" in sys.argv:
    QUICK = int(sys.argv[sys.argv.index("--quick") + 1])

# 预计算 G-H 节点（20 点，与论文一致）
_HX, _HW = _hg(20)
_HX = _HX[np.argsort(_HX)]
_HW = _HW[np.argsort(_HW)]


def bs_vec(S0, K, T, r, sigma, cp, q):
    """向量化 BS 定价（T 为数组时广播到 G-H 节点）"""
    T = np.maximum(T, 1e-10)
    d1 = (np.log(S0 / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    call = S0 * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    put = K * np.exp(-r * T) * norm.cdf(-d2) - S0 * np.exp(-q * T) * norm.cdf(-d1)
    return np.where(cp == "call", call, put)


def gh_price_batch(S0, K_arr, T_arr, cp_arr, r, sigma, alpha, q, n_quad=20):
    """向量化 G-H 定价：一次性算全部合约"""
    if alpha >= 0.999:
        return bs_vec(S0, K_arr, T_arr, r, sigma, cp_arr, q)
    mean = T_arr ** alpha / gamma_func(1 + alpha)
    m2 = 2 * T_arr ** (2 * alpha) / gamma_func(1 + 2 * alpha)
    var = np.maximum(m2 - mean ** 2, 0)
    x = _HX[:n_quad]
    w = _HW[:n_quad]
    out = np.zeros(len(K_arr))
    for i in range(len(K_arr)):
        if var[i] <= 1e-15:
            out[i] = bs_vec(S0, K_arr[i], np.array([mean[i]]), r, sigma, cp_arr[i], q)[0]
            continue
        U = np.maximum(mean[i] + np.sqrt(2 * var[i]) * x, 0)
        prices = bs_vec(S0, K_arr[i], U, r, sigma, cp_arr[i], q)
        out[i] = np.dot(w, prices) / np.sqrt(np.pi)
    return out


def load_data():
    """加载并合并 12 个月数据，按论文标准过滤"""
    files = sorted(glob.glob(DATA_GLOB, recursive=True))
    dfs = []
    for f in files:
        d = pd.read_csv(f, encoding="gbk")
        dfs.append(d)
    df = pd.concat(dfs, ignore_index=True)
    df.columns = [str(c).strip() for c in df.columns]
    df["日期"] = pd.to_datetime(df["日期"])
    # 过滤（论文 5.1.2）
    df = df[(df["距离到期日天数"] >= 7) & (df["距离到期日天数"] <= 90)]
    df = df[df["成交量"] > 0]
    # 中间价 = (开盘+收盘)/2，缺失侧用非零值
    mid = []
    for o, c in zip(df["开盘价"], df["收盘价"]):
        if o > 0 and c > 0:
            mid.append((o + c) / 2)
        elif o > 0:
            mid.append(o)
        else:
            mid.append(c)
    df["mid"] = mid
    df = df[df["mid"] > 0.001]
    df = df[(df["经插值的隐含波动率"] >= 0.01) & (df["经插值的隐含波动率"] <= 5.0)]
    df = df[df["经插值的隐含波动率"].notna()]
    return df


def calib_day(K, T, cp, vm, S0, r, q, alpha_min=0.3, n_quad=20):
    """单日校准：GH 积分 + 绝对 MSE。返回 (alpha*, sigma*, mse_t, sigma_bs, mse_bs)"""
    def obj(a, s):
        V = gh_price_batch(S0, K, T, cp, r, s, a, q, n_quad)
        return float(np.mean((vm - V) ** 2))

    # 粗网格（论文：α 8 点 × σ 10 点）
    best = (1e10, (1.0, 0.2))
    for a in np.arange(alpha_min, 1.0001, 0.1):
        for s in np.arange(0.05, 0.5001, 0.05):
            m = obj(a, s)
            if m < best[0]:
                best = (m, (a, s))
    res = minimize(lambda p: obj(p[0], p[1]), x0=list(best[1]),
                   method="Nelder-Mead",
                   options={"xatol": 1e-3, "fatol": 1e-8, "maxiter": 120})
    a_star = float(np.clip(res.x[0], alpha_min, 1.0))
    s_star = float(np.clip(res.x[1], 0.01, 1.0))
    mse_t = obj(a_star, s_star)
    # BS 基准：α=1 一维搜 σ
    res_bs = minimize_scalar(lambda s: obj(1.0, s), bounds=(0.05, 0.5), method="bounded")
    return a_star, s_star, mse_t, float(res_bs.x), float(res_bs.fun)


def est_q(K, T, cp, vm, S0, r):
    """C/P parity 反解净持有成本（当日中位数）"""
    qs = []
    # 按 (T, K) 配对 call/put
    pairs = {}
    for k, t, c, m in zip(K, T, cp, vm):
        key = (round(t, 6), round(k, 3))
        pairs.setdefault(key, {})[c] = m
    for (t, k), d in pairs.items():
        if "call" not in d or "put" not in d:
            continue
        C, P = d["call"], d["put"]
        if C <= 0 or P <= 0 or t <= 0:
            continue
        inner = (C - P + k * np.exp(-r * t)) / S0
        if inner <= 0:
            continue
        q = -np.log(inner) / t
        if -0.05 < q < 0.25:
            qs.append(q)
    return float(np.median(qs)) if qs else 0.0


def main():
    t0 = time.time()
    df = load_data()
    dates = sorted(df["日期"].unique())
    if QUICK:
        dates = dates[:QUICK]
    print(f"数据: {len(df)} 条合约记录, {len(dates)} 个交易日")

    rows = []
    for i, day in enumerate(dates):
        grp = df[df["日期"] == day]
        S0 = float(grp["标的ETF收盘价"].iloc[0])
        r = float(grp["无风险利率"].median())
        K = grp["执行价"].values.astype(float)
        T = grp["距离到期日时间（折算成年）"].values.astype(float)
        cp = grp["期权类型"].values
        vm = grp["mid"].values.astype(float)
        q = est_q(K, T, cp, vm, S0, r)
        # 1) 论文原样（无 q）
        a0, s0, m0, bs_s0, mbs0 = calib_day(K, T, cp, vm, S0, r, 0.0)
        imp0 = (mbs0 - m0) / mbs0 * 100 if mbs0 > 0 else 0.0
        # 2) 加入 q
        a1, s1, m1, bs_s1, mbs1 = calib_day(K, T, cp, vm, S0, r, q)
        imp1 = (mbs1 - m1) / mbs1 * 100 if mbs1 > 0 else 0.0
        rows.append({"date": day, "S0": S0, "r": r, "q": q,
                     "n": len(grp),
                     "a_nq": a0, "s_nq": s0, "imp_nq": imp0,
                     "a_q": a1, "s_q": s1, "imp_q": imp1})
        if (i + 1) % 20 == 0 or i == 0:
            print(f"  [{i+1}/{len(dates)}] {day.date()}: n={len(grp)} q={q*100:.1f}% "
                  f"a_nq={a0:.3f} imp_nq={imp0:+.1f}% | a_q={a1:.3f} imp_q={imp1:+.1f}% "
                  f"({time.time()-t0:.0f}s)")

    rdf = pd.DataFrame(rows)
    out = os.path.join(BASE, "data", "paper_2018_calib.csv")
    rdf.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n已保存: {out}")

    # ---- 汇总统计 ----
    print("\n===== 汇总（无 q = 论文原样 | 有 q = 修正后） =====")
    for tag, a_col, imp_col in [("无q", "a_nq", "imp_nq"), ("有q", "a_q", "imp_q")]:
        a = rdf[a_col]
        imp = rdf[imp_col]
        print(f"[{tag}] alpha*: 均值{a.mean():.3f} 中位{a.median():.3f}  "
              f"<0.95占比{(a < 0.95).mean()*100:.1f}% | "
              f"改善: 均值{imp.mean():+.1f}% 中位{imp.median():+.1f}% "
              f"赢BS占比{(imp > 0).mean()*100:.1f}%")

    # ---- OOS 检验（方法 B：昨日 α 固定，今日 σ 新鲜） ----
    print("\n===== OOS 检验（昨日 α 固定，今日 σ 新鲜） =====")
    oos_nq, oos_q = [], []

    def mse_for(a, qq, K, T, cp, vm, S0, r):
        def f(s):
            V = gh_price_batch(S0, K, T, cp, r, s, a, qq, 20)
            return float(np.mean((vm - V) ** 2))
        return minimize_scalar(f, bounds=(0.05, 0.5), method="bounded").fun

    for i in range(1, len(dates)):
        grp = df[df["日期"] == dates[i]]
        S0 = float(grp["标的ETF收盘价"].iloc[0])
        r = float(grp["无风险利率"].median())
        K = grp["执行价"].values.astype(float)
        T = grp["距离到期日时间（折算成年）"].values.astype(float)
        cp = grp["期权类型"].values
        vm = grp["mid"].values.astype(float)
        q = rows[i]["q"]
        prev_a_nq, prev_a_q = rows[i - 1]["a_nq"], rows[i - 1]["a_q"]
        m_tf_nq = mse_for(prev_a_nq, 0.0, K, T, cp, vm, S0, r)
        m_bs_nq = mse_for(1.0, 0.0, K, T, cp, vm, S0, r)
        m_tf_q = mse_for(prev_a_q, q, K, T, cp, vm, S0, r)
        m_bs_q = mse_for(1.0, q, K, T, cp, vm, S0, r)
        oos_nq.append((m_bs_nq - m_tf_nq) / m_bs_nq * 100)
        oos_q.append((m_bs_q - m_tf_q) / m_bs_q * 100)

    on = np.array(oos_nq)
    oq = np.array(oos_q)
    print(f"[无q] OOS改善: 均值{on.mean():+.2f}% 中位{np.median(on):+.2f}% "
          f"赢BS占比{(on > 0).mean()*100:.1f}% (n={len(on)})")
    print(f"[有q] OOS改善: 均值{oq.mean():+.2f}% 中位{np.median(oq):+.2f}% "
          f"赢BS占比{(oq > 0).mean()*100:.1f}% (n={len(oq)})")
    print(f"\n总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
