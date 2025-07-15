# etl.py
import os
import pandas as pd
import sqlite3

HERE       = os.path.dirname(os.path.abspath(__file__))
EXCEL_FILE = os.path.join(HERE, "Elevators.xlsx")
DB_FILE    = os.path.join(HERE, "elevators.db")

def load_excel_to_db(excel_path=EXCEL_FILE, db_path=DB_FILE):
    print(f"Loading from {excel_path!r} → database {db_path!r}")

    # 1) Read sheets directly
    models_df      = pd.read_excel(excel_path, sheet_name="Models")
    parts_rules_df = pd.read_excel(excel_path, sheet_name="PartsRules")

    # 2) Ensure numeric types
    models_df["max_persons"] = models_df["max_persons"].astype(int)
    models_df["max_floors"]  = models_df["max_floors"].astype(int)
    models_df["base_price"]  = models_df["base_price"].astype(float)

    parts_rules_df["unit_price"] = parts_rules_df["unit_price"].astype(float)
    # qty_formula stays as text

    # 3) Drop incomplete rows
    models_df      = models_df.dropna(subset=["model_id", "max_persons", "max_floors", "base_price"])
    parts_rules_df = parts_rules_df.dropna(subset=["unit_price", "qty_formula"])

    # 4) Write to SQLite
    conn = sqlite3.connect(db_path)
    models_df.to_sql("models", conn, if_exists="replace", index=False)
    parts_rules_df.to_sql("parts_rules", conn, if_exists="replace", index=False)
    conn.close()

    print(f"✅ Loaded {len(models_df)} models and {len(parts_rules_df)} parts into '{db_path}'")

if __name__ == "__main__":
    load_excel_to_db()
