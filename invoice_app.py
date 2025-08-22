#!/usr/bin/env python3
import etl
import os
import io
import re
import sqlite3
import pandas as pd
import streamlit as st
from math import ceil
from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm
import sqlalchemy as sa
from PIL import Image
import os, base64
from sqlalchemy import text
import math, re
from uuid import uuid4
import numpy as np
import re

# ── Configuration ─────────────────────────────────────────────────────────────
HERE     = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(HERE, "elevators.db")
TEMPLATE_TRACTION  = os.path.join(HERE, "invoice_template.docx")
TEMPLATE_HYDRAULIC = os.path.join(HERE, "invoice_template_hydraulic.docx")
IMG_DIR  = os.path.join(HERE, "images")
LOGO_PATH = os.path.join(IMG_DIR, "logo.png")   # <— define it here
BAD_WORDS_RE = re.compile(r"\b(?:bancada|soporte|base|montaje|kit|accesorio)\b", re.IGNORECASE)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")
engine   = sa.create_engine(DATABASE_URL)


def _ensure_invoice_images_category_column():
    # Add a TEXT 'category' column if missing (works for SQLite & Postgres)
    try:
        with engine.begin() as conn:
            insp = sa.inspect(conn)
            cols = {c["name"] for c in insp.get_columns("invoice_images")}
            if "category" not in cols:
                conn.execute(text("ALTER TABLE invoice_images ADD COLUMN category TEXT"))
    except Exception:
        # ignore if it already exists or DB doesn’t support ALTER in this context
        pass

def _run_schema():
    from sqlalchemy import text
    d = engine.dialect.name  # 'postgresql' or 'sqlite' (or others)

    if d == "postgresql":
        parts_rules_sql = """
        CREATE TABLE IF NOT EXISTS parts_rules (
            part_id TEXT,
            description TEXT,
            qty_formula TEXT,
            condition_expr TEXT,
            costo DOUBLE PRECISION,
            venta DOUBLE PRECISION,
            iva DOUBLE PRECISION,
            unit_weight DOUBLE PRECISION
        )"""
        invoices_sql = """
        CREATE TABLE IF NOT EXISTS invoices (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            customer TEXT,
            ubicacion TEXT,
            persons INTEGER,
            floors INTEGER,
            control_type TEXT,
            door_type TEXT,
            machine_room BOOLEAN,
            hydraulic BOOLEAN,
            grand_venta DOUBLE PRECISION,
            grand_total DOUBLE PRECISION,
            filename TEXT,
            file_bytes BYTEA
        )"""

        images_sql = """
        CREATE TABLE IF NOT EXISTS invoice_images (
          id BIGSERIAL PRIMARY KEY,
          invoice_id BIGINT NOT NULL,
          title TEXT,
          description TEXT,
          category TEXT,
          image_bytes BYTEA
        )"""
    else:  # SQLite (default local)
        parts_rules_sql = """
        CREATE TABLE IF NOT EXISTS parts_rules (
            part_id TEXT,
            description TEXT,
            qty_formula TEXT,
            condition_expr TEXT,
            costo REAL,
            venta REAL,
            iva REAL,
            unit_weight REAL
        )"""
        invoices_sql = """
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            customer TEXT,
            ubicacion TEXT,
            persons INTEGER,
            floors INTEGER,
            control_type TEXT,
            door_type TEXT,
            machine_room INTEGER,
            hydraulic INTEGER,
            grand_venta REAL,
            grand_total REAL,
            filename TEXT,
            file_bytes BLOB
        )"""
        images_sql = """
        CREATE TABLE IF NOT EXISTS invoice_images (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          invoice_id INTEGER NOT NULL,
          title TEXT,
          description TEXT,
          category TEXT
          image_bytes BLOB
        )"""

    with engine.begin() as conn:
        conn.execute(text(parts_rules_sql))
        conn.execute(text(invoices_sql))
        conn.execute(text(images_sql))
    _ensure_invoice_images_category_column()
    

# regex for weight fallback
WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kg", flags=re.IGNORECASE)

MOTOR_GROUPS = {
    "AKIS-VF3X 9KW": "double_drum_990",
    "AKIS-ZF603KW/DT": "double_drum_250",
}

@st.cache_resource
def init_db():
    _run_schema()
    return True

DB_READY = init_db()
#helpers
def _blob(x):
    # Postgres returns memoryview for BYTEA; SQLite returns bytes
    try:
        if isinstance(x, memoryview):
            return x.tobytes()
    except NameError:
        pass
    return x

def _images_has_category() -> bool:
    try:
        insp = sa.inspect(engine)
        cols = {c["name"] for c in insp.get_columns("invoice_images")}
        return "category" in cols
    except Exception:
        return False
    
def load_parts():
    with engine.connect() as conn:
        df = pd.read_sql("SELECT * FROM parts_rules", conn)
    df.columns = [c.strip() for c in df.columns]
    return df
def save_invoice_blob(
    customer, ubicacion, P, F, control, door_key, machine, hydraulic_cylinder,
    grand_venta, grand_total, filename, file_bytes
    ) -> int:
     with engine.begin() as conn:
        d = engine.dialect.name
        params = dict(
            customer=customer or "",
            ubicacion=ubicacion or "",
            persons=int(P or 0),
            floors=int(F or 0),
            control_type=control,
            door_type=door_key,
            machine_room=bool(machine),
            hydraulic=bool(hydraulic_cylinder),
            grand_venta=float(str(grand_venta).replace("$","").replace(",","")),
            grand_total=float(str(grand_total).replace("$","").replace(",","")),
            filename=filename,
            file_bytes=file_bytes,
        )
        if d == "postgresql":
            row = conn.execute(
                text("""
                INSERT INTO invoices
                  (customer, ubicacion, persons, floors, control_type, door_type,
                   machine_room, hydraulic, grand_venta, grand_total, filename, file_bytes)
                VALUES
                  (:customer, :ubicacion, :persons, :floors, :control_type, :door_type,
                   :machine_room, :hydraulic, :grand_venta, :grand_total, :filename, :file_bytes)
                RETURNING id
                """),
                params
            ).one()
            return int(row[0])
        else:
            conn.execute(
                text("""
                INSERT INTO invoices
                  (customer, ubicacion, persons, floors, control_type, door_type,
                   machine_room, hydraulic, grand_venta, grand_total, filename, file_bytes)
                VALUES
                  (:customer, :ubicacion, :persons, :floors, :control_type, :door_type,
                   :machine_room, :hydraulic, :grand_venta, :grand_total, :filename, :file_bytes)
                """),
                params
            )
            row = conn.execute(text("SELECT last_insert_rowid()")).one()
            return int(row[0])


def list_invoices(limit=50):
        
        with engine.begin() as conn:
            rows = conn.execute(
                text("""
                SELECT id, created_at, customer, ubicacion, persons, floors,
                    control_type, door_type, machine_room, hydraulic,
                    grand_venta, grand_total, filename
                FROM invoices
                ORDER BY created_at DESC
                LIMIT :limit
                """),
                {"limit": int(limit)}
            ).mappings().all()
        return list(rows)

def fetch_invoice_file(inv_id: int):
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT filename, file_bytes FROM invoices WHERE id = :id"),
            {"id": int(inv_id)}
        ).one_or_none()
    if row is None:
        return None
    fname, fbytes = row
    return fname, _blob(fbytes)

def delete_invoice(inv_id: int):
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM invoices WHERE id = :id"),
                {"id": int(inv_id)}
            )

