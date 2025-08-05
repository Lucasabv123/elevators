import os
import pandas as pd
import sqlite3
import re

HERE        = os.path.dirname(os.path.abspath(__file__))
EXCEL_FILE  = os.path.join(HERE, "Elevators.xlsx")
DB_FILE     = os.path.join(HERE, "elevators.db")

WEIGHT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)")

def load_excel_to_db(excel_path=EXCEL_FILE, db_path=DB_FILE):
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
    conn = sqlite3.connect(db_path)
    parts_df.to_sql("parts_rules", conn, if_exists="replace", index=False)
    conn.close()

    print(f"✅ Loaded {len(parts_df)} parts into '{db_path}'")

if __name__ == "__main__":
    load_excel_to_db()
