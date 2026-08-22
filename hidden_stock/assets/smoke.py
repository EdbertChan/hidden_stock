"""Tiny always-on smoke asset for DO1 deploy verification."""

from datetime import datetime, timezone

import pandas as pd
from dagster import AssetExecutionContext, asset


@asset(group_name="ops")
def do1_smoke(context: AssetExecutionContext) -> pd.DataFrame:
    """Writes one row proving Dagster ran on the DO1 Compose stack."""
    now = datetime.now(timezone.utc).isoformat()
    df = pd.DataFrame([{"smoke": "ok", "ts_utc": now, "host_hint": "do1-compose"}])
    context.add_output_metadata({"ts_utc": now, "rows": 1})
    context.log.info("do1_smoke ok at %s", now)
    return df