def save_invoice_images(inv_id: int, imgs: list[dict]):
    if not imgs:
        return
    has_cat = _images_has_category()
    with engine.begin() as conn:
        if has_cat:
            conn.execute(
                text("""
                    INSERT INTO invoice_images (invoice_id, title, description, image_bytes, category)
                    VALUES (:invoice_id, :title, :description, :image_bytes, :category)
                """),
                [
                    dict(
                        invoice_id=int(inv_id),
                        title=(i.get("title") or "")[:200],
                        description=(i.get("desc") or "")[:2000],
                        category=(i.get("category") or "normal")[:40],
                        image_bytes=i["bytes"],
                        
                    )
                    for i in imgs
                ]
            )
        else:
            conn.execute(
                text("""
                    INSERT INTO invoice_images (invoice_id, title, description, image_bytes)
                    VALUES (:invoice_id, :title, :description, :image_bytes)
                """),
                [
                    dict(
                        invoice_id=int(inv_id),
                        title=(i.get("title") or "")[:200],
                        description=(i.get("desc") or "")[:2000],
                        image_bytes=i["bytes"],
                    )
                    for i in imgs
                ]
            )
def get_invoice_images(inv_id: int):
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT id, title, description, category, image_bytes   -- <— include category
                FROM invoice_images
                WHERE invoice_id = :id
                ORDER BY id
            """),
            {"id": int(inv_id)}
        ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["image_bytes"] = _blob(d.get("image_bytes"))
        d["category"] = d.get("category") or "normal"              # <— default
        out.append(d)
    return out

def get_recent_images(limit: int = 24):
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT id, title, description, category, image_bytes   -- <— include category
                FROM invoice_images
                ORDER BY id DESC
                LIMIT :limit
            """),
            {"limit": int(limit)},
        ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["image_bytes"] = _blob(d.get("image_bytes"))
        d["category"] = d.get("category") or "normal"              # <— default
        out.append(d)
    return out


def delete_invoice_image(image_id: int):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM invoice_images WHERE id = :id"), {"id": int(image_id)})

#cuarto de maquinas la mano de obra cuesta valor adicional de 350|
def parse_money(x):
    """Turn '$1,234.50' / '1234.5' / '' / None into float safely."""
    if x is None:
        return 0.0
    if isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x)):
        return float(x)
    s = str(x).strip()
    if s == "":
        return 0.0
    try:
        # keep digits, dot, minus (ignore $ and commas)
        s = re.sub(r"[^\d.\-]", "", s)
        if s in {"", ".", "-"}:
            return 0.0
        return float(s)
    except Exception:
        return 0.0

def fmt_money(v):
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return "$0.00"
#hidraulico y motor
def save_parts(df: pd.DataFrame):
    with engine.begin() as conn:
        df.to_sql("parts_rules", conn, if_exists="replace", index=False)

TRUE_LITS  = {"true","verdadero","sí","si","yes","y","1"}
# add "" so empty cells are False
FALSE_LITS = {"false","falso","no","n","0",""}  

def _coerce_bool_like(val):
    if isinstance(val, bool): return val
    if val is None: return False
    s = str(val).strip().lower()
    if s in TRUE_LITS: return True
    if s in FALSE_LITS: return False
    return None

def safe_eval(expr, ctx):
    b = _coerce_bool_like(expr)
    if b is not None:
        return b
    try:
        return eval(str(expr), {"__builtins__": None}, {**ctx, "ceil": ceil})
    except Exception:
        return False
def _images_has_category() -> bool:
    try:
        insp = sa.inspect(engine)
        cols = {c["name"] for c in insp.get_columns("invoice_images")}
        return "category" in cols
    except Exception:
        return False

def get_known_categories() -> list[str]:
    """
    Devuelve la lista de secciones conocidas.
    Arranca con ['normal', 'hydraulic'], añade las de sesión y las que existan en BD.
    """
    cats = {"normal", "hydraulic"}  # por defecto
    # de la sesión actual
    for im in st.session_state.get("custom_images", []):
        cats.add((im.get("category") or "normal").strip() or "normal")
    # de la BD (si ya hay imágenes guardadas)
    try:
        for im in get_recent_images(limit=1000):
            cats.add((im.get("category") or "normal").strip() or "normal")
    except Exception:
        pass
    # extras añadidas manualmente por el usuario (sin imágenes aún)
    cats.update(st.session_state.get("image_categories_extra", []))
    return sorted(cats)

def add_new_category(name: str):
    name = (name or "").strip()
    if not name:
        return False, "Escribe un nombre."
    if len(name) > 40:
        return False, "Máximo 40 caracteres."
    cats = set(get_known_categories())
    if name in cats:
        return False, "Ya existe esa sección."
    extra = set(st.session_state.get("image_categories_extra", []))
    extra.add(name)
    st.session_state.image_categories_extra = sorted(extra)
    return True, f"Sección “{name}” creada."

# keep these
# --- capacity steps and targeting ---
# --- Capacity steps (EN 81 / ISO 8100) -------------------------
RATED_STEPS = [250, 320, 450, 630, 800, 1000, 1125, 1275]

def target_capacity_kg(P: int) -> int:
    """Smallest standard capacity ≥ 75 kg per person."""
    demand = 75 * int(P or 0)
    bigger = [s for s in RATED_STEPS if s >= demand]
    return min(bigger) if bigger else max(RATED_STEPS)

# --- Parse capacity from unit_weight / "…kg" text ----------------
def _cap_kg(row):
    v = pd.to_numeric(row.get("unit_weight", None), errors="coerce")
    if pd.isna(v) or float(v) <= 0:
        txt = f"{row.get('weight','')} {row.get('description','')}"
        m = WEIGHT_RE.search(txt)  # e.g. "... 630kg ..."
        v = float(m.group(1)) if m else float('nan')
    return 0.0 if pd.isna(v) else float(v)

# --- Branch title helpers ---------------------------------------
def _branch_name(ctx: dict) -> str:
    return ("gearless" if ctx.get("gearless") else "con reductor") + \
           (" — sin cuarto" if not ctx.get("machine_room") else " — con cuarto")

def _header_from_df(df: pd.DataFrame | None, tgt: int, ctx: dict) -> str:
    """Text for the motor expander header."""
    if df is not None and not df.empty:
        r    = df.iloc[0]
        cap  = int(pd.to_numeric(r.get("cap_kg"), errors="coerce")) \
               if pd.notna(r.get("cap_kg")) else tgt
        vta  = float(pd.to_numeric(r.get("venta"), errors="coerce") or 0.0)
        desc = (str(r.get("description") or r.get("part_id") or "").strip() or "motor").replace("\n"," ")
        return f"{cap} kg — {desc} — ${vta:,.2f} — {_branch_name(ctx)}"
    return f"Cambiar a Motor para el siguiente peso: ≥ {tgt} kg — {_branch_name(ctx)}"


def _branch_split(parts_df: pd.DataFrame, ctx: dict):
    # 0) Treat blank/NaN condition_expr as True (include by default)
    if "condition_expr" not in parts_df.columns:
        parts_df = parts_df.copy()
        parts_df["condition_expr"] = "True"
    else:
        parts_df = parts_df.copy()
        parts_df["condition_expr"] = (
            parts_df["condition_expr"]
                .astype(str)
                .apply(lambda s: "True" if str(s).strip() in {"", "nan", "None"} else s)
        )
    mask = parts_df["condition_expr"].apply(lambda e: bool(safe_eval(e, ctx)))
    allowed = parts_df[mask].copy()

    # 2) capacity for all rows
    allowed["cap_kg"] = allowed.apply(_cap_kg, axis=1).fillna(0.0)

    # 3) motors = has "motor" and cap > 0, excluding accessories
    is_motor = (
        allowed["description"].str.contains(r"\bmotor\b", case=False, na=False)
        & (allowed["cap_kg"] > 0)
    )
    exclude = allowed["description"].str.contains(BAD_WORDS_RE, na=False)
    is_motor = is_motor & ~exclude

    # (optional) allow prompt_key "motor…" to force inclusion
    if "prompt_key" in allowed.columns:
        is_motor = is_motor | allowed["prompt_key"].str.fullmatch(r"motor.*", na=False)

    motors = allowed.loc[is_motor].copy()

    # label used in legacy selectboxes (kept for debugging/consistency)
    def _fmt(r):
        venta = pd.to_numeric(r.get("venta"), errors="coerce")
        costo = pd.to_numeric(r.get("costo"), errors="coerce")
        cap   = pd.to_numeric(r.get("cap_kg"), errors="coerce")
        venta_f = f"${(0.0 if pd.isna(venta) else float(venta)):,.2f}"
        costo_f = f"${(0.0 if pd.isna(costo) else float(costo)):,.2f}"
        cap_i   = int(cap) if pd.notna(cap) and cap > 0 else 0
        pid  = r.get("part_id", "(sin id)")
        desc = str(r.get("description", "")).strip()
        return f"{pid} — {cap_i} kg — {venta_f} VTA / {costo_f} Costo — {desc}"

    motors["__label__"] = motors.apply(_fmt, axis=1)
    return allowed, motors

