"""Produces reports/evidence.json: a DIP ×5 bundle traced leg by leg (used by the acceptance report)."""
import json
import os
from decimal import Decimal
from pathlib import Path

from solbot.orders import OrderParams

from .conftest import wait_for


async def test_write_evidence(harness, tmp_path):
    app = await harness.start()
    ch = harness.chain
    order = await app.orders.create("DIP", str(ch.mint), OrderParams(
        sol_per_buy=Decimal("0.1"), count=5, slippage_pct=Decimal(10), tp_pct=Decimal(20), sl_pct=Decimal(10),
        dip_pct=Decimal(10)), None)
    ch.move_price_to(0.85)
    o = await wait_for(lambda: (lambda o: o if o["status"] == "FILLED" else None)(app.db.get("orders", order["id"])))
    sub = app.db.get("submissions", o["submission_id"])
    legs = app.db.query("SELECT idx,signature,status,planned_out_raw,actual_token_delta,max_in_lamports,"
                        "swap_lamports,planned_price,exec_price,limit_price,fee_lamports,tip_paid_lamports,"
                        "length(raw_b64)*3/4 AS tx_bytes FROM sub_txs WHERE submission_id=? ORDER BY idx", (sub["id"],))
    pos = app.positions.open_position(str(ch.mint))
    keep = ("status", "sim_method", "sim_detail", "bundle_id", "verify_level", "snapshot_age_ms", "tip_lamports",
            "priority_micro_lamports", "last_valid_block_height")
    ev = {"order": {k: o[k] for k in ("id", "kind", "count", "reference_price", "trigger_price", "dip_pct",
                                      "status", "tp_pct", "sl_pct")},
          "submission": {k: sub[k] for k in keep},
          "timings_ms": {k: round((sub[k] - sub["t_signal"]) * 1000, 2) for k in
                         ("t_planned", "t_signed", "t_simulated", "t_sent", "t_ack", "t_landed", "t_verified")
                         if sub[k]},
          "legs": legs,
          "position": {k: pos[k] for k in ("token_raw", "cost_lamports", "rent_lamports", "tp_pct", "sl_pct",
                                           "tpsl_enabled")}}
    for l in legs:
        assert Decimal(l["exec_price"]) <= Decimal(l["limit_price"])
    # only scripts/acceptance_report.py publishes evidence into the repo; plain test runs write to a temp dir
    out = Path(os.environ.get("SOLBOT_EVIDENCE_PATH") or tmp_path / "evidence.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ev, indent=1, default=str))
