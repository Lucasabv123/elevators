import os
import pandas as pd
import sqlalchemy as sa
import re
import sqlite3  # 👈 add this

HERE        = os.path.dirname(os.path.abspath(__file__))
EXCEL_FILE  = os.path.join(HERE, "Elevators.xlsx")
DB_FILE     = os.path.join(HERE, "elevators.db")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_FILE}")
engine      = sa.create_engine(DATABASE_URL)

WEIGHT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)")

def load_excel_to_db(excel_path=EXCEL_FILE, engine=engine):
    parts_df = pd.read_excel(excel_path, sheet_name="PartsRules")

    # Cast numeric columns safely
    for col in ("costo", "venta", "iva"):
        parts_df[col] = pd.to_numeric(parts_df[col], errors="coerce").fillna(0.0)

    # Extract weight if present
    if "weight" in parts_df.columns:
        parts_df["unit_weight"] = (
            parts_df["weight"].astype(str)
            .str.extract(WEIGHT_PATTERN, expand=False)
            .astype(float)
        )
    else:
        parts_df["unit_weight"] = 0.0

    # Drop incomplete rows
    parts_df = parts_df.dropna(subset=["part_id", "qty_formula", "condition_expr"])

    # ✅ Persist
    backend = engine.url.get_backend_name()
    if backend == "sqlite":
        # Use the raw sqlite3 connection to avoid pandas/SQLAlchemy 2.x cursor issues
        db_path = engine.url.database  # absolute path to elevators.db
        os.makedirs(os.path.dirname(db_path or "."), exist_ok=True)
        with sqlite3.connect(db_path) as con:
            parts_df.to_sql("parts_rules", con, if_exists="replace", index=False)
    else:
        # For Postgres (Render), a SQLAlchemy connection is fine
        with engine.begin() as conn:
            parts_df.to_sql("parts_rules", conn, if_exists="replace", index=False)

    print(f"✅ Loaded {len(parts_df)} parts into '{engine.url}'")

if __name__ == "__main__":
    load_excel_to_db()

