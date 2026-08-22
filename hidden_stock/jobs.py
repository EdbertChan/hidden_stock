import dagster as dg
from dagster_celery import celery_executor

# Broker is Compose service DNS name inside the stack; remotes use DO1 public IP.
do1_celery_executor = celery_executor.configured(
    {
        "broker": {"env": "DAGSTER_CELERY_BROKER_URL"},
        "backend": {"env": "DAGSTER_CELERY_BACKEND_URL"},
    }
)

full_pipeline_job = dg.define_asset_job(
    name="full_pipeline_job",
    selection=dg.AssetSelection.all() - dg.AssetSelection.groups("ops"),
)

equity_holdings_job = dg.define_asset_job(
    name="equity_holdings_job",
    selection=dg.AssetSelection.assets(
        "equity_holdings", "equity_holdings_history", "equity_holdings_export"
    ),
)

celery_smoke_job = dg.define_asset_job(
    name="celery_smoke_job",
    selection=dg.AssetSelection.assets("do1_smoke"),
    executor_def=do1_celery_executor,
)