def get_all_motors(parts_df: pd.DataFrame) -> pd.DataFrame:
    """Every motor in DB (any branch) with cap_kg parsed, accessories excluded."""
    df = parts_df.copy()
    df["cap_kg"] = df.apply(_cap_kg, axis=1).fillna(0.0)
    is_motor = df["description"].str.contains(r"\bmotor\b", case=False, na=False) & (df["cap_kg"] > 0)
    exclude  = df["description"].str.contains(BAD_WORDS_RE, na=False)
    return df.loc[is_motor & ~exclude].copy()

def motor_picker_table(df: pd.DataFrame, key: str, title: str = "Motores"):
    """Let the user tick one motor; return the *full* row from df (all rule columns)."""
    if df is None or df.empty:
        st.subheader(title)
        st.info("No hay motores para mostrar.")
        return None

    # Make a view for display only
    view = df.copy()
    for c in ["part_id", "description", "costo", "venta", "cap_kg"]:
        if c not in view.columns:
            view[c] = np.nan
    for c in ["costo", "venta", "cap_kg"]:
        view[c] = pd.to_numeric(view[c], errors="coerce").fillna(0.0)
    if "__pick__" not in view.columns:
        view.insert(0, "__pick__", False)
    view = view.reset_index(drop=True)

    st.subheader(title)
    edited = st.data_editor(
        view[["__pick__", "part_id", "cap_kg", "costo", "venta", "description"]],
        hide_index=True,
        use_container_width=True,
        key=f"mot_picker_{key}",
        column_config={
            "__pick__":    st.column_config.CheckboxColumn("Seleccionar", help="Marca uno"),
            "part_id":     st.column_config.TextColumn("Parte", disabled=True),
            "cap_kg":      st.column_config.NumberColumn("Cap. (kg)", disabled=True, format="%.0f"),
            "costo":       st.column_config.NumberColumn("Costo",   disabled=True, format="$%.2f"),
            "venta":       st.column_config.NumberColumn("Venta",   disabled=True, format="$%.2f"),
            "description": st.column_config.TextColumn("Descripción", disabled=True),
        },
    )

    picked = edited.loc[edited["__pick__"] == True]  # noqa: E712
    if len(picked) == 1:
        pid = picked["part_id"].iloc[0]
        # Return the *full* row from the original df so condition_expr/qty_formula/etc. are preserved.
        full = df[df["part_id"] == pid].iloc[[0]].copy()
        # Ensure cap_kg exists for later headers, if needed
        if "cap_kg" not in full.columns:
            full["cap_kg"] = full.apply(_cap_kg, axis=1).fillna(0.0)
        return full

    return None

def persons_for_capacity(cap_kg: float) -> int:
    try:
        return max(1, int(math.floor(float(cap_kg) / 75.0)))
    except Exception:
        return 1

def _motor_table(df: pd.DataFrame, title: str = "Motores", key: str = "mot_tbl"):
    if df is None or df.empty:
        st.subheader(title)
        st.info("No hay motores para mostrar.")
        return

    view = df.copy()
    view = view.loc[~view["description"].str.contains(BAD_WORDS_RE, na=False)]

    # Ensure we have a usable cap_kg (fallbacks if missing/zero)
    if "cap_kg" not in view.columns:
        view["cap_kg"] = view.apply(_cap_from_row, axis=1)
    else:
        view["cap_kg"] = pd.to_numeric(view["cap_kg"], errors="coerce").fillna(0.0)
        mask_zero = view["cap_kg"] <= 0
        if mask_zero.any():
            view.loc[mask_zero, "cap_kg"] = view.loc[mask_zero].apply(_cap_from_row, axis=1)

    view["costo"] = pd.to_numeric(view.get("costo"), errors="coerce").fillna(0.0)
    view["venta"] = pd.to_numeric(view.get("venta"), errors="coerce").fillna(0.0)

    # NEW: persons the motor fits (≈)
    view["fits_ppl"] = view["cap_kg"].apply(persons_for_capacity)

    view = view.rename(columns={
        "part_id": "Parte",
        "cap_kg": "Cap. (kg)",
        "costo": "Costo",
        "venta": "Venta",
        "description": "Descripción",
        "fits_ppl": "≈ Personas",
    })

    cols = [c for c in ["Parte", "Cap. (kg)", "≈ Personas", "Costo", "Venta", "Descripción"] if c in view.columns]
    st.markdown(f"**{title}**")
    st.dataframe(view[cols], use_container_width=True, key=key)


def _build_new_motor_row(part_id, desc, cap_kg, venta, costo, ctx):
    iva_unit = float(venta) * 1.15
    cond_bits = [
        "gearless" if ctx.get("gearless") else "(not gearless)",
        "machine_room" if ctx.get("machine_room") else "(not machine_room)",
        "not hydraulic_cylinder",
    ]
    row = {
        "part_id":        part_id,
        "description":    desc,
        "costo":          float(costo),
        "venta":          float(venta),
        "iva":            float(iva_unit),
        "qty_formula":    "1",
        "condition_expr": " and ".join(cond_bits),
        "prompt_key":     "machine_room" if ctx.get("machine_room") else "not machine_room",
        "unit_weight":    float(cap_kg),
        "weight":         f"{int(cap_kg)}kg",
    }
    return row

def compute_lines(ctx, parts_df, cabina_price, shipping_cost, cabina_cost):
    mask = parts_df["condition_expr"].apply(lambda e: bool(safe_eval(e, ctx)))
    rules = parts_df[mask].copy()

    lines, total_iva, total_venta, caps = [], 0.0, 0.0, []

    for _, part in rules.iterrows():
        raw_qty = safe_eval(part.get("qty_formula", 0), ctx)

        # explicit boolean handling (True->1, False->0)
        if isinstance(raw_qty, bool):
            qty = 1 if raw_qty else 0
        else:
            try:
                qty = int(float(raw_qty))
            except Exception:
                qty = 0

        if qty <= 0:
            continue

        iva_up     = float(part.get("iva",   0.0) or 0.0)
        venta_up   = float(part.get("venta", 0.0) or 0.0)
        costo_unit = float(part.get("costo", 0.0) or 0.0)

        total_iva   += qty * iva_up
        total_venta += qty * venta_up

        uw = part.get("unit_weight", 0.0) or 0.0
        m  = WEIGHT_RE.search(str(part.get("description","")))
        if uw == 0.0 and m:
            uw = float(m.group(1))
        if uw > 0:
            caps.append(qty * uw)

        lines.append({
            "Description": part["description"],
            "Qty": qty,
            "Costo Unitario":  f"${costo_unit:,.2f}",
            "Total Costo":     f"${qty * costo_unit:,.2f}",
            "Unit Price (IVA)": f"${iva_up:,.2f}",
            "Line Total (IVA)": f"${qty * iva_up:,.2f}",
            "Unit Price (VTA)": f"${venta_up:,.2f}",
            "Line Total (VTA)": f"${qty * venta_up:,.2f}",
        })

    if cabina_price > 0:
        total_iva   += cabina_price
        total_venta += cabina_price
        lines.append({
            "Description": "Cabina",
            "Qty": 1,
            "Costo Unitario":  f"${cabina_cost:,.2f}",
            "Total Costo":     f"${cabina_cost:,.2f}",
            "Unit Price (VTA)": f"${cabina_price:,.2f}",
            "Line Total (VTA)": f"${cabina_price:,.2f}",
            "Unit Price (IVA)": f"${cabina_price:,.2f}",
            "Line Total (IVA)": f"${cabina_price:,.2f}",
        })

    if shipping_cost > 0:  # optional: avoid showing a $0 shipping line
        total_iva   += shipping_cost
        total_venta += shipping_cost
        lines.append({
            "Description": "Precio de envío",
            "Qty": 1,
            "Costo Unitario": "$0.00",
            "Total Costo":    "$0.00",
            "Unit Price (VTA)": f"${shipping_cost:,.2f}",
            "Line Total (VTA)": f"${shipping_cost:,.2f}",
            "Unit Price (IVA)": f"${shipping_cost:,.2f}",
            "Line Total (IVA)": f"${shipping_cost:,.2f}",
        })

    capacity = max(caps) if caps else None
    return lines, total_iva, total_venta, capacity



