import dagster as dg
import pandas as pd
from sqlalchemy import create_engine, inspect, text

SCHEMA = "stock_data"


class DBResource(dg.ConfigurableResource):
    host: str = dg.EnvVar("POSTGRES_HOST")
    port: str = dg.EnvVar("POSTGRES_PORT")
    user: str = dg.EnvVar("POSTGRES_USER")
    password: str = dg.EnvVar("POSTGRES_PASSWORD")
    database: str = dg.EnvVar("POSTGRES_DB")

    def get_engine(self):
        url = f"postgresql+psycopg2://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text(f"create schema if not exists {SCHEMA}"))
            conn.commit()
        return engine

    def read_table_if_exists(self, table_name: str) -> pd.DataFrame:
        engine = self.get_engine()
        if not inspect(engine).has_table(table_name, schema=SCHEMA):
            return pd.DataFrame()
        return pd.read_sql(f"select * from {SCHEMA}.{table_name}", engine)

    def ensure_columns(self, table_name: str, columns: dict[str, str]) -> None:
        """Add missing columns (name -> SQL type) so appends after schema
        upgrades do not fail. No-op if the table does not exist yet."""
        engine = self.get_engine()
        if not inspect(engine).has_table(table_name, schema=SCHEMA):
            return
        existing = {c["name"] for c in inspect(engine).get_columns(table_name, schema=SCHEMA)}
        with engine.begin() as conn:
            for name, sql_type in columns.items():
                if name not in existing:
                    conn.execute(
                        text(f"ALTER TABLE {SCHEMA}.{table_name} ADD COLUMN {name} {sql_type}")
                    )
