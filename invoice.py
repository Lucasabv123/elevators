# invoice.py
import sqlite3
import pandas as pd
import numexpr as ne

DB_PATH = "elevators.db"

def load_tables():
    conn     = sqlite3.connect(DB_PATH)
    models   = pd.read_sql("SELECT * FROM models", conn)
    parts    = pd.read_sql("SELECT * FROM parts_rules", conn)
    conn.close()
    return models, parts

def choose_model(models_df):
    print("\nAvailable models:")
    for idx, row in models_df.iterrows():
        print(f"  [{idx}] {row.model_id} — capacity {row.max_persons}p, "
              f"{row.max_floors}f, base ${float(row.base_price):,.2f}")
    while True:
        sel = input("Enter the number in [ ] for the model you want: ")
        if sel.isdigit() and int(sel) in models_df.index:
            return models_df.loc[int(sel)]
        print("↳ Invalid choice; try again.")

def compute_invoice(model_row, persons, floors, trans_cost, tech_days, tech_rate, elec_days, elec_rate, parts_df):
    lines = []
    total = float(model_row.base_price)

    # Base price
    lines.append(("Base price", 1, total))

    # Parts by formula
    for _, part in parts_df.iterrows():
        qty = int(ne.evaluate(part.qty_formula, {"P": persons, "F": floors}))
        if qty <= 0:
            continue
        unit_price = float(part.unit_price)
        lines.append((part.description, qty, unit_price))
        total += qty * unit_price

    # Transporte
    lines.append(("Transporte", 1, trans_cost));      total += trans_cost
    # Técnico
    lines.append(("Técnico (montaje)", tech_days, tech_rate)); total += tech_days * tech_rate
    # Eléctrico
    lines.append(("Eléctrico (montaje)", elec_days, elec_rate)); total += elec_days * elec_rate

    return lines, total

if __name__ == "__main__":
    # 1) Gather your inputs
    persons    = int(input("Number of persons: "))
    floors     = int(input("Number of floors: "))
    trans_cost = float(input("Transporte cost: "))
    tech_days  = int(input("Técnico days: "))
    tech_rate  = float(input("Técnico daily rate: "))
    elec_days  = int(input("Eléctrico days: "))
    elec_rate  = float(input("Eléctrico daily rate: "))

    # 2) Load DB tables
    models_df, parts_df = load_tables()

    # 3) Prompt for which model to use
    model_row = choose_model(models_df)

    # 4) Compute the invoice
    lines, grand_total = compute_invoice(
        model_row, persons, floors,
        trans_cost, tech_days, tech_rate,
        elec_days, elec_rate, parts_df
    )

    # 5) Print it out neatly
    print(f"\nInvoice for Model {model_row.model_id}\n" + "-"*50)
    for desc, qty, price in lines:
        print(f"{desc:25s} x{qty:2d} @ ${price:8.2f} = ${qty*price:8.2f}")
    print("-"*50)
    print(f"{'Grand Total':25s}     ${grand_total:8.2f}")
