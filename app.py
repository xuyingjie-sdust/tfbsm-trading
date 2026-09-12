#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TFBSM 期权交易系统 — Flask 主服务
==================================
功能：
  - REST API（供前端 ECharts 仪表盘消费）
  - APScheduler 定时任务：每小时运行一次数据流水线
  - 静态托管前端页面
  - 手动触发接口 POST /api/run_now

启动：
  python app.py                 # 默认 127.0.0.1:8000
  python app.py --port 9000     # 指定端口
"""

import os
import sys
import threading

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # PyInstaller 打包模式：数据/静态文件跟随 exe 所在目录
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
sys.path.insert(0, BASE_DIR)

import pandas as pd
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory, send_file
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import db as dbm
import pipeline

DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
scheduler = BackgroundScheduler()
_sched_started = False

# 访问令牌（可选，公网部署时设置）
ACCESS_TOKEN = None


def _check_token():
    """校验访问令牌：query 参数 token 或 X-Token 头"""
    if not ACCESS_TOKEN:
        return True
    tok = request.args.get("token", "")
    if not tok:
        tok = request.headers.get("X-Token", "")
    return tok == ACCESS_TOKEN


@app.before_request
def require_token():
    if not ACCESS_TOKEN:
        return
    path = request.path
    # 只对 API 和首页做 token 校验；静态资源（PWA/图标/JS）放行
    if path.startswith("/api/") or path == "/":
        if not _check_token():
            return jsonify({"ok": False, "error": "unauthorized"}), 401


# ============================================================
# 定时任务
# ============================================================

def scheduled_job():
    pipeline.run_pipeline()


def tick_job():
    """每分钟采集一次实时价（轻量请求，失败静默）"""
    price = pipeline.fetch_tick()
    if price:
        dbm.save_tick(price)


def start_scheduler():
    """启动定时任务：每小时流水线 + 每分钟实时价采集"""
    global _sched_started
    if _sched_started:
        return
    scheduler.add_job(
        scheduled_job,
        trigger=IntervalTrigger(hours=1),
        id="hourly_pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        tick_job,
        trigger=IntervalTrigger(seconds=60),
        id="minute_tick",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.start()
    _sched_started = True
    dbm.add_log("INFO", "定时任务已启动: 每小时流水线 + 每分钟实时价采集")

    # 首次启动且无数据 → 立即跑一次（后台线程，不阻塞服务）
    if dbm.get_snapshot_count() == 0:
        dbm.add_log("INFO", "数据库为空，触发首次流水线运行...")
        threading.Thread(target=pipeline.run_pipeline, daemon=True).start()

    # 立即采集一次实时价，避免要等 60 秒才有首个 tick
    threading.Thread(target=tick_job, daemon=True).start()

    # 回填历史日线价格到实时走势表（幂等，只有首次有效）
    threading.Thread(target=pipeline.backfill_ticks_from_history, daemon=True).start()

    # 定期清理过老 tick（每 6 小时）
    scheduler.add_job(
        dbm.prune_ticks,
        trigger=IntervalTrigger(hours=6),
        id="tick_prune",
        max_instances=1,
        coalesce=True,
    )


def next_run_time():
    try:
        job = scheduler.get_job("hourly_pipeline")
        if job and job.next_run_time:
            return job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None


# ============================================================
# API 路由
# ============================================================

@app.after_request
def add_no_cache_headers(resp):
    """禁用浏览器缓存，避免前端显示旧数据"""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/status")
def api_status():
    latest = dbm.get_latest_snapshot()
    last_run = pipeline.PIPELINE_STATE["last_run"]
    if not last_run and latest:
        last_run = latest["ts"]  # 服务重启后回退到最新快照时间
    return jsonify({
        "ok": True,
        "running": pipeline.PIPELINE_STATE["running"],
        "last_run": last_run,
        "last_duration": pipeline.PIPELINE_STATE["last_duration"],
        "last_error": pipeline.PIPELINE_STATE["last_error"],
        "next_run": next_run_time(),
        "snapshot_count": dbm.get_snapshot_count(),
        "latest_snapshot_ts": latest["ts"] if latest else None,
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/api/overview")
def api_overview():
    latest = dbm.get_latest_snapshot()
    if not latest:
        return jsonify({"ok": True, "data": None})
    chg_pct = None
    if latest.get("prev_close") and latest.get("S0"):
        chg_pct = (latest["S0"] - latest["prev_close"]) / latest["prev_close"] * 100
    return jsonify({
        "ok": True,
        "data": {
            "ts": latest["ts"],
            "S0": latest["S0"],
            "prev_close": latest["prev_close"],
            "chg_pct": round(chg_pct, 3) if chg_pct is not None else None,
            "alpha": latest["alpha"],
            "sigma": latest["sigma"],
            "mse": latest["mse"],
            "bs_sigma": latest["bs_sigma"],
            "bs_mse": latest["bs_mse"],
            "improvement_pct": latest["improvement_pct"],
            "pred_vol_20": latest["pred_vol_20"],
            "hist_vol_20": latest["hist_vol_20"],
            "r": latest["r"],
            "n_options": latest["n_options"],
            "signal_summary": latest["signal_summary"],
            "n_buy": latest["n_buy"],
            "n_sell": latest["n_sell"],
            "data_source": latest["data_source"],
            "skew": latest.get("skew"),
            "smile_coef": latest.get("smile_coef"),
            "calib_method": latest.get("calib_method"),
            "q": latest.get("q"),
        },
    })


@app.route("/api/price_history")
def api_price_history():
    """历史 K 线（腾讯拉取的 CSV，近 250 条）+ GRU 未来 20 日价格预测"""
    import json
    path = os.path.join(DATA_DIR, "etf_price_510300_tencent.csv")
    if not os.path.exists(path):
        path = os.path.join(DATA_DIR, "etf_price_510300.csv")
    try:
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").tail(250)
        latest = dbm.get_latest_snapshot()
        price_pred = None
        if latest and latest.get("price_pred"):
            try:
                price_pred = json.loads(latest["price_pred"])
            except Exception:
                price_pred = None
        return jsonify({
            "ok": True,
            "data": {
                "dates": df["date"].dt.strftime("%Y-%m-%d").tolist(),
                "open": df["open"].tolist(),
                "close": df["close"].tolist(),
                "high": df["high"].tolist(),
                "low": df["low"].tolist(),
                "volume": df["volume"].tolist(),
                "price_pred": price_pred,
            },
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/params_history")
def api_params_history():
    """alpha / sigma / MSE 改善率 时间序列"""
    rows = dbm.get_snapshot_history(limit=500)
    return jsonify({
        "ok": True,
        "data": {
            "ts": [r["ts"] for r in rows],
            "alpha": [r["alpha"] for r in rows],
            "sigma": [r["sigma"] for r in rows],
            "improvement_pct": [r["improvement_pct"] for r in rows],
            "S0": [r["S0"] for r in rows],
        },
    })


@app.route("/api/volatility")
def api_volatility():
    """预测波动率 vs 已实现波动率"""
    rows = dbm.get_snapshot_history(limit=500)
    return jsonify({
        "ok": True,
        "data": {
            "ts": [r["ts"] for r in rows],
            "pred_vol_20": [r["pred_vol_20"] for r in rows],
            "hist_vol_20": [r["hist_vol_20"] for r in rows],
        },
    })


@app.route("/api/smile")
def api_smile():
    """最新快照的波动率微笑：按到期日分组的 IV vs moneyness"""
    latest = dbm.get_latest_snapshot()
    if not latest:
        return jsonify({"ok": True, "data": None})
    quotes = dbm.get_options_for_snapshot(latest["id"])
    if not quotes:
        return jsonify({"ok": True, "data": None})

    S0 = latest["S0"] or 0
    groups = {}
    for q in quotes:
        exp = q["expire_date"]
        if exp not in groups:
            groups[exp] = {"call": [], "put": []}
        groups[exp][q["cp"]].append(q)

    smiles = []
    for exp in sorted(groups.keys()):
        calls = sorted(groups[exp]["call"], key=lambda x: x["K"])
        if len(calls) >= 2:
            smiles.append({
                "expiry": exp,
                "name": f"{exp}",
                "moneyness": [round(c["K"] / S0, 4) if S0 else c["K"] for c in calls],
                "iv": [c["iv"] for c in calls],
            })
    return jsonify({"ok": True, "data": smiles, "S0": S0})


@app.route("/api/surface")
def api_surface():
    """波动率曲面：moneyness x 到期日 x IV"""
    latest = dbm.get_latest_snapshot()
    if not latest:
        return jsonify({"ok": True, "data": None})
    quotes = dbm.get_options_for_snapshot(latest["id"])
    if not quotes:
        return jsonify({"ok": True, "data": None})

    S0 = latest["S0"] or 0
    exps = sorted({q["expire_date"] for q in quotes})
    ks = sorted({q["K"] for q in quotes})
    moneyness = [round(k / S0, 4) if S0 else k for k in ks]

    iv_mat = []
    dev_mat = []
    for exp in exps:
        iv_row, dev_row = [], []
        for k in ks:
            match = [q for q in quotes if q["expire_date"] == exp and abs(q["K"] - k) < 1e-9]
            if match:
                c = match[0]
                iv_row.append(c["iv"])
                dev_row.append(c["deviation_pct"])
            else:
                iv_row.append(None)
                dev_row.append(None)
        iv_mat.append(iv_row)
        dev_mat.append(dev_row)

    return jsonify({
        "ok": True,
        "data": {
            "expiries": exps,
            "moneyness": moneyness,
            "iv_matrix": iv_mat,
            "dev_matrix": dev_mat,
        },
    })


def _is_valid_quote(q):
    """信号有效合约过滤：价格下限 + 偏差上限 + 买卖价差上限"""
    return (q["market_price"] >= pipeline.SIGNAL_MIN_PRICE
            and abs(q["deviation_pct"]) <= pipeline.SIGNAL_MAX_DEV
            and q.get("spread_pct", 0.0) <= pipeline.SIGNAL_MAX_SPREAD)


@app.route("/api/options")
def api_options():
    """最新期权链（含偏差、信号标记）"""
    latest = dbm.get_latest_snapshot()
    if not latest:
        return jsonify({"ok": True, "data": None})
    quotes = dbm.get_options_for_snapshot(latest["id"])
    valid = [q for q in quotes if _is_valid_quote(q)]
    mean_dev = sum(q["deviation_pct"] for q in valid) / len(valid) if valid else 0.0
    for q in quotes:
        q["cn_name"] = _cn_option_name(q["cp"], q["expire_date"], q["K"])
        is_valid = _is_valid_quote(q)
        rel = q["deviation_pct"] - mean_dev
        if is_valid and rel > pipeline.SIGNAL_THRESHOLD_PCT:
            q["signal"] = "buy"
        elif is_valid and rel < -pipeline.SIGNAL_THRESHOLD_PCT:
            q["signal"] = "sell"
        else:
            q["signal"] = None
    return jsonify({"ok": True, "data": quotes, "S0": latest["S0"], "alpha": latest["alpha"],
                    "sigma": latest["sigma"], "pred_vol": latest["pred_vol_20"]})


def _cn_option_name(cp, expire_date, K):
    """生成中文合约名，如 300ETF购9月4490"""
    try:
        m = int(expire_date[5:7])
    except (ValueError, TypeError):
        m = "?"
    return f"300ETF{'购' if cp == 'call' else '沽'}{m}月{int(round(K * 1000))}"


@app.route("/api/signals")
def api_signals():
    """交易信号列表（按方向分组，带中文合约名）"""
    latest = dbm.get_latest_snapshot()
    if not latest:
        return jsonify({"ok": True, "data": None})
    quotes = dbm.get_options_for_snapshot(latest["id"])
    valid = [q for q in quotes if _is_valid_quote(q)]
    mean_dev = sum(q["deviation_pct"] for q in valid) / len(valid) if valid else 0.0
    buys, sells = [], []
    for q in quotes:
        is_valid = _is_valid_quote(q)
        rel = q["deviation_pct"] - mean_dev
        if is_valid and rel > pipeline.SIGNAL_THRESHOLD_PCT:
            q["signal"] = "buy"
            buys.append(q)
        elif is_valid and rel < -pipeline.SIGNAL_THRESHOLD_PCT:
            q["signal"] = "sell"
            sells.append(q)
    buys.sort(key=lambda x: -(x["deviation_pct"] - mean_dev))
    sells.sort(key=lambda x: x["deviation_pct"] - mean_dev)
    for q in buys + sells:
        q["cn_name"] = _cn_option_name(q["cp"], q["expire_date"], q["K"])
    return jsonify({
        "ok": True,
        "data": {"buys": buys, "sells": sells},
        "summary": latest["signal_summary"],
        "S0": latest["S0"], "alpha": latest["alpha"], "pred_vol": latest["pred_vol_20"],
    })


_surface_gif_lock = threading.Lock()
_surface_gif_cache = {"snapshot_id": None, "path": None}


@app.route("/api/surface_gif")
def api_surface_gif():
    """服务器端 matplotlib 渲染的 3D 曲面旋转 GIF（无 WebGL 依赖，全设备兼容）"""
    import surface_render
    latest = dbm.get_latest_snapshot()
    if not latest:
        return "no snapshot", 404
    with _surface_gif_lock:
        if _surface_gif_cache["snapshot_id"] != latest["id"]:
            # 磁盘已有对应快照的 GIF 则直接复用（服务重启后不重渲染）
            cached_path = os.path.join(surface_render.FIG_DIR, f"surface_3d_{latest['id']}.gif")
            if not os.path.exists(cached_path):
                path = surface_render.render_surface_gif(cached_path)
                if not path or not os.path.exists(path):
                    return "render failed", 500
            _surface_gif_cache["snapshot_id"] = latest["id"]
            _surface_gif_cache["path"] = cached_path
        return send_file(_surface_gif_cache["path"], mimetype="image/gif")


@app.route("/api/price_ticks")
def api_price_ticks():
    """分钟级实时价格序列（含历史日线回填，供可缩放的实时走势图）"""
    limit = min(int(request.args.get("limit", 8000)), 30000)
    ticks = dbm.get_ticks(limit=limit)
    return jsonify({
        "ok": True,
        "data": {
            "ts": [t["ts"] for t in ticks],
            "S0": [t["S0"] for t in ticks],
        },
    })


@app.route("/api/logs")
def api_logs():
    limit = min(int(request.args.get("limit", 100)), 500)
    logs = dbm.get_logs(limit=limit)
    return jsonify({"ok": True, "data": logs})


@app.route("/api/run_now", methods=["POST"])
def api_run_now():
    if pipeline.PIPELINE_STATE["running"]:
        return jsonify({"ok": True, "status": "already_running",
                        "message": "流水线正在运行中"})
    threading.Thread(target=pipeline.run_pipeline, daemon=True).start()
    return jsonify({"ok": True, "status": "started", "message": "流水线已在后台启动"})


# ============================================================
# 启动
# ============================================================

def _open_browser_when_ready(port):
    """后台线程：等服务端口就绪后自动打开浏览器"""
    import socket
    import time
    import webbrowser
    for _ in range(90):
        time.sleep(1)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    webbrowser.open(f"http://127.0.0.1:{port}")
                    return
        except Exception:
            pass


def _port_in_use(port):
    """检测端口是否已被占用（避免多进程同端口堆积导致新旧代码混跑）"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main():
    global ACCESS_TOKEN
    port = 8000
    host = "127.0.0.1"
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass
    if "--host" in sys.argv:
        try:
            host = sys.argv[sys.argv.index("--host") + 1]
        except IndexError:
            pass
    if "--token" in sys.argv:
        try:
            ACCESS_TOKEN = sys.argv[sys.argv.index("--token") + 1]
        except IndexError:
            pass

    dbm.init_db()
    os.makedirs(STATIC_DIR, exist_ok=True)

    # 端口占用保护：已有服务在运行时直接退出，避免多进程新旧代码混跑
    if _port_in_use(port):
        print(f"端口 {port} 已被占用，可能已有 TFBSM 服务在运行。")
        print(f"请直接访问 http://127.0.0.1:{port}（无需重复启动）")
        return

    print("=" * 60)
    print("TFBSM 期权交易系统")
    print(f"  前端: http://{host}:{port}" + (f"/?token={ACCESS_TOKEN}" if ACCESS_TOKEN else ""))
    print(f"  定时任务: 每 1 小时拉取数据并预测")
    if ACCESS_TOKEN:
        print(f"  访问令牌: 已启用")
    print("=" * 60)

    start_scheduler()

    # 便携版：自动打开浏览器（等端口就绪后）
    if "--open-browser" in sys.argv:
        threading.Thread(target=_open_browser_when_ready, args=(port,), daemon=True).start()
        print(f"  自动打开浏览器: 已启用")

    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
