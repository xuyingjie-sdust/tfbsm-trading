#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TFBSM 交易系统每小时数据流水线
===============================
单次流程：
  1. 腾讯行情拉取 510300 实时价 + 历史 K 线（失败回退默认价 4.7）
  2. 计算 20 日历史已实现波动率
  3. 生成模拟期权链（3 到期日 x 13 行权价 x C/P）
  4. 校准 (alpha, sigma)，与 BS 基准对比
  5. Transformer 预测未来 20 日波动率（模型缺失时回退历史波动率）
  6. 组合定价 + 偏差计算 + 交易信号
  7. 保存快照/报价/日志到 SQLite

函数 run_pipeline() 供 app.py 的定时任务与手动触发调用。
"""

import os
import io
import sys
import json
import time
import contextlib
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
DATA_DIR = os.path.join(BASE_DIR, "data")
MODEL_DIR = os.path.join(BASE_DIR, "models")

import db as dbm
import pricing

# 复用 data_fetch_v9 的行情函数（模块级默认配置为 510300 国内标的）
import data_fetch_v9 as df9

DEFAULT_S0 = 4.7
DEFAULT_R = 0.02
SIGNAL_THRESHOLD_PCT = 3.0   # 偏差超过该百分比才生成信号
SIGNAL_MIN_PRICE = 0.05      # 市场价下限：过滤深度虚值/低价噪声合约
SIGNAL_MAX_DEV = 40.0        # 偏差上限：过滤极端噪声值
SIGNAL_MAX_SPREAD = 0.15     # 买卖价差上限（相对中间价）：过滤低流动性合约

# 校准模式：'gh' = G-H 积分法（推荐，真实数据稳健），'et' = 有效时间法，'smile' = 多参数微笑校准（真实数据易撞参数边界）
CALIBRATION_MODE = 'gh'

# 波动率预测收缩混合权重：Transformer 样本外改善≈0（过拟合），向朴素复读大幅收缩。
# blend = VOL_TF_WEIGHT * Transformer + (1-VOL_TF_WEIGHT) * 当日已实现波动率(20日)
VOL_TF_WEIGHT = 0.3

# 流水线运行状态（供 /api/status 读取）
PIPELINE_STATE = {
    "running": False,
    "last_run": None,       # 上次运行完成时间
    "last_duration": None,  # 上次运行耗时（秒）
    "last_error": None,     # 上次错误信息
    "snapshot_id": None,    # 上次快照 ID
}


def _capture(fn, *args, **kwargs):
    """执行函数并捕获其 stdout 输出到日志"""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            result = fn(*args, **kwargs)
        out = buf.getvalue().strip()
        if out:
            for line in out.splitlines()[-20:]:  # 只保留末尾 20 行，避免日志过长
                dbm.add_log("INFO", "[fetch] " + line[:200])
        return result
    except Exception as e:
        out = buf.getvalue().strip()
        if out:
            for line in out.splitlines()[-10:]:
                dbm.add_log("WARN", "[fetch] " + line[:200])
        raise


# ============================================================
# 步骤 1：拉取行情
# ============================================================

def fetch_market():
    """
    返回 (S0, prev_close, price_df, source)
    price_df: 历史 K 线 DataFrame（date/open/close/high/low/volume）
    """
    source = "tencent"
    try:
        dbm.add_log("INFO", "拉取腾讯行情: 510300 实时价 + 历史K线...")
        df_hist, S0 = _capture(df9.fetch_price_tencent)
        # fetch_price_tencent 内部吞掉异常并返回 (None, None)，这里必须显式回退默认价
        if not S0:
            dbm.add_log("WARN", f"实时价缺失，回退默认价 {DEFAULT_S0}")
            S0 = DEFAULT_S0
        prev_close = 0.0
        if df_hist is not None and len(df_hist) > 0:
            df_hist = df_hist.sort_values("date").reset_index(drop=True)
            prev_close = float(df_hist["close"].iloc[-2]) if len(df_hist) > 1 else 0.0
            dbm.add_log("INFO", f"行情获取成功: S0={S0:.4f}, 历史K线 {len(df_hist)} 条")
        else:
            df_hist = _load_local_price()
            dbm.add_log("WARN", "在线K线为空，使用本地历史数据")
        return S0, prev_close, df_hist, source
    except Exception as e:
        dbm.add_log("ERROR", f"腾讯行情拉取失败: {type(e).__name__}: {e}，回退默认价 {DEFAULT_S0}")
        return DEFAULT_S0, 0.0, _load_local_price(), "fallback"


def fetch_tick():
    """
    轻量实时价采集（每分钟调用）：只拉腾讯实时价，不拉K线，失败静默返回 None
    """
    import re
    import requests
    url = "https://qt.gtimg.cn/q=sh510300"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://gu.qq.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        text = resp.text.strip()
        m = re.search(r'v_\w+="([^"]+)"', text)
        if not m:
            return None
        fields = m.group(1).split("~")
        price = float(fields[3]) if len(fields) > 3 else 0.0
        return price if price > 0 else None
    except Exception:
        return None


def backfill_ticks_from_history():
    """
    把历史日线收盘价回填到 price_ticks（幂等，已存在的跳过）。
    时间戳用当日 15:00:00（收盘时刻），今天的日线跳过（由实时 tick 覆盖）。
    """
    df = _load_local_price()
    if df is None or len(df) == 0:
        dbm.add_log("WARN", "回填历史价格失败：本地K线数据缺失")
        return 0
    today_str = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for _, r in df.iterrows():
        d = pd.to_datetime(r["date"])
        if d.strftime("%Y-%m-%d") == today_str:
            continue  # 今天的日线由实时 tick 覆盖
        ts = d.strftime("%Y-%m-%d") + " 15:00:00"
        rows.append((ts, float(r["close"])))
    n = dbm.backfill_daily_ticks(rows)
    if n:
        dbm.add_log("INFO", f"已回填历史日线价格 {n} 条到实时走势表（每日 15:00 收盘价）")
    return n


def _load_local_price():
    """从本地 CSV 加载历史 K 线"""
    path = os.path.join(DATA_DIR, "etf_price_510300_tencent.csv")
    if not os.path.exists(path):
        path = os.path.join(DATA_DIR, "etf_price_510300.csv")
    if os.path.exists(path):
        try:
            df = pd.read_csv(path)
            df["date"] = pd.to_datetime(df["date"])
            return df.sort_values("date").reset_index(drop=True)
        except Exception:
            pass
    return None


# ============================================================
# 步骤 2：历史波动率
# ============================================================

def compute_hist_vol(price_df):
    """20 日年化已实现波动率（√365，与 Transformer 特征口径一致，前端两条线可比）"""
    if price_df is None or len(price_df) < 21:
        return 0.0
    df = price_df.copy()
    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
    return float(df["log_ret"].dropna().tail(20).std() * np.sqrt(365))


# ============================================================
# 步骤 3：期权链
# ============================================================

def _chain_quality_ok(df, S0, r):
    """真实期权链数据质量校验：
    1. 数量足够（>=10）
    2. 价格全为正且无极端异常（cost 不得远高于 S0，防止单位/字段错位）
    3. BS 反解 IV 大部分落在合理区间（1%~150%），否则说明价格字段不可信
    返回 (ok, 诊断信息)
    """
    import pricing
    if df is None or len(df) < 10:
        return False, f"数量不足({0 if df is None else len(df)})"
    try:
        prices = df["price"].values
        strikes = df["strike"].values
        times = df["T"].values
        types = list(df["cp"])
        if (prices <= 0).any():
            return False, "存在非正价格"
        # 深度实值 call 价格理论上 ≤ S0；若出现远超 S0 的价格，说明字段错位/单位错误
        if prices.max() > S0 * 1.2:
            return False, f"价格异常(S0={S0:.2f} max={prices.max():.2f})"
        # BS 反解 IV 合理性统计
        ok_iv = 0
        for K, T, cp, p in zip(strikes, times, types, prices):
            try:
                iv = pricing.bs_iv(S0, K, T, r, p, cp)
                if iv == iv and 0.01 <= iv <= 1.5:  # 1%~150%
                    ok_iv += 1
            except Exception:
                continue
        if ok_iv / len(df) < 0.6:
            return False, f"IV反解通过率过低({ok_iv}/{len(df)})"
    except Exception as e:
        return False, f"校验异常:{type(e).__name__}"
    return True, f"通过({len(df)}条, IV通过{ok_iv}/{len(df)})"


def build_option_chain_with_fallback(S0, base_vol=None):
    """
    优先尝试拉取真实期权链（新浪 → 东方财富），失败回退模拟期权链。
    每个真实源都过数据质量校验（数量+价格+IV合理性），不合格则继续回退。
    返回 (options_df, source)
    """
    # 1) 新浪（上交所 ETF 期权真实行情，主数据源）
    try:
        real_df = df9.fetch_options_sina(S0)
        if real_df is not None and len(real_df) >= 10:
            ok, diag = _chain_quality_ok(real_df, S0, DEFAULT_R)
            if ok:
                dbm.add_log("INFO", f"使用真实期权链[新浪]: {len(real_df)} 条合约 ({diag})")
                return real_df, "sina"
            dbm.add_log("WARN", f"新浪期权链质量不合格: {diag}")
    except Exception as e:
        dbm.add_log("WARN", f"新浪期权链拉取失败: {e}")

    # 2) 东方财富（备用源）
    try:
        real_df = df9.fetch_options_eastmoney(S0)
        if real_df is not None and len(real_df) >= 10:
            ok, diag = _chain_quality_ok(real_df, S0, DEFAULT_R)
            if ok:
                dbm.add_log("INFO", f"使用真实期权链[东方财富]: {len(real_df)} 条合约 ({diag})")
                return real_df, "eastmoney"
            dbm.add_log("WARN", f"东方财富期权链质量不合格: {diag}")
    except Exception as e:
        dbm.add_log("WARN", f"东方财富期权链拉取失败: {e}")
    
    # 3) 回退模拟
    dbm.add_log("WARN", "真实期权链均不可用或质量不合格，回退模拟期权链")
    df = build_option_chain(S0, base_vol=base_vol)
    return df, "simulated"


def build_option_chain(S0, base_vol=None):
    """
    生成带市场结构的模拟期权链：
      - 市场价用 TFBSM 定价生成（与模型结构一致，避免系统性偏差）
      - 波动率微笑：OTM 两侧 IV 升高（U 形）+ 偏斜（低行权价 IV 更高）
      - 期限结构：近月 IV 高于远月（正向期限结构的衰减形式）
      - 时间种子噪声：每小时不同的随机种子，使每次运行的链有真实变化
    base_vol: 市场 IV 中心（默认用历史波动率）
    """
    from datetime import date
    from scipy.stats import norm as sp_norm

    today = date.today()
    maturities = [30, 60, 90]

    # 行权价范围 ±15%（ATM 附近加密）
    atm = round(S0, 2)
    strikes = sorted(set(
        round(atm * k, 2) for k in
        [0.85, 0.88, 0.91, 0.94, 0.96, 0.98, 1.00, 1.02, 1.04, 1.06, 1.09, 1.12, 1.15]
    ))

    # 时间种子：每小时变化，同一小时内可复现
    seed = int(datetime.now().strftime("%Y%m%d%H"))
    rng = np.random.default_rng(seed)

    # 市场波动率中心：围绕基准波动率 ±3% 小幅漂移（市场对预测波动的合理定价偏差）
    base_src = base_vol if base_vol and 0.05 < base_vol < 0.8 else 0.19
    base = float(base_src * rng.uniform(0.97, 1.03))

    # 市场自身的分数阶参数（模型需通过校准识别，每小时小幅漂移）
    alpha_market = float(rng.uniform(0.82, 0.95))

    r = DEFAULT_R
    records = []
    raw_ivs = []  # 先生成带结构的 IV，再归一化
    for days in maturities:
        T = days / 365.0
        expiry = today + timedelta(days=days)

        # 期限结构：近月 IV 更高，向远月衰减
        term_adj = 0.92 + 0.20 * np.exp(-T * 8.0)

        # 微笑强度：近月微笑更陡，远月趋平
        smile_coef = 2.2 * np.exp(-T * 5.0)
        skew_coef = 0.30 * np.exp(-T * 4.0)

        for K in strikes:
            m = K / S0  # moneyness
            smile = smile_coef * (m - 1.0) ** 2          # U 形微笑
            skew = -skew_coef * (m - 1.0)                # 负偏斜：低行权价 IV 更高
            iv = max(0.05, base * (1.0 + smile + skew) * term_adj)
            iv += float(rng.normal(0.0, 0.006))          # 合约级噪声
            iv = max(0.05, iv)
            raw_ivs.append((expiry, T, K, iv))

    # 归一化：使市场 IV 均值精确等于 base（微笑/期限结构只保留形状，不引入系统性偏差）
    iv_mean = float(np.mean([x[3] for x in raw_ivs]))
    if iv_mean > 0:
        scale = base / iv_mean
        raw_ivs = [(e, T, K, max(0.05, iv * scale)) for e, T, K, iv in raw_ivs]

    for expiry, T, K, iv in raw_ivs:
        for cp in ["call", "put"]:
            # 市场价用 TFBSM 定价生成（alpha_market + iv），与模型定价结构一致
            price = pricing.tfbsm_price(S0, K, T, r, alpha_market, iv, cp)
            price = max(0.005, price * float(rng.uniform(0.99, 1.01)))  # 买卖价差噪声

            records.append({
                "code": f"SIM_{cp}_{K}_{int(round(T*365))}d",
                "name": f"510300{cp[0].upper()}{K}",
                "cp": cp,
                "strike": float(K),
                "expire_date": expiry.strftime("%Y-%m-%d"),
                "T": T,
                "price": round(price, 4),
                "iv": round(iv, 4),
                "volume": int(rng.integers(200, 15000)),
                "open_interest": int(rng.integers(500, 30000)),
            })

    df = pd.DataFrame(records)
    iv_min, iv_max = df["iv"].min(), df["iv"].max()
    dbm.add_log("INFO",
                f"期权链生成: {len(df)} 条合约 (IV {iv_min*100:.1f}%~{iv_max*100:.1f}%, "
                f"base {base*100:.1f}%, alpha_market {alpha_market:.3f}, seed {seed})")
    return df


# ============================================================
# 步骤 4：校准
# ============================================================

def estimate_holding_cost(S0, options_df, r):
    """
    用 put-call parity 从真实期权链反解隐含净持有成本 q（分红 + 融券成本）。
    C - P = S0*e^{-qT} - K*e^{-rT}  =>  q = -ln((C - P + K*e^{-rT}) / S0) / T
    取全部有效 C/P 对的中位数，稳健估计。
    模拟链由 BS/TFBSM 无持有成本生成，反解会接近 0，自动兼容。
    流动性过滤：价差过宽的合约（午休/盘后错配）不参与反解。
    """
    if options_df is None or len(options_df) == 0:
        return 0.0
    df = options_df[(options_df["price"] > 0.005) & (options_df["T"] > 0)].copy()
    if "spread_pct" in df.columns:
        df = df[df["spread_pct"] <= SIGNAL_MAX_SPREAD]
    qs = []
    for (exp, K), grp in df.groupby(["expire_date", "strike"]):
        c = grp[grp["cp"] == "call"]
        p = grp[grp["cp"] == "put"]
        if len(c) == 0 or len(p) == 0:
            continue
        C, P, T = float(c["price"].iloc[0]), float(p["price"].iloc[0]), float(c["T"].iloc[0])
        if C <= 0 or P <= 0 or T <= 0:
            continue
        inner = (C - P + K * np.exp(-r * T)) / S0
        if inner <= 0:
            continue
        q = -np.log(inner) / T
        # 净持有成本（分红+融券）非负；小负值容忍报价噪声，明显负值剔除
        if -0.01 < q < 0.25:
            qs.append(q)
    q_est = float(np.median(qs)) if qs else 0.0
    q_est = max(q_est, 0.0)  # 下限保护：q 物理上非负
    dbm.add_log("INFO", f"隐含净持有成本 q = {q_est*100:.2f}%/年 ({len(qs)} 对 C/P 反解)")
    return q_est


def _vega_weights(S0, r, q, strikes, times, sigma_prior=0.18):
    """
    vega 加权：每个合约按 1/vega² 加权（波动率信息量等权）。
    实验结论（2026-08-14）：vega 加权会让 α 拟合虚值合约噪声，
    样本内改善（α<1）在样本外全部失效——属于过拟合，生产未采用。
    保留此函数仅供后续研究对比。
    """
    from scipy.stats import norm as sp_norm
    ws = []
    for K, T in zip(strikes, times):
        T_safe = max(float(T), 1e-6)
        d1 = (np.log(S0 / K) + (r - q + 0.5 * sigma_prior ** 2) * T_safe) / (sigma_prior * np.sqrt(T_safe))
        vega = S0 * np.sqrt(T_safe) * sp_norm.pdf(d1)
        ws.append(1.0 / max(vega, 1e-4) ** 2)
    ws = np.array(ws, dtype=float)
    return ws / ws.mean()


def run_calibration(S0, r, options_df, q=0.0):
    """校准 (alpha, sigma)，支持 G-H 积分法 / 有效时间法 / 多参数微笑校准。
    q 为净持有成本；目标函数为绝对价格 MSE（等权）——样本外验证表明
    vega 加权会放大虚值合约噪声导致 α 过拟合，生产采用等权绝对 MSE，α 自然收敛。
    流动性过滤：买卖价差 >15% 的合约不进校准（午休/盘后报价错配噪声）。"""
    if options_df is None or len(options_df) == 0:
        return None
    df = options_df[(options_df["price"] > 0) & (options_df["strike"] > 0) & (options_df["T"] > 0)]
    # 流动性过滤：剔除买卖价差过宽的合约（模拟链无 spread 字段则不过滤）
    if "spread_pct" in df.columns:
        n_before = len(df)
        df = df[df["spread_pct"] <= SIGNAL_MAX_SPREAD]
        if len(df) < n_before:
            dbm.add_log("INFO", f"校准流动性过滤: {n_before} -> {len(df)} 条 (价差>{SIGNAL_MAX_SPREAD*100:.0f}% 剔除)")
    if len(df) < 10:
        dbm.add_log("WARN", f"有效期权过少({len(df)})，跳过校准")
        return None

    mode = CALIBRATION_MODE
    dbm.add_log("INFO", f"开始校准 [{mode}] ({len(df)} 条合约, q={q*100:.1f}%)...")
    t0 = time.time()

    strikes = df["strike"].values
    times = df["T"].values

    if mode == 'smile':
        # 多参数微笑校准：先 G-H 两参数粗搜，再四参数精化
        result = pricing.calibrate_smile(
            S0, r,
            strikes, times,
            df["cp"].tolist(), df["price"].values,
            use_gh=True, q=q,
        )
        dt = time.time() - t0
        dbm.add_log("INFO",
                    f"微笑校准完成({dt:.1f}s): alpha={result['alpha']:.4f}, sigma={result['sigma']:.4f}, "
                    f"skew={result['skew']:.4f}, smile={result['smile_coef']:.4f}, "
                    f"MSE={result['mse']:.6f}, BS_MSE={result['bs_mse']:.6f}, "
                    f"改善vsBS={result['improvement_pct']:.1f}%, 改善vs2P={result['improvement_vs_2p']:.1f}%")
    elif mode == 'gh':
        # G-H 积分法校准（能捕获 α<1 效应）
        result = pricing.calibrate(
            S0, r,
            strikes, times,
            df["cp"].tolist(), df["price"].values,
            use_gh=True, q=q,
        )
        dt = time.time() - t0
        dbm.add_log("INFO",
                    f"G-H校准完成({dt:.1f}s): alpha={result['alpha']:.4f}, sigma={result['sigma']:.4f}, "
                    f"MSE={result['mse']:.6f}, BS_MSE={result['bs_mse']:.6f}, 改善={result['improvement_pct']:.1f}%")
    else:
        # 有效时间法（旧模式，兼容用）
        result = pricing.calibrate(
            S0, r,
            strikes, times,
            df["cp"].tolist(), df["price"].values,
            use_gh=False, q=q,
        )
        dt = time.time() - t0
        dbm.add_log("INFO",
                    f"ET校准完成({dt:.1f}s): alpha={result['alpha']:.4f}, sigma={result['sigma']:.4f}, "
                    f"MSE={result['mse']:.6f}, BS_MSE={result['bs_mse']:.6f}, 改善={result['improvement_pct']:.1f}%")
    return result


# ============================================================
# 步骤 5：Transformer 波动率预测
# ============================================================

def predict_vol_20(price_df):
    """
    用 Transformer 预测未来 20 日波动率。
    用最新价格重新计算特征（log_ret, rv_5/10/20/60, momentum），
    构建 60 日窗口，标准化后过模型。
    模型或训练数据缺失时回退历史波动率。
    """
    model_path = os.path.join(MODEL_DIR, "transformer_vol.pt")
    npz_path = os.path.join(DATA_DIR, "transformer_train.npz")

    fallback = compute_hist_vol(price_df)

    if not os.path.exists(model_path):
        dbm.add_log("WARN", f"Transformer 模型缺失({model_path})，回退历史波动率 {fallback:.4f}")
        return fallback, "hist_fallback"
    if not os.path.exists(npz_path):
        dbm.add_log("WARN", "训练数据缺失，回退历史波动率")
        return fallback, "hist_fallback"
    if price_df is None or len(price_df) < 80:
        dbm.add_log("WARN", "历史K线不足 80 条，回退历史波动率")
        return fallback, "hist_fallback"

    try:
        import torch
        import transformer_vol as tv
    except ImportError as e:
        dbm.add_log("ERROR", f"PyTorch 不可用: {e}，回退历史波动率")
        return fallback, "hist_fallback"

    # 特征计算（与 transformer_vol.prepare_data 一致）
    df = price_df.copy().sort_values("date").reset_index(drop=True)
    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
    for window in [5, 10, 20, 60]:
        df[f"rv_{window}"] = df["log_ret"].rolling(window).std() * np.sqrt(365)
    df["ma_20"] = df["close"].rolling(20).mean()
    df["momentum"] = df["close"] / df["ma_20"] - 1
    df = df.dropna()

    features = ["log_ret", "rv_5", "rv_10", "rv_20", "rv_60", "momentum"]
    seq_len = 60
    if len(df) < seq_len:
        dbm.add_log("WARN", "有效特征数据不足 60 日，回退历史波动率")
        return fallback, "hist_fallback"

    data = np.load(npz_path, allow_pickle=True)
    X_mean = data["X_mean"]
    X_std = data["X_std"]
    y_mean = float(data["y_mean"])
    y_std = float(data["y_std"])

    last_seq = df[features].iloc[-seq_len:].values.astype(np.float64)
    X_norm = (last_seq - X_mean) / X_std
    X_tensor = torch.tensor(X_norm[None, ...], dtype=torch.float32)

    model = tv.build_transformer(seq_len=seq_len, n_features=len(features))
    model.load_state_dict(torch.load(model_path, weights_only=True))
    model.eval()

    with torch.no_grad():
        pred_norm = model(X_tensor).item()

    pred_vol = float(pred_norm * y_std + y_mean)
    pred_vol = max(pred_vol, 0.02)  # 下限保护
    # 收缩混合：Transformer 样本外改善≈0（过拟合），向朴素复读（当日已实现）收缩，
    # 压低过拟合噪声的同时保留模型在极端期的弱信号（权重见 VOL_TF_WEIGHT）
    blend = VOL_TF_WEIGHT * pred_vol + (1.0 - VOL_TF_WEIGHT) * fallback
    dbm.add_log("INFO",
                f"波动率预测: TF原始 {pred_vol*100:.1f}% × {VOL_TF_WEIGHT:.0%} "
                f"+ 已实现 {fallback*100:.1f}% × {1 - VOL_TF_WEIGHT:.0%} = {blend*100:.1f}%")
    return blend, "blend"


# ============================================================
# 步骤 5.5：GRU 价格路径预测
# ============================================================

def predict_price_path_20(price_df):
    """GRU 预测未来 20 个交易日价格路径。模型缺失/数据不足返回 None。"""
    if price_df is None or len(price_df) < 80:
        return None
    try:
        import price_predict as pp
    except ImportError:
        return None
    dates = price_df["date"].values
    close = price_df["close"].values.astype(float)
    try:
        out = pp.predict_price_path(dates, close)
    except Exception as e:
        dbm.add_log("WARN", f"价格预测失败: {type(e).__name__}: {e}")
        return None
    if out is None:
        return None
    fut, pred, lo, hi = out
    return {
        "dates": [d.strftime("%Y-%m-%d") for d in fut],
        "prices": [round(float(x), 4) for x in pred],
        "lo": [round(float(x), 4) for x in lo],
        "hi": [round(float(x), 4) for x in hi],
    }


# ============================================================
# 步骤 6：定价 + 偏差 + 信号
# ============================================================

def price_and_signals(S0, r, options_df, calib, pred_vol, q=0.0):
    """
    模型价用校准参数 (alpha*, sigma*) 定价（校准已最小化 MSE，偏差自然归零），
    预测波动率 pred_vol 用于整体方向判断（市场隐含 σ* vs 预测 σ）。
    q 为净持有成本（分红 + 融券成本）。
    返回 (quotes, signal_summary, n_buy, n_sell)
    """
    quotes = []
    if options_df is None or len(options_df) == 0:
        return quotes, "无数据", 0, 0

    alpha = calib["alpha"] if calib else 0.95
    sigma_model = calib["sigma"] if calib else pred_vol
    # 微笑校准时用带偏斜的 IV 定价，更贴合 OTM 合约
    skew = calib.get("skew", 0.0) if calib else 0.0
    smile_coef = calib.get("smile_coef", 0.0) if calib else 0.0
    use_smile = calib is not None and "smile_coef" in calib
    df = options_df[(options_df["price"] > 0) & (options_df["T"] > 0)]

    for _, row in df.iterrows():
        K = float(row["strike"])
        T = float(row["T"])
        market = float(row["price"])
        cp = str(row["cp"])
        if use_smile:
            # 用微笑校准的 IV 模型定价
            iv_model = pricing._smile_iv(K, S0, sigma_model, skew, smile_coef)
            model = pricing.tfbsm_price_gh(S0, K, T, r, iv_model, alpha, cp, 32, q)
        else:
            # 与 G-H 校准一致：同一定价函数，避免一阶近似引入系统性偏差
            model = pricing.tfbsm_price_gh(S0, K, T, r, sigma_model, alpha, cp, 32, q)
        dev = (model - market) / market * 100.0 if market > 0 else 0.0
        # IV：真实链无 IV 时用 BS 从中间价反解；模拟链直接用生成值
        iv = float(row.get("iv", 0.0) or 0.0)
        if iv <= 0:
            iv = pricing.bs_iv(S0, K, T, r, market, cp, q)
        quotes.append({
            "cp": cp,
            "K": round(K, 4),
            "T": round(T, 6),
            "expire_date": str(row.get("expire_date", "")),
            "market_price": round(market, 4),
            "model_price": round(model, 4),
            "deviation_pct": round(dev, 2),
            "iv": round(float(iv), 4) if not np.isnan(iv) else 0.0,
            "volume": int(row.get("volume", 0) or 0),
            "spread_pct": float(row.get("spread_pct", 0.0) or 0.0),
        })

    # IV 稳健化：套利违约（反解≈0）或深度虚值尖刺（>1.5）的合约，用同到期日相邻行权价的有效 IV 填补
    for exp in {q["expire_date"] for q in quotes}:
        sub = sorted([q for q in quotes if q["expire_date"] == exp], key=lambda x: x["K"])
        valid_iv = [(q["K"], q["iv"]) for q in sub if 0.02 <= q["iv"] <= 1.5]
        for q in sub:
            if 0.02 <= q["iv"] <= 1.5:
                continue
            best_k, best_iv, best_d = None, 0.0, float("inf")
            for k, ivv in valid_iv:
                d = abs(k - q["K"])
                if d < best_d:
                    best_d, best_k, best_iv = d, k, ivv
            q["iv"] = round(best_iv, 4) if best_k is not None else 0.0

    # 信号：过滤低价噪声合约后，用"相对偏差"（减去有效合约均值）判断
    # 系统性差异（如 α 有效时间效应）被基线吸收，剩余相对偏差反映合约间相对价值
    # 模型价 > 市场价 → 相对便宜 → 买入；模型价 < 市场价 → 相对贵 → 卖出
    valid = [q for q in quotes
             if q["market_price"] >= SIGNAL_MIN_PRICE
             and abs(q["deviation_pct"]) <= SIGNAL_MAX_DEV
             and q.get("spread_pct", 0.0) <= SIGNAL_MAX_SPREAD]

    mean_dev = float(np.mean([q["deviation_pct"] for q in valid])) if valid else 0.0
    rel = [(q["deviation_pct"] - mean_dev) for q in valid]

    buys = sorted([q for q, r in zip(valid, rel) if r > SIGNAL_THRESHOLD_PCT],
                  key=lambda x: -(x["deviation_pct"] - mean_dev))
    sells = sorted([q for q, r in zip(valid, rel) if r < -SIGNAL_THRESHOLD_PCT],
                   key=lambda x: x["deviation_pct"] - mean_dev)

    # 整体方向：市场隐含波动率(校准σ*) vs 模型预测波动率
    sigma_star = calib["sigma"] if calib else pred_vol
    if sigma_star > pred_vol * 1.02:
        summary = (f"市场隐含波动率 {sigma_star*100:.1f}% 高于预测 {pred_vol*100:.1f}%"
                   f" → 期权整体偏贵，偏空")
    elif sigma_star < pred_vol * 0.98:
        summary = (f"市场隐含波动率 {sigma_star*100:.1f}% 低于预测 {pred_vol*100:.1f}%"
                   f" → 期权整体偏便宜，偏多")
    else:
        summary = (f"市场隐含波动率 {sigma_star*100:.1f}% 与预测 {pred_vol*100:.1f}% 接近"
                   f" → 整体中性（均值偏差 {mean_dev:+.1f}%）")

    dbm.add_log("INFO", f"信号: 买入 {len(buys)} 条, 卖出 {len(sells)} 条 (有效合约 {len(valid)} 条, 均值偏差 {mean_dev:+.1f}%) | {summary}")
    return quotes, summary, len(buys), len(sells)


# ============================================================
# 主流水线
# ============================================================

def is_trading_session(now=None):
    """
    判断当前是否处于 A 股期权交易时段（9:30-11:30, 13:00-15:00，工作日）。
    非交易时段（午休/盘后/周末）期权买卖盘冻结或撤单，报价错配严重，
    校准结果失真（α/σ/q 随机跳），应跳过校准，参数沿用上次交易时段结果。
    """
    from datetime import datetime as _dt
    now = now or _dt.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)


def run_pipeline():
    """执行一次完整流水线。异常不抛出，记录到状态与日志。"""
    if PIPELINE_STATE["running"]:
        dbm.add_log("WARN", "流水线正在运行中，跳过本次触发")
        return None

    # 非交易时段保护：期权报价失真，跳过校准（实时价格 tick 不受影响）
    if not is_trading_session():
        dbm.add_log("WARN",
                    f"非交易时段({datetime.now().strftime('%H:%M')})，期权报价失真，跳过流水线。"
                    f"交易时段: 9:30-11:30, 13:00-15:00")
        return None

    PIPELINE_STATE["running"] = True
    PIPELINE_STATE["last_error"] = None
    t0 = time.time()
    dbm.add_log("INFO", "=" * 50)
    dbm.add_log("INFO", "流水线开始")

    try:
        # 1. 行情
        S0, prev_close, price_df, source = fetch_market()

        # 2. 历史波动率
        hist_vol = compute_hist_vol(price_df)
        dbm.add_log("INFO", f"20日历史已实现波动率: {hist_vol:.4f} ({hist_vol*100:.1f}%)")

        # 3. Transformer 预测（先预测，市场 IV 围绕预测波动率生成，信号更均衡）
        pred_vol, vol_source = predict_vol_20(price_df)

        # 3.5 GRU 价格路径预测（模型缺失则跳过，不影响主流程）
        price_pred = predict_price_path_20(price_df)
        if price_pred:
            dbm.add_log("INFO", f"GRU 价格预测: 20日后 {price_pred['prices'][-1]:.3f} "
                                f"(区间 {price_pred['lo'][-1]:.3f}~{price_pred['hi'][-1]:.3f})")

        # 4. 期权链：优先拉取真实期权链，失败回退模拟
        options_df, option_source = build_option_chain_with_fallback(S0, base_vol=pred_vol)

        # 4.5 隐含净持有成本（真实链从 put-call parity 反解，模拟链自然为 0）
        r = DEFAULT_R
        q = estimate_holding_cost(S0, options_df, r)

        # 5. 校准
        calib = run_calibration(S0, r, options_df, q=q)

        # 6. 定价与信号（微笑校准用带偏斜的 IV 定价，更贴合 OTM）
        quotes, summary, n_buy, n_sell = price_and_signals(S0, r, options_df, calib, pred_vol, q=q)

        # 7. 保存
        snapshot_id = dbm.save_snapshot({
            "S0": round(S0, 6),
            "prev_close": round(prev_close, 6),
            "alpha": calib["alpha"] if calib else None,
            "sigma": calib["sigma"] if calib else None,
            "mse": calib["mse"] if calib else None,
            "bs_sigma": calib["bs_sigma"] if calib else None,
            "bs_mse": calib["bs_mse"] if calib else None,
            "improvement_pct": calib["improvement_pct"] if calib else None,
            "pred_vol_20": round(pred_vol, 6),
            "hist_vol_20": round(hist_vol, 6),
            "r": r,
            "n_options": len(quotes),
            "signal_summary": summary,
            "n_buy": n_buy,
            "n_sell": n_sell,
            "data_source": f"{source}|opt:{option_source}|vol:{vol_source}|cal:{calib.get('method','n/a') if calib else 'n/a'}|q:{q*100:.1f}%",
            "skew": calib.get("skew") if calib else None,
            "smile_coef": calib.get("smile_coef") if calib else None,
            "calib_method": calib.get("method") if calib else None,
            "q": round(q, 6),
            "price_pred": json.dumps(price_pred) if price_pred else None,
        })
        dbm.save_option_quotes(snapshot_id, quotes)

        duration = time.time() - t0
        PIPELINE_STATE["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        PIPELINE_STATE["last_duration"] = round(duration, 1)
        PIPELINE_STATE["snapshot_id"] = snapshot_id
        dbm.add_log("INFO",
                    f"流水线完成({duration:.1f}s): S0={S0:.4f}, "
                    f"alpha={calib['alpha'] if calib else 'N/A'}, 预测vol={pred_vol*100:.1f}%, "
                    f"快照#{snapshot_id}, 期权 {len(quotes)} 条")
        return snapshot_id
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        PIPELINE_STATE["last_error"] = f"{type(e).__name__}: {e}"
        PIPELINE_STATE["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dbm.add_log("ERROR", f"流水线异常: {type(e).__name__}: {e}")
        dbm.add_log("ERROR", tb[-500:])
        return None
    finally:
        PIPELINE_STATE["running"] = False


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    dbm.init_db()
    run_pipeline()