# ── Streamlit UI ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Elevator Invoice", page_icon=LOGO_PATH,layout="wide")

# ---- Session defaults (run once, safe across reruns) ----
st.session_state.setdefault("show_preview", False)
st.session_state.setdefault("custom_images", [])  # list of {"title","desc","bytes"}

# --- Theme (keep yours, just adding a couple header classes) ---
st.markdown("""
<style>
.stApp { background-color: #f7faff; }
section[data-testid="stSidebar"] { background:#e6f0ff; }
h1, h2, h3 { color:#004080; }
button[kind="primary"] { background:#004080; color:white; }
.kmon-header { display:flex; align-items:flex-end; gap:24px; }
.kmon-logo   { height: 90px; }
.kmon-title  { margin:0 0 2px 0; }
</style>
""", unsafe_allow_html=True)

#logo_path = os.path.join(HERE, "images", "logo.png")  # use HERE so it works in Docker/Render

def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

# --- Header: logo only ---
if os.path.exists(LOGO_PATH):
    st.markdown(
        f"""
        <div class="kmon-header">
          <img
            src="data:image/png;base64,{_b64(LOGO_PATH)}"
            class="kmon-logo"
            alt="La Casa del Ascensor"
          />
        </div>
        """,
        unsafe_allow_html=True,
    )
# no else: hide title when logo missing

# no else: no title fallback; nothing is shown if the logo file is missing


# --- Simple password gate ---
REQUIRED_PW = os.getenv("APP_PASSWORD", "")
tab_gen, tab_arch, tab_admin = st.tabs(["🧾 Generar factura", "🗂️ Archivo", "⚙️ Admin"])
# ── Admin: edit costo/venta ─────────────────────────────────────────────────────
with tab_admin:
    st.header("🔧 Admin")

    # --- Admin login just for this tab ---
    can_show_admin = True
    if REQUIRED_PW:  # only gate if a password is set
        authed = st.session_state.get("authed_admin", False)
        if not authed:
            # small inline login (no st.stop so the rest of the app keeps running)
            pw = st.text_input("Password de admin", type="password", key="admin_pw")
            if st.button("Entrar", key="admin_login_btn"):
                st.session_state.authed_admin = (pw == REQUIRED_PW)
                if not st.session_state.authed_admin:
                    st.error("Password incorrecto")
            authed = st.session_state.get("authed_admin", False)
        can_show_admin = authed

    if not can_show_admin:
        st.info("Ingresa el password para ver esta sección.")
    else:
        # --- Price editor ---
        edit_mode = st.checkbox("Editar precios (costo / venta)", value=False)
        if edit_mode:
            parts_df = load_parts()
            edited = st.data_editor(
                parts_df[["part_id", "description", "costo", "venta"]],
                num_rows="dynamic",
                use_container_width=True,
                key="price_editor",
            )
            if st.button("💾 Guardar cambios", type="primary", key="save_prices"):
                full = parts_df.set_index("part_id")
                for pid in edited["part_id"]:
                    full.at[pid, "costo"] = float(
                        edited.loc[edited["part_id"] == pid, "costo"].iloc[0]
                    )
                    full.at[pid, "venta"] = float(
                        edited.loc[edited["part_id"] == pid, "venta"].iloc[0]
                    )
                save_parts(full.reset_index())
                st.success("Precios actualizados.")

        st.divider()

        # --- One-click ETL: Elevators.xlsx -> DB ---
        st.subheader("📥 Cargar Excel → DB")
        st.caption(
            "Ejecuta el ETL para poblar/actualizar la tabla `parts_rules` desde *Elevators.xlsx*."
            " Usa `DATABASE_URL` si está definido; de lo contrario, SQLite local."
        )
        if st.button("Cargar ahora", key="run_etl"):
            try:
                import etl  # ensure imported at top of file too
                etl.load_excel_to_db()
                st.success("✅ Carga completada. Tabla `parts_rules` actualizada.")
            except Exception as e:
                st.error(f"❌ Error cargando datos: {e}")

