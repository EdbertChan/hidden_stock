import os

import dagster as dg

from .assets import (
    backtest,
    business_description,
    buybacks,
    candidates,
    equity_holdings,
    final_table,
    index_membership,
    insider_ownership,
    lifo_fifo,
    smoke,
    universe,
    valuation,
)
from .jobs import celery_smoke_job, equity_holdings_job, full_pipeline_job
from .resources.db_resource import DBResource
from .resources.edgar_resource import EdgarResource
from .resources.equity_holdings_settings import EquityHoldingsSettings
from .resources.gemini_resource import GeminiResource
from .resources.reconstitution_resource import ReconstitutionResource
from .resources.universe_resource import UniverseResource
from .schedules import daily_refresh_schedule

all_assets = dg.load_assets_from_modules(
    [
        universe,
        index_membership,
        valuation,
        candidates,
        lifo_fifo,
        business_description,
        insider_ownership,
        buybacks,
        equity_holdings,
        final_table,
        backtest,
        smoke,
    ]
)

# Local path so Celery remotes (no Docker /opt/dagster) can materialize outputs.
_storage_dir = os.environ.get("DAGSTER_STORAGE_DIR", "/tmp/dagster_storage")

defs = dg.Definitions(
    assets=all_assets,
    jobs=[full_pipeline_job, celery_smoke_job, equity_holdings_job],
    schedules=[daily_refresh_schedule],
    resources={
        "io_manager": dg.FilesystemIOManager(base_dir=_storage_dir),
        "db": DBResource(),
        "edgar": EdgarResource(),
        "llm": GeminiResource(),
        "universe": UniverseResource(),
        "reconstitution": ReconstitutionResource(),
        "equity_holdings_settings": EquityHoldingsSettings(),
    },
)
