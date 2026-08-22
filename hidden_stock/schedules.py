import dagster as dg

from .jobs import full_pipeline_job

daily_refresh_schedule = dg.ScheduleDefinition(
    name="daily_refresh_schedule",
    cron_schedule="30 21 * * 1-5",  # 21:30 UTC, weekdays — after US market close
    job=full_pipeline_job,
)