# — Inputs —
with tab_gen:
# Cliente / Fecha
    c1, c2 = st.columns([2,1])
    with c1:
     customer = st.text_input("Cliente")
    with c2:
        date_str = st.text_input("Fecha", pd.Timestamp("today").strftime("%d/%m/%Y"))

    # Ubicación
    ubicacion = st.text_input("Ubicación")
    # Personas / Paradas
    cP, cF, cDoor = st.columns(3)
    with cP:
        P = st.number_input("Número de personas", 1, 17, 1)
    with cF:
        F = st.number_input("Número de paradas", 1, 50, 1)



    # ===== Control / Tracción / Puertas / Cuarto de máquinas =====
    col_ctrl, col_trac, col_door, col_room = st.columns(4)

    with col_ctrl:
        # Control type (Monarch/Heytech)
        control = st.radio("Tipo de control", ("Monarch", "Heytech"), horizontal=True)

        # Encoder selector ONLY when Heytech, as a compact dropdown
        if control == "Heytech":
            enc_choice = st.selectbox(
                "Encoder (Heytech)",                # short label
                ["Sin encoder", "Con encoder"],     # dropdown saves space
                index=0,
            )
            encoder = (enc_choice == "Con encoder")
        else:
            encoder = False

    with col_door:
        # Puertas right next to cuarto de máquinas
        # --- Doors ---
        door_opt = st.radio(
            "Puertas",
            ("manuales", "automáticas", "sin puertas"),
            horizontal=True
        )

        DOOR_KEY = {
            "manuales": "manuales",
            "automáticas": "automaticas",   # NOTE: no accent in the key used in rules
            "sin puertas": "sin_puertas",
        }
        door_key = DOOR_KEY[door_opt]
        door_text = "Sin puertas" if door_key == "sin_puertas" else f"Puertas {door_opt}"

    with col_room:
        machine = st.radio("¿Con cuarto de máquinas?", ("Sí", "No"), horizontal=True) == "Sí"

    # Cilindro hidráulico (solo si P ≤ 3 y F ≤ 3)
    if P <= 3 and F <= 3:
        hydraulic_cylinder = st.radio(
            "¿Incluir cilindro hidráulico BTD-55?", ("Sí","No")) == "Sí"
    else:
        hydraulic_cylinder = False
    #remove this stray branch that sets an unused variable:
    if not hydraulic_cylinder and P == 3 and F <= 3:
        machine_room = True

    with col_trac:
        traction = st.radio("Tipo de tracción", ("Gearless", "Con reductor"), horizontal=True)
        gearless = (traction == "Gearless")
    
    # Precios: Envío / Cabina (venta) / Costo Cabina
    cShip, cCab, cCost = st.columns(3)
    with cShip:
        precio_envio = st.number_input("Precio de envío (USD)", min_value=0.0, value=0.0, step=1.0, format="%.2f")
    with cCab:
        cabina = st.number_input("🚪 Cabina (venta unitaria)", 0.0, step=0.01, format="%.2f")
    with cCost:
        costo_cabina = st.number_input("🏷️ Costo de Cabina (costo unitario)", 0.0, step=0.01, format="%.2f")


    # ---- Context for rules -------------------------------------------------------
    ctx = {
        "P": P, "F": F,
        "machine_room": machine,
        "door_type": door_key,
        "control_type": control,
        "encoder": encoder,
        "hydraulic_cylinder": hydraulic_cylinder,
        "gearless": gearless,
    }

    # Always have ctx2 available (even when hydraulic)
    ctx2 = {
    **ctx,
    "chosen_motor_pid": "",
    "chosen_motor_cap": 0.0,
    "motor_group": "",
    "is_double_drum": False,
    }

    

    # ---- Branch + motors ---------------------------------------------------------
    parts_df_full = load_parts()
    allowed, motors    = _branch_split(parts_df_full, ctx)   # rows valid for THIS branch
    motors_all         = get_all_motors(parts_df_full)       # all motors in DB (any branch)
    tgt                = target_capacity_kg(P)

     # -------------------------------------------------------------------------------
    # ---- After ctx / allowed / motors / tgt are computed -------------------------

    def _motor_ctx_key(ctx: dict, tgt: int) -> str:
        """Minimal signature of what determines motor eligibility/target."""
        return "|".join([
            "G1" if ctx.get("gearless") else "G0",
            "R1" if ctx.get("machine_room") else "R0",
            "H1" if ctx.get("hydraulic_cylinder") else "H0",
            ctx.get("door_type", ""),
            ctx.get("control_type", ""),
            "E1" if ctx.get("encoder") else "E0",
            f"T{int(tgt)}",
        ])


    def _cap_from_row(r) -> float:
        v = pd.to_numeric(r.get("cap_kg"), errors="coerce")
        if pd.notna(v) and v > 0: return float(v)
        v = pd.to_numeric(r.get("unit_weight"), errors="coerce")
        if pd.notna(v) and v > 0: return float(v)
        m = WEIGHT_RE.search(str(r.get("description","")))
        return float(m.group(1)) if m else 0.0

    def _tags_for_motor_row(r: pd.Series) -> dict:
        pid  = str(r.get("part_id",""))
        desc = (r.get("description") or "").lower()
        group = MOTOR_GROUPS.get(pid, "")
        is_double = ("doble" in desc and "tambor" in desc) or group.startswith("double_drum")
        return {
            "chosen_motor_pid": pid,
            "chosen_motor_cap": _cap_from_row(r),
            "motor_group": group,              # e.g., "double_drum_990"
            "is_double_drum": bool(is_double),
        }

    def _is_chosen_motor_valid(chosen_df: pd.DataFrame | None,
                            allowed_df: pd.DataFrame,
                            tgt: int) -> bool:
        if chosen_df is None or chosen_df.empty:
            return False
        r = chosen_df.iloc[0]
        pid = str(r.get("part_id", ""))
        cap = _cap_from_row(r)
        # must still exist in this branch and meet capacity
        allowed_ids = set(allowed_df["part_id"].astype(str))
        return (pid in allowed_ids) and (cap >= float(tgt))

    # 1) Reset manual choice if the context key changed (persons, floors, etc.)
    new_key = _motor_ctx_key(ctx, tgt)
    old_key = st.session_state.get("motor_ctx_key")
    if old_key != new_key:
        st.session_state["chosen_motor_df"] = None
    st.session_state["motor_ctx_key"] = new_key

    # 2) If a prior manual pick is no longer valid for this branch/target, drop it
    chosen_motor_df = st.session_state.get("chosen_motor_df")
    if not _is_chosen_motor_valid(chosen_motor_df, allowed, tgt):
        st.session_state["chosen_motor_df"] = None
        chosen_motor_df = None


    # -------------------------------------------------------------------------------

    # keep a persisted pick across reruns

    if hydraulic_cylinder:
                # Cylinder selected => do not inject a motor; still allow browsing all motors
                with st.expander("⚙️ Cilindro hidráulico seleccionado — no se requiere motor", expanded=False):
                    with st.expander("🔍 Ver todos los motores (todas las ramas)", expanded=False):
                        _motor_table(motors_all, title="Todos los motores (todas las ramas)", key="tbl_all_motors_h")
                parts_df = allowed.copy()
                st.session_state["chosen_motor_df"] = None
    else:
            # Eligible motors (>= target), smallest first
            cap_num  = pd.to_numeric(motors["cap_kg"], errors="coerce").fillna(0.0)
            min_required = 75 * int(P or 0)  # raw demand (kg)
            cap_num  = pd.to_numeric(motors["cap_kg"], errors="coerce").fillna(0.0)
            eligible = motors.loc[cap_num >= min_required] \
                            .sort_values(["cap_kg", "costo", "venta"], na_position="last")
            auto_choice_df = eligible.iloc[[0]] if not eligible.empty else None

            # Persisted manual choice (full-row df) if any
            chosen_motor_df = st.session_state.get("chosen_motor_df")

            # The motor *currently* in effect: manual pick beats auto; else None
            active_motor_df = (
                chosen_motor_df if (chosen_motor_df is not None and not chosen_motor_df.empty)
                else auto_choice_df
            )

            cap_active = _cap_from_row(active_motor_df.iloc[0]) if (active_motor_df is not None and not active_motor_df.empty) else 0.0
            fits_ppl  = persons_for_capacity(cap_active)

            title = _header_from_df(active_motor_df, tgt, ctx)
            if 0 < cap_active < float(tgt):
                title += f" — ⚠️ fuera de estándar ({int(cap_active)} kg < {int(tgt)} kg, ≈{fits_ppl} personas)"
            with st.expander(f"⚙️ {title} — cambiar si quieres", expanded=False):
                manual_pick = st.checkbox(
                    "Elegir motor manualmente",
                    value=False,
                    key=f"pick_motor_manually_{ctx.get('gearless')}_{ctx.get('machine_room')}"
                )

                if manual_pick:
                    picked = motor_picker_table(
                        eligible if not eligible.empty else motors,  # pass dataframe that has full rule columns
                        key=f"eligible_{ctx.get('gearless')}_{ctx.get('machine_room')}",
                        title=f"Motores que cumplen ≥ {tgt} kg" if not eligible.empty else "Motores de esta rama"
                    )
                    if picked is not None:
                        st.session_state["chosen_motor_df"] = picked  # persist immediately
                        active_motor_df = picked

                with st.expander("🔍 Ver todos los motores (todas las ramas)", expanded=False):
                    _motor_table(motors_all, title="Todos los motores (todas las ramas)", key="tbl_all_motors")

                with st.expander("➕ Agregar motor nuevo", expanded=False):
                    with st.form("add_custom_motor"):
                        new_desc  = st.text_input("Descripción", value="MOTOR CUSTOM", key="nm_desc")
                        new_cap   = st.number_input("Capacidad (kg)", min_value=200, max_value=2000,
                                                    step=10, value=int(tgt), key="nm_cap")
                        new_venta = st.number_input("Precio unitario (VTA)", min_value=0.0, step=10.0, key="nm_vta")
                        new_costo = st.number_input("Costo unitario",      min_value=0.0, step=10.0, key="nm_cost")
                        persist   = st.checkbox("Guardar en la base de datos para futuras cotizaciones",
                                                value=False, key="nm_persist")
                        add_ok    = st.form_submit_button("Añadir motor")

                    if add_ok:
                        part_id = f"MOTOR_USER_{pd.Timestamp('now').strftime('%Y%m%d_%H%M%S')}"
                        new_df  = pd.DataFrame([_build_new_motor_row(part_id, new_desc, new_cap, new_venta, new_costo, ctx)])
                        # Use it now + remember it
                        st.session_state["chosen_motor_df"] = new_df.copy()
                        active_motor_df = new_df.copy()
                        # Also make sure it’s available in allowed for this run
                        allowed = pd.concat([allowed, new_df], ignore_index=True)
                        if persist:
                            current = load_parts()
                            save_parts(pd.concat([current, new_df], ignore_index=True))
                            st.success(f"Guardado en BD como {part_id}.")

            # Build final parts_df with exactly ONE motor row (active or none)
            # --- Augment ctx with selected-motor tags, then re-split rules -----------------
            ctx2 = {**ctx}
            if active_motor_df is not None and not active_motor_df.empty:
                ctx2.update(_tags_for_motor_row(active_motor_df.iloc[0]))

            # IMPORTANT: re-run branch split with the augmented ctx so bundle parts can key off motor
            allowed2, _ = _branch_split(parts_df_full, ctx2)

            # Keep exactly one motor line: the selected/auto motor
            is_motor_allowed2 = allowed2["description"].str.contains(r"\bmotor\b", case=False, na=False)
            if active_motor_df is not None and not active_motor_df.empty:
                parts_df = pd.concat([allowed2[~is_motor_allowed2], active_motor_df], ignore_index=True)
            else:
                parts_df = allowed2.copy()

        

        # -------------------------------------------------------------------------------

    if "show_preview" not in st.session_state:
            st.session_state.show_preview = False

    include_civil = st.checkbox("✏️ Incluir tabla de Trabajos de Obra Civil", value=False)
    shipping = precio_envio

        # 1) Unpack both totals
    lines, total_iva, total_venta, cap = compute_lines(ctx2, parts_df, cabina, shipping, costo_cabina)
    if st.button("🔍 Previsualizar Cotización"):
            st.session_state.show_preview = not st.session_state.show_preview

        # ── Toggle for enabling the editable grid ────────────────────────────────────────
        # Toggle for enabling the editable grid
    edit_preview = st.checkbox(
            "✏️ Habilitar edición de la tabla de cotización",
            value=False,
            key="edit_preview_toggle"
        )

    if st.session_state.show_preview:
            # 1) Build your base DataFrame
            df_base = pd.DataFrame(lines)
            if "qty" in df_base.columns:
                df_base = df_base.rename(columns={"qty": "Qty"})

            st.subheader("📄 Previsualización de Cotización")

            # 2) Show editor only in edit mode
            if edit_preview:
                df_edit = st.data_editor(
                    df_base,
                    num_rows="dynamic",
                    use_container_width=True,
                    column_config={
                        "Description":      st.column_config.TextColumn("Descripción", disabled=False),
                        "Qty":              st.column_config.NumberColumn("Cantidad",),
                        "Costo Unitario":   st.column_config.TextColumn("Costo Unitario", disabled=False),
                        "Total Costo":      st.column_config.TextColumn("Total Costo", disabled=True),
                        "Unit Price (VTA)": st.column_config.TextColumn("Precio Unit. (VTA)"),
                        "Unit Price (IVA)": st.column_config.TextColumn("Precio Unit. (IVA)", disabled=True),
                        "Line Total (VTA)": st.column_config.TextColumn("Total (VTA)", disabled=True),
                        "Line Total (IVA)": st.column_config.TextColumn("Total (IVA)", disabled=True),
                    },
                )
                working_df = df_edit.copy()
            else:
                working_df = df_base.copy()

            # 3) Ensure required columns exist (new rows may miss some)
            for col in [
                "Description", "Qty",
                "Costo Unitario", "Total Costo",
                "Unit Price (VTA)", "Unit Price (IVA)",
                "Line Total (VTA)", "Line Total (IVA)"
            ]:
                if col not in working_df.columns:
                    working_df[col] = None

            # 4) Clean Qty → numeric (blanks → 0)
            working_df["Qty"] = pd.to_numeric(working_df["Qty"], errors="coerce").fillna(0.0)

            # 5) Parse money-like fields safely (handles $, commas, blanks)
            #    These two are user-editable in the grid:
            working_df["Unit Price (VTA)"] = working_df["Unit Price (VTA)"].apply(parse_money)
            working_df["Costo Unitario"]   = working_df["Costo Unitario"].apply(parse_money)

            # 6) Recalculate totals
            working_df["Unit Price (IVA)"] = working_df["Unit Price (VTA)"] * 1.15
            working_df["Line Total (VTA)"] = working_df["Qty"] * working_df["Unit Price (VTA)"]
            working_df["Line Total (IVA)"] = working_df["Qty"] * working_df["Unit Price (IVA)"]
            working_df["Total Costo"]      = working_df["Qty"] * working_df["Costo Unitario"]

            # 7) Format as currency for display (keeps the grid pretty)
            for col in [
                "Costo Unitario", "Total Costo",
                "Unit Price (VTA)", "Unit Price (IVA)",
                "Line Total (VTA)", "Line Total (IVA)"
            ]:
                working_df[col] = working_df[col].apply(fmt_money)

            # 8) Persist for export
            st.session_state["invoice_lines"]     = working_df.to_dict("records")
            st.session_state["total_venta_edited"] = (
                working_df["Line Total (VTA)"]
                    .str.replace(r"[\$,]", "", regex=True)
                    .astype(float)
                    .sum()
            )
            st.session_state["total_iva_edited"]   = (
                working_df["Line Total (IVA)"]
                    .str.replace(r"[\$,]", "", regex=True)
                    .astype(float)
                    .sum()
            )
            st.session_state["total_costo_edited"] = (
            working_df["Total Costo"]
                .str.replace(r"[\$,]", "", regex=True)
                .astype(float)
                .sum()
        )

            # 9) Show final table & metrics
            st.dataframe(working_df, use_container_width=True)
            c1, c2, c3 = st.columns(3)
            c1.metric("💲 Total sin IVA", f"${st.session_state['total_venta_edited']:,.2f}")
            c2.metric("💰 Total con IVA", f"${st.session_state['total_iva_edited']:,.2f}")
            c3.metric("Costo total", f"${st.session_state['total_costo_edited']:,.2f}")
        # Only show this when NOT hydraulic

    with st.expander("➕ Agregar imágenes"):
        st.caption("Sube hasta 4 imágenes. Puedes renombrarlas antes de guardar.")

        # NEW: selector for type; default follows current equipment
        default_cat = "hydraulic" if hydraulic_cylinder else "normal"
        cat_label = {"normal":"Normales", "hydraulic":"Hidráulicas"}
        upload_cat = st.selectbox(
            "Tipo de estas imágenes",
            options=["normal", "hydraulic"],
            index=0 if default_cat=="normal" else 1,
            format_func=lambda v: cat_label[v],
            help="Este tipo se guardará con todas las imágenes que subas aquí."
        )

        up = st.file_uploader("Imágenes", type=["png","jpg","jpeg"], accept_multiple_files=True)

        new_rows = []
        if up:
            for f in up:
                data = f.read()
                title = os.path.splitext(f.name)[0]
                new_rows.append({"preview": data, "title": title, "desc": "", "category": upload_cat})

            df_new = pd.DataFrame(new_rows)
            df_new = st.data_editor(
                df_new,
                num_rows="dynamic",
                use_container_width=True,
                column_config={
                    "preview":  st.column_config.ImageColumn("Imagen", width="small"),
                    "title":    st.column_config.TextColumn("Título"),
                    "desc":     st.column_config.TextColumn("Descripción"),
                    # allow per-row change
                    "category": st.column_config.SelectboxColumn("Tipo", options=["normal","hydraulic"],
                                                                required=True, help="normal / hidráulicas"),
                },
            )

            if st.button("Guardar nuevas imágenes", type="primary"):
                current = st.session_state.get("custom_images", [])
                for _, r in df_new.iterrows():
                    current.append({
                        "id": str(uuid4()),
                        "bytes": r["preview"],
                        "title": r["title"],
                        "desc":  r["desc"],
                        "category": r.get("category") or "normal",
                    })
                st.session_state.custom_images = current[:4]
                st.success(f"Guardadas. Ahora tienes {len(st.session_state.custom_images)} imagen(es).")

        # Edit current images (now includes category)
        current = st.session_state.get("custom_images", [])
        if current:
            st.markdown("#### Imágenes de esta factura")
            df_cur = pd.DataFrame(current)
            if "remove" not in df_cur.columns:
                df_cur["remove"] = False
            if "category" not in df_cur.columns:
                df_cur["category"] = "normal"

            df_cur = st.data_editor(
                df_cur,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "bytes":    st.column_config.ImageColumn("Imagen", width="small"),
                    "title":    st.column_config.TextColumn("Título"),
                    "desc":     st.column_config.TextColumn("Descripción"),
                    "category": st.column_config.SelectboxColumn("Tipo", options=["normal","hydraulic"]),
                    "id":       st.column_config.TextColumn("ID", disabled=True),
                    "remove":   st.column_config.CheckboxColumn("Eliminar"),
                },
            )

            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("Aplicar cambios a nombres/descripciones/tipo"):
                    new_list = []
                    for _, r in df_cur.iterrows():
                        if bool(r.get("remove", False)):
                            continue
                        new_list.append({
                            "id":    r["id"],
                            "bytes": r["bytes"],
                            "title": r.get("title",""),
                            "desc":  r.get("desc",""),
                            "category": r.get("category") or "normal",
                        })
                    st.session_state.custom_images = new_list
                    st.success("Cambios aplicados.")
            with col_b:
                if st.button("Vaciar imágenes de esta factura"):
                    st.session_state.custom_images = []
                    st.success("Se eliminaron todas las imágenes de esta factura.")

        # === Biblioteca de imágenes guardadas (con filtro de tipo) ===
        st.markdown("#### 📚 Usar imágenes guardadas anteriormente")

        # === NUEVO: crear secciones desde aquí ===
        with st.expander("🗂️ Administrar secciones"):
            new_cat = st.text_input("Nueva sección (ej. 'Obra civil', 'Puertas')", key="new_cat_input")
            col_add, col_tip = st.columns([1,3])
            with col_add:
                if st.button("➕ Añadir sección", key="btn_add_cat"):
                    ok, msg = add_new_category(new_cat)
                    (st.success if ok else st.warning)(msg)

            with col_tip:
                st.caption("Las secciones aparecen en el filtro, en el cargador y en la columna **Tipo**.")

        # Lista de opciones (dinámicas)
        all_cats = get_known_categories()
        filtro = st.selectbox(
            "Filtrar por tipo",
            options=["all"] + all_cats,
            index=0,
            format_func=lambda v: {"all":"Todas","normal":"Normales","hydraulic":"Hidráulicas"}.get(v, v)
        )

        prev_imgs = get_recent_images(limit=24)
        if not prev_imgs:
            st.info("No hay imágenes guardadas todavía.")
        else:
            rows_prev = []
            for im in prev_imgs:
                cat = (im.get("category") or "normal")
                if filtro != "all" and cat != filtro:
                    continue
                rows_prev.append({
                    "use": False,
                    "id": im["id"],
                    "img": im["image_bytes"],
                    "title": (im.get("title") or ""),
                    "desc":  (im.get("description") or ""),
                    "category": cat,
                })

            if rows_prev:
                df_prev = pd.DataFrame(rows_prev)
                df_prev = st.data_editor(
                    df_prev,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "use":      st.column_config.CheckboxColumn("Usar"),
                        "img":      st.column_config.ImageColumn("Imagen", width="small"),
                        "title":    st.column_config.TextColumn("Título", disabled=True),
                        "desc":     st.column_config.TextColumn("Descripción", disabled=True),
                        "category": st.column_config.TextColumn("Tipo", disabled=True),
                        "id":       st.column_config.TextColumn("ID", disabled=True),
                    },
                )
                if st.button("➕ Añadir seleccionadas a esta factura"):
                    selected = df_prev[df_prev["use"]].to_dict("records")
                    if not selected:
                        st.info("No hay imágenes seleccionadas.")
                    else:
                        cur = st.session_state.get("custom_images", [])
                        for r in selected:
                            cur.append({
                                "id":    str(uuid4()),
                                "bytes": r["img"],
                                "title": r["title"],
                                "desc":  r["desc"],
                                "category": r.get("category") or "normal",
                            })
                        st.session_state.custom_images = cur[:4]
                        st.success("Imágenes añadidas a la factura.")



    # ---- Specs: shown but NOT saved anywhere (different for hidráulico vs tracción) ----
    with st.expander("📐 Especificaciones (se incluyen SIEMPRE en el Word; no se guardan en BD)"):
            if hydraulic_cylinder:
                # HIDRÁULICO
                st.text_input("Tamaño de la plataforma interna (mm)", "", key="spec_plataforma_interna")
                st.text_input("Altura de la cabina (mm)",              "", key="spec_altura_cabina_h")
                st.text_input("Tamaño del ducto (mm)",                 "", key="spec_tamano_ducto_h")
                st.text_input("Puerta de cabina",                      "", key="spec_puerta_cabina_h")
            else:
                # TRACCIÓN
                c1, c2 = st.columns(2)
                with c1:
                    st.text_input("Dimensiones del ducto: Ancho (mm)",        "", key="spec_ducto_ancho")
                    st.text_input("Dimensiones de cabina: Ancho (mm)",         "", key="spec_cab_ancho")
                    st.text_input("Sobre recorrido (mm)",                       "", key="spec_sobre_recorrido")
                with c2:
                    st.text_input("Dimensiones del ducto: Fondo (mm)",         "", key="spec_ducto_fondo")
                    st.text_input("Dimensiones de cabina: Fondo (mm)",         "", key="spec_cab_fondo")
                    st.text_input("Dimensiones de cabina: Altura libre (mm)",  "", key="spec_cab_altura")
                st.text_input("Dimensión del pozo (mm)", "", key="spec_pozo_dim")

    # ---- Preview current custom images ----
    imgs = st.session_state.get("custom_images", [])
    if imgs:
            st.markdown("**Vista previa (máx. 4):**")
            cols = st.columns(4)
            for i, img in enumerate(imgs[:4]):
                with cols[i]:
                    st.image(img["bytes"], use_container_width=True)
                    if img.get("title"):
                        st.caption(f"**{img['title']}** – {img.get('desc','')}")




        
    # BEFORE "📝 Generar Invoice" button, or inside its block right before building images_info:

    imgs_all = st.session_state.get("custom_images") or []
    imgs_h = [i for i in imgs_all if (i.get("category") or "normal") == "hydraulic"]
    imgs_n = [i for i in imgs_all if (i.get("category") or "normal") == "normal"]

    choice_default = "auto"
    img_set_choice = st.selectbox(
        "Conjunto de imágenes a usar en el Word",
        options=["auto","hydraulic","normal","both"],
        index=0,
        format_func=lambda v: {
            "auto":"Auto (según equipo)",
            "hydraulic":"Solo hidráulicas",
            "normal":"Solo normales",
            "both":"Ambos (hidráulicas y normales)",
        }[v],
        help="Auto: si es hidráulico usa hidráulicas; si no, usa normales."
    )

    def _pick_images_for_export():
        if img_set_choice == "hydraulic":
            return imgs_h
        if img_set_choice == "normal":
            return imgs_n
        if img_set_choice == "both":
            # hydraulic first, then normal; cap at 4 total
            return (imgs_h + imgs_n)[:4]
        # auto
        base = imgs_h if hydraulic_cylinder else imgs_n
        if base:
            return base[:4]
        # fallback if the preferred set is empty
        alt = imgs_n if hydraulic_cylinder else imgs_h
        return alt[:4]

    images_to_use = _pick_images_for_export()

        # WORD-DOC BUTTON
    if st.button("📝 Generar Invoice en Word"):
                is_hydraulic = bool(hydraulic_cylinder)
                tpl_path = TEMPLATE_HYDRAULIC if is_hydraulic else TEMPLATE_TRACTION
                tpl = DocxTemplate(tpl_path)

                # --- Images: required for BOTH modes (no defaults) ---
                custom = st.session_state.get("custom_images") or []
                if not images_to_use:
                    st.error("Agrega al menos 1 imagen del conjunto seleccionado antes de generar.")
                    st.stop()

                images_info = []
                for ci in images_to_use:
                    inline = InlineImage(tpl, io.BytesIO(ci["bytes"]), width=Mm(50))
                    caption = (ci.get("title") or "").strip()
                    if ci.get("desc"):
                        caption = (caption + " — " + ci["desc"].strip()).strip(" —")
                    images_info.append({"image": inline, "text": caption})

                # --- Build context (specs always included; obra civil separate) ---
                context = {
                    "date": date_str,
                    "customer_name": customer,
                    "floors": F,
                    "persons": P,
                    "door_text": door_text,
                    "machine_room_text": "Con cuarto de máquinas" if machine else "Sin cuarto de máquinas",
                    "encoder_text": "Con encoder" if encoder else "No encoder",
                    "capacity": f"{cap:.0f} kg" if cap else "n/a",  # 'cap' from compute_lines()
                    "ubicacion": ubicacion,
                    "shipping_cost": f"${precio_envio:,.2f}",
                    "lines": st.session_state.get("invoice_lines", lines),
                    "grand_total": f"${st.session_state.get('total_iva_edited', total_iva):,.2f}",
                    "grand_venta": f"${st.session_state.get('total_venta_edited', total_venta):,.2f}",
                    "images": images_info,
                    "include_civil": include_civil,    # obra civil stays independent
                    "is_hydraulic": is_hydraulic,      # handy for template conditionals
                }

                # Always include specs in the document (not saved to DB)
                if is_hydraulic:
                    context.update({
                        "plataforma_interna": st.session_state.get("spec_plataforma_interna", ""),
                        "altura_cabina":      st.session_state.get("spec_altura_cabina_h", ""),
                        "tamano_ducto":       st.session_state.get("spec_tamano_ducto_h", ""),
                        "puerta_cabina":      st.session_state.get("spec_puerta_cabina_h", ""),
                    })
                else:
                    context.update({
                        "ducto_ancho":     st.session_state.get("spec_ducto_ancho", ""),
                        "ducto_fondo":     st.session_state.get("spec_ducto_fondo", ""),
                        "cab_ancho":       st.session_state.get("spec_cab_ancho", ""),
                        "cab_fondo":       st.session_state.get("spec_cab_fondo", ""),
                        "cab_altura":      st.session_state.get("spec_cab_altura", ""),
                        "sobre_recorrido": st.session_state.get("spec_sobre_recorrido", ""),
                        "pozo_dim":        st.session_state.get("spec_pozo_dim", ""),
                    })

                # Render + download
                tpl.render(context)
                buf = io.BytesIO()
                tpl.save(buf)
                buf.seek(0)

                out_name = f"Invoice_{customer}_{pd.Timestamp('now').strftime('%Y%m%d_%H%M')}.docx"
                st.download_button(
                    "📄 Descargar Invoice",
                    buf,
                    file_name=out_name,
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )

                # Save to DB (images for BOTH modes)
                if st.checkbox("Guardar copia en el archivo (base de datos)", value=True, key="save_archive_ck"):
                    try:
                        invoice_id = save_invoice_blob(
                            customer=customer,
                            ubicacion=ubicacion,
                            P=P, F=F,
                            control=control,
                            door_key=door_key,
                            machine=machine,
                            hydraulic_cylinder=hydraulic_cylinder,
                            grand_venta=st.session_state.get('total_venta_edited', total_venta),
                            grand_total=st.session_state.get('total_iva_edited', total_iva),
                            filename=out_name,
                            file_bytes=buf.getvalue(),
                        )
                        if st.session_state.get("custom_images"):
                            save_invoice_images(invoice_id, st.session_state.custom_images)
                        st.success("✅ Guardado en el archivo.")
                    except Exception as e:
                        st.error(f"❌ No se pudo guardar en el archivo: {e}")
            


