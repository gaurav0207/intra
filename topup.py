"""One-off: add paper capital to the live ledger.

Raises both cash and initial_cash so the top-up is never counted as profit.
"""

import datetime
import json
import sys

ADD = float(sys.argv[1]) if len(sys.argv) > 1 else 100_000.0
PATH = "/data/paper_trades.json"

with open(PATH) as handle:
    state = json.load(handle)

before_cash = float(state["cash"])
before_initial = float(state["initial_cash"])
state["cash"] = round(before_cash + ADD, 2)
state["initial_cash"] = round(before_initial + ADD, 2)
state.setdefault("events", []).append(
    {
        "time": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(),
        "type": "CAPITAL_TOPUP",
        "amount": ADD,
        "cash_after": state["cash"],
        "initial_cash_after": state["initial_cash"],
    }
)

with open(PATH, "w") as handle:
    json.dump(state, handle, indent=2)

print(f"cash        {before_cash:,.2f} -> {state['cash']:,.2f}")
print(f"initial     {before_initial:,.2f} -> {state['initial_cash']:,.2f}")
print(f"open positions: {len(state['open_positions'])}")
