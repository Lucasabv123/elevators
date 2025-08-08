import os
import pandas as pd
import sqlalchemy as sa
import re

HERE        = os.path.dirname(os.path.abspath(__file__))
EXCEL_FILE  = os.path.join(HERE, "Elevators.xlsx")
DB_FILE     = os.path.join(HERE, "elevators.db")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_FILE}")
engine      = sa.create_engine(DATABASE_URL)


WEIGHT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)")

def load_excel_to_db(excel_path=EXCEL_FILE, engine=engine):
    # Read unified PartsRules sheet
    parts_df = pd.read_excel(excel_path, sheet_name="PartsRules")

    # Cast numeric price columns
    for col in ("costo", "venta", "iva"):
        parts_df[col] = parts_df[col].astype(float)

    # Parse 'weight' text (e.g. "1000kg") if the column exists
    if "weight" in parts_df.columns:
        parts_df['unit_weight'] = (
            parts_df['weight'].astype(str)
            .str.extract(WEIGHT_PATTERN, expand=False)
            .astype(float)
        )
    else:
        # if no weight column, initialize with zeros
        parts_df['unit_weight'] = 0.0

    # Drop rows missing critical data
    parts_df = parts_df.dropna(subset=["part_id", "qty_formula", "condition_expr"])

    # Persist to SQLite
    with engine.begin() as conn:
        parts_df.to_sql("parts_rules", conn, if_exists="replace", index=False)

    print(f"✅ Loaded {len(parts_df)} parts into '{engine.url}'")

if __name__ == "__main__":
    load_excel_to_db()