# ── Archive tab: list past invoices ─────────────────────────────────────────────
with tab_arch:
    _run_schema()
    st.divider()
    st.subheader("🗂️ Archivo de facturas")

    colA, colB = st.columns([1,2])
    with colA:
        max_rows = st.number_input("Cuántas ver", min_value=5, max_value=500, value=50, step=5)
    with colB:
        search = st.text_input("Buscar por cliente (opcional)")

    rows = list_invoices(limit=max_rows)
    if search:
        s = search.strip().lower()
        rows = [r for r in rows if s in (r["customer"] or "").lower()]

    if not rows:
        st.info("No hay facturas guardadas todavía.")
    else:
        df = pd.DataFrame([
            {
                "ID": r["id"],
                "Fecha": pd.to_datetime(r["created_at"]).strftime("%Y-%m-%d %H:%M"),
                "Cliente": r["customer"],
                "Ubicación": r["ubicacion"],
                "Personas": r["persons"],
                "Paradas": r["floors"],
                "Control": r["control_type"],
                "Puertas": r["door_type"],
                "Cuarto Máq.": "Sí" if r["machine_room"] else "No",
                "Hidráulico": "Sí" if r["hydraulic"] else "No",
                "SubTotal": r["grand_venta"],
                "Total": r["grand_total"],
                "Archivo": r["filename"],
            } for r in rows
        ])
        st.dataframe(df, use_container_width=True)

        sel_id = st.number_input("ID para descargar", min_value=1, step=1, value=int(rows[0]["id"]))
        if st.button("⬇️ Descargar archivo guardado", key="download_saved"):
            rec = fetch_invoice_file(int(sel_id))
            if rec is None:
                st.error("No se encontró el ID.")
            else:
                fname, fbytes = rec
                st.download_button(
                    "Descargar",
                    io.BytesIO(fbytes),
                    file_name=fname,
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
        if st.button("🗑️ Eliminar factura", key="delete_invoice"):
            try:
                 delete_invoice(int(sel_id))
                 st.success(f"Factura con ID {sel_id} eliminada.")
            except Exception as e:
                st.error(f"No se pudo eliminar la factura: {e}")
        # --- Image gallery for this invoice ---
        st.markdown("### 📷 Imágenes guardadas")
        imgs_db = get_invoice_images(int(sel_id))
        if not imgs_db:
            st.info("Esta factura no tiene imágenes guardadas aún.")
        else:
            cols = st.columns(4)
            for i, img in enumerate(imgs_db):
                with cols[i % 4]:
                    st.image(img["image_bytes"], use_container_width=True)
                    title = (img.get("title") or "").strip()
                    desc  = (img.get("description") or "").strip()
                    if title or desc:
                        st.caption(f"**{title}** — {desc}".strip(" —"))

                    st.download_button(
                        "Descargar imagen",
                        data=io.BytesIO(img["image_bytes"]),
                        file_name=f"invoice_{sel_id}_img_{img['id']}.jpg",
                        key=f"dlimg_{img['id']}"
                    )
                    if st.button("Eliminar imagen", key=f"delimg_{img['id']}"):
                        delete_invoice_image(img["id"])
                        st.success("Imagen eliminada.")
                        st.rerun()  # refresh gallery

        # --- Add more images to this invoice ---
        with st.expander("➕ Agregar imágenes a esta factura"):
            up_more = st.file_uploader(
                "Sube 1–4 imágenes", type=["png","jpg","jpeg"], accept_multiple_files=True, key="arch_add_uploader"
            )
            add_title = st.text_input("Título (opcional)", key="arch_img_title")
            add_desc  = st.text_area("Descripción (opcional)", key="arch_img_desc")
            if st.button("Guardar imágenes", key="arch_img_save"):
                new_imgs = []
                for f in (up_more or [])[:4]:
                    new_imgs.append({"title": add_title, "desc": add_desc, "bytes": f.read()})
                if new_imgs:
                    save_invoice_images(int(sel_id), new_imgs)
                    st.success("Imágenes agregadas.")
                    st.rerun()
                else:
                    st.info("No subiste imágenes.")


