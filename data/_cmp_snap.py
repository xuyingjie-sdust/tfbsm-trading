# -*- coding: utf-8 -*-
"""对比快照 46/47 数据质量（用后即删）"""
import sys
import sqlite3
import statistics

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
c = sqlite3.connect("trading_system.db")
c.row_factory = sqlite3.Row
for sid in [46, 47]:
    rows = [dict(r) for r in c.execute(
        "SELECT cp,K,T,market_price,iv,deviation_pct,spread_pct FROM option_quotes WHERE snapshot_id=?", (sid,)).fetchall()]
    ivs = [r["iv"] for r in rows if r["iv"] and r["iv"] > 0.01]
    sprs = [r["spread_pct"] or 0 for r in rows]
    print(f"=== 快照#{sid} ===")
    print(f"  合约 {len(rows)}, IV: min {min(ivs)*100:.1f}% / med {statistics.median(ivs)*100:.1f}% / max {max(ivs)*100:.1f}%")
    print(f"  spread_pct: med {statistics.median(sprs)*100:.2f}% / max {max(sprs)*100:.1f}% / >15%: {sum(1 for s in sprs if s>0.15)} 条")
    atm = [r for r in rows if abs(r["K"] - 4.7) < 1e-9]
    for r in atm[:2]:
        print(f"  ATM {r['cp']} K=4.7: mid={r['market_price']}, iv={r['iv']:.4f}, spread={r['spread_pct']*100:.2f}%")
c.close()
