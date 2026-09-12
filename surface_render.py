#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务器端 3D 波动率曲面渲染（matplotlib，无 WebGL 依赖）
=====================================================
为前端生成旋转动画 GIF，任何设备/浏览器均可直接显示图片。
数据来源：SQLite 最新快照的期权报价（IV 反解后按 到期日 x 行权价 排列）。

用法：
  python surface_render.py            # 手动生成一次
  或由 app.py 的 /api/surface_gif 懒调用
"""
import os
import sys
import io
import numpy as np

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
FIG_DIR = os.path.join(BASE_DIR, "figures")
os.makedirs(FIG_DIR, exist_ok=True)

# 与前端一致的 9 段高对比色带（深蓝→青→绿→黄→橙→红）
_SURFACE_COLORS = ["#0d47a1", "#1565c0", "#2196f3", "#00e5ff",
                   "#76ff03", "#ffea00", "#ff9800", "#f44336", "#b71c1c"]
SURFACE_CMAP = LinearSegmentedColormap.from_list("tfbsm_surface", _SURFACE_COLORS)

GIF_PATH = os.path.join(FIG_DIR, "surface_3d.gif")
N_FRAMES = 24   # 360 度一圈的帧数
FIG_SIZE = (7.2, 4.8)
DPI = 96
IV_DISPLAY_CAP = 0.60   # 展示用 IV 上限（深度虚值尖刺截断，避免曲面扭曲）


def _load_surface_from_db():
    """从 SQLite 读最新快照，构造 (expiries, moneyness, iv_matrix)"""
    import sqlite3
    conn = sqlite3.connect(os.path.join(BASE_DIR, "data", "trading_system.db"))
    conn.row_factory = sqlite3.Row
    snap = conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if not snap:
        conn.close()
        return None
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM option_quotes WHERE snapshot_id=?", (snap["id"],)).fetchall()]
    conn.close()
    S0 = snap["S0"] or 0
    if not rows or not S0:
        return None
    exps = sorted({r["expire_date"] for r in rows})
    ks = sorted({r["K"] for r in rows})
    iv_mat = np.full((len(exps), len(ks)), np.nan)
    for r in rows:
        i, j = exps.index(r["expire_date"]), ks.index(r["K"])
        iv_mat[i, j] = r["iv"] if r["iv"] else np.nan
    moneyness = [round(k / S0, 3) for k in ks]
    return exps, moneyness, iv_mat


def render_surface_gif(path=None, n_frames=N_FRAMES):
    """
    渲染旋转 3D 曲面 GIF（平滑插值 + 极端 IV 截断，教科书式样）。
    返回文件路径；数据缺失返回 None。
    """
    data = _load_surface_from_db()
    if data is None:
        return None
    exps, moneyness, iv_mat = data

    # 1) 展示截断：深度虚值合约反解 IV 失真严重（可达 70%+），截断避免曲面被尖刺扭曲
    iv_capped = np.clip(iv_mat, 0.01, IV_DISPLAY_CAP)

    # 2) 平滑插值：4 到期日 x 15 行权价的稀疏网格 -> 细网格，cubic 插值
    from scipy.interpolate import griddata
    ny, nx = iv_capped.shape
    y0, x0 = np.meshgrid(np.arange(ny), np.arange(nx), indexing="ij")
    pts, vals = [], []
    for i in range(ny):
        for j in range(nx):
            if np.isfinite(iv_capped[i, j]):
                pts.append((x0[i, j], y0[i, j]))
                vals.append(iv_capped[i, j])
    if len(pts) < 4:
        return None
    xi = np.linspace(0, nx - 1, 60)
    yi = np.linspace(0, ny - 1, 32)
    Xi, Yi = np.meshgrid(xi, yi)
    Zi = griddata(np.array(pts), np.array(vals), (Xi, Yi), method="cubic")
    Zi = np.nan_to_num(Zi, nan=np.nanmedian(vals))
    money_xi = np.interp(xi, np.arange(nx), np.asarray(moneyness, dtype=float))
    Xm, Ym = np.meshgrid(money_xi, yi)

    Zp = Zi * 100.0  # 转百分比
    vmin = float(np.nanmin(Zp))
    vmax = float(np.nanmax(Zp))
    if vmax - vmin < 1e-6:
        vmax = vmin + 1.0

    ylabels = [e[5:] for e in exps]
    ytick_pos = np.arange(len(exps))

    frames = []
    fig = plt.figure(figsize=FIG_SIZE, dpi=DPI)
    ax = fig.add_subplot(111, projection="3d")
    fig.patch.set_facecolor("#161b22")
    ax.set_facecolor("#161b22")

    for frame in range(n_frames):
        azim = frame * 360.0 / n_frames
        ax.clear()
        surf = ax.plot_surface(Xm, Ym, Zp, cmap=SURFACE_CMAP,
                               vmin=vmin, vmax=vmax, linewidth=0,
                               antialiased=True, rcount=60, ccount=40)
        ax.view_init(elev=26, azim=azim)
        ax.set_xlabel("K/S0", fontsize=9, color="#8b949e")
        ax.set_ylabel("Expiry", fontsize=9, color="#8b949e")
        ax.set_zlabel("IV %", fontsize=9, color="#8b949e")
        ax.set_yticks(ytick_pos)
        ax.set_yticklabels(ylabels, fontsize=8, color="#8b949e")
        ax.tick_params(axis="x", labelsize=8, colors="#8b949e")
        ax.tick_params(axis="z", labelsize=8, colors="#8b949e")
        ax.grid(color="#2b3445", alpha=0.4)
        ax.set_title(f"IV Surface (azimuth {azim:.0f} deg)", fontsize=10, color="#c9d1d9", pad=2)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
        buf.seek(0)
        frames.append(plt.imread(buf))

    plt.close(fig)

    out = path or GIF_PATH
    import matplotlib.animation as anim_mod  # noqa
    try:
        from PIL import Image
        imgs = [Image.fromarray((f * 255).astype(np.uint8)) for f in frames]
        imgs[0].save(out, save_all=True, append_images=imgs[1:],
                     duration=100, loop=0, optimize=True)
    except ImportError:
        # 无 PIL 时用 matplotlib 动画写 GIF（依赖 imagemagick，可能失败）
        from matplotlib.animation import FuncAnimation
        fig2 = plt.figure(figsize=FIG_SIZE, dpi=DPI)
        def _upd(i):
            ax2 = fig2.axes[0] if fig2.axes else fig2.add_subplot(111)
            ax2.clear()
            ax2.imshow(frames[i % n_frames])
            ax2.axis("off")
        _upd(0)
        anim = FuncAnimation(fig2, _upd, frames=n_frames, interval=100)
        anim.save(out, writer="pillow")
        plt.close(fig2)
    return out


if __name__ == "__main__":
    p = render_surface_gif()
    print("GIF:", p if p else "FAILED (no data)")
