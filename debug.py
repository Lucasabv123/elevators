import sqlite3, pandas as pd

conn = sqlite3.connect("elevators.db")
df   = pd.read_sql("SELECT model_id, max_persons, max_floors, base_price FROM models", conn)
conn.close()

print(df.to_string(index=False))