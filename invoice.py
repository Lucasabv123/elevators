# invoice.py
#!/usr/bin/env python3
import sqlalchemy as sa
import os
import pandas as pd
from math import ceil
import re

DB = os.path.join(os.path.dirname(__file__), "elevators.db")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB}")
engine = sa.create_engine(DATABASE_URL)
WEIGHT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)")

def load_parts(engine=engine):
    with engine.connect() as conn:
        df = pd.read_sql("SELECT * FROM parts_rules", conn)
    df.columns = [c.strip() for c in df.columns]
    return df

def safe_eval(expr, ctx):
    try:
        return eval(expr, {"__builtins__": None}, {**ctx, "ceil": ceil})
    except Exception:
        return False

def main():
    # Gather inputs
    P            = int(input("How many persons? "))
    F            = int(input("How many floors?  "))
    machine_room = input("¿Con cuarto de máquinas? (y/n): ").strip().lower().startswith("y")
    door         = input("Puertas — manuales (m) o automáticas (a)? ").strip().lower()
    door_type    = "manual" if door.startswith("m") else "automatica"
    control_type = input("Monarch o Heytech? ").strip().title()
    encoder      = False
    if control_type == "Heytech":
        encoder = input("¿Con encoder? (y/n): ").strip().lower().startswith("y")
    gearless = not machine_room or input("¿Motor gearless? (y/n): ").strip().lower().startswith("y")
    if P <= 4 and F <= 3:
        include_cyl = input("¿Incluir cilindro hidráulico BTD-55? (y/n): ")\
                      .strip().lower().startswith("y")
    else:
        include_cyl = False
  

    # Build context
    ctx = {
        "P": P, "F": F,
        "machine_room": machine_room,
        "door_type": door_type,
        "control_type": control_type,
        "encoder": encoder,
        "gearless": gearless,
        "hydraulic_cylinder": include_cyl
    }

    # Load and filter parts
    parts_df = load_parts()
    mask = parts_df["condition_expr"].apply(lambda e: safe_eval(str(e), ctx))
    rules = parts_df[mask]

    # Compute capacity from motor weight
    motor_rows = rules[rules["unit_weight"].notna()]
    if not motor_rows.empty:
        capacities = motor_rows["unit_weight"] * motor_rows["qty_formula"].astype(float)
        capacity = int(capacities.max())
        print(f"Capacity (max load): {capacity} kg")
    else:
        print("Capacity (max load): n/a")

    # Compute line items
    lines = []
    total = 0.0
    for _, part in rules.iterrows():
        qty = int(safe_eval(str(part["qty_formula"]), ctx) or 0)
        if qty <= 0:
            continue
        up = float(part["iva"])
        lines.append((part["description"], qty, up))
        total += qty * up

    # Print invoice
    print("\nItemized Invoice\n" + "-"*60)
    for desc, qty, up in lines:
        print(f"{desc:35s} x{qty:3d} @ ${up:8.2f} = ${qty*up:8.2f}")
    print("-"*60)
    print(f"{'Grand Total':35s}     ${total:10.2f}")

if __name__ == "__main__":
    main()