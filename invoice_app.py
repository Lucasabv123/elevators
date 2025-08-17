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

# ── Configuration ─────────────────────────────────────────────────────────────
HERE     = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(HERE, "elevators.db")
TEMPLATE_TRACTION  = os.path.join(HERE, "invoice_template.docx")
TEMPLATE_HYDRAULIC = os.path.join(HERE, "invoice_template_hydraulic.docx")
IMG_DIR  = os.path.join(HERE, "images")

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")
engine   = sa.create_engine(DATABASE_URL)

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
          image_bytes BLOB
        )"""

    with engine.begin() as conn:
        conn.execute(text(parts_rules_sql))
        conn.execute(text(invoices_sql))
        conn.execute(text(images_sql))
    

# regex for weight fallback
WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kg", flags=re.IGNORECASE)


@st.cache_resource
def init_db():
    _run_schema()
    return True

DB_READY = init_db()
#helpers
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
        from sqlalchemy import text
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT filename, file_bytes FROM invoices WHERE id = :id"),
                {"id": int(inv_id)}
            ).one_or_none()
        return row  # (filename, file_bytes) or None

def delete_invoice(inv_id: int):
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM invoices WHERE id = :id"),
                {"id": int(inv_id)}
            )

def save_invoice_images(inv_id: int, imgs: list[dict]):
        # imgs: [{"title":..., "desc":..., "bytes":...}, ...]
        if not imgs:
            return
        with engine.begin() as conn:
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
                        SELECT id, title, description, image_bytes
                        FROM invoice_images
                        WHERE invoice_id = :id
                        ORDER BY id
                    """),
                    {"id": int(inv_id)}
                ).mappings().all()
            return list(rows)

def get_recent_images(limit: int = 24):
    """Return the most recent images saved across all invoices."""
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT id, title, description, image_bytes
                FROM invoice_images
                ORDER BY id DESC
                LIMIT :limit
            """),
            {"limit": int(limit)},
        ).mappings().all()
    return list(rows)

def delete_invoice_image(image_id: int):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM invoice_images WHERE id = :id"), {"id": int(image_id)})

#cuarto de maquinas la mano de obra cuesta valor adicional de 350|
#hidraulico y motor
def save_parts(df: pd.DataFrame):
    with engine.begin() as conn:
        df.to_sql("parts_rules", conn, if_exists="replace", index=False)

def safe_eval(expr, ctx):
    try:
        return eval(expr, {"__builtins__": None}, {**ctx, "ceil": ceil})
    except:
        return False



def compute_lines(ctx, parts_df, cabina_price, shipping_cost, cabina_cost):
    # filter by your condition expressions


    
    mask  = parts_df["condition_expr"] \
                    .astype(str) \
                    .apply(lambda e: bool(safe_eval(e.strip(), ctx)))
    rules = parts_df[mask]

    lines        = []
    total_iva    = 0.0
    total_venta  = 0.0
    caps         = []

    for _, part in rules.iterrows():
        qty = int(safe_eval(str(part["qty_formula"]), ctx) or 0)
        if qty <= 0:
            continue

        iva_up   = float(part.get("iva",   0.0))
        venta_up = float(part.get("venta", 0.0))
        costo_unit = float(part.get("costo", 0.0))


        # accumulate both totals
        total_iva   += qty * iva_up
        total_venta += qty * venta_up

        # weight/capacity logic unchanged
        uw = part.get("unit_weight", 0.0) or 0.0
        m  = WEIGHT_RE.search(part["description"])
        if uw == 0.0 and m:
            uw = float(m.group(1))
        if uw > 0:
            caps.append(qty * uw)

        # always show the IVA unit‐price in the itemized list
        lines.append({
        "Description": part["description"],
        "Qty":         qty,
        "Costo Unitario":  f"${costo_unit:,.2f}",
        "Total Costo":     f"${qty * costo_unit:,.2f}",
        "Unit Price (IVA)":  f"${iva_up:,.2f}",
        "Line Total (IVA)":  f"${qty * iva_up:,.2f}",
        "Unit Price (VTA)":  f"${venta_up:,.2f}",
        "Line Total (VTA)":  f"${qty * venta_up:,.2f}",
         })

    # Cabina one-off on both totals
    if cabina_price > 0:
        total_iva   += cabina_price
        total_venta += cabina_price
        lines.append({
           "Description":     "Cabina",
            "Qty":             1,
           "Costo Unitario":  f"${cabina_cost:,.2f}",
           "Total Costo":     f"${cabina_cost:,.2f}",
            "Unit Price (VTA)": f"${cabina_price:,.2f}",
            "Line Total (VTA)": f"${cabina_price:,.2f}",
           "Unit Price (IVA)": f"${cabina_price:,.2f}",
            "Line Total (IVA)": f"${cabina_price:,.2f}",
        })
    #envio
    if shipping_cost >= 0:
        total_iva   += shipping_cost
        total_venta += shipping_cost
        lines.append({
            "Description":    "Precio de envío",
            "Qty":            1,
            "Costo Unitario": f"$0.00",
            "Total Costo":    f"$0.00",
            "Unit Price (VTA)": f"${shipping_cost:,.2f}",
            "Line Total (VTA)": f"${shipping_cost:,.2f}",
            "Unit Price (IVA)": f"${shipping_cost:,.2f}",
            "Line Total (IVA)": f"${shipping_cost:,.2f}",
        })
        

    capacity = max(caps) if caps else None
    return lines, total_iva, total_venta, capacity

# ── Streamlit UI ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Elevator Invoice", layout="wide")

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

# --- Logo + title (flexbox so bottoms align) ---
logo_path = os.path.join("images", "logo.png")

def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

if os.path.exists(logo_path):
    st.markdown(
        f"""
        <div class="kmon-header">
          <img src="data:image/png;base64,{_b64(logo_path)}" class="kmon-logo" />
          <h1 class="kmon-title">La Casa del Ascensor</h1>
        </div>
        """,
        unsafe_allow_html=True,
    )
else:
    st.title("La Casa del Ascensor")

# --- Theme (blue + white) ---

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



    # ===== Control / Puertas / Cuarto de máquinas (side‑by‑side) =====
    col_ctrl, col_door, col_room = st.columns(3)

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
        door_opt = st.radio("Puertas", ("manuales", "automáticas"), horizontal=True)
        door_key  = "manuales" if door_opt == "manuales" else "automaticas"
        door_text = f"Puertas {door_opt}"

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
    
    # Precios: Envío / Cabina (venta) / Costo Cabina
    cShip, cCab, cCost = st.columns(3)
    with cShip:
        precio_envio = st.number_input("Precio de envío (USD)", min_value=0.0, value=0.0, step=1.0, format="%.2f")
    with cCab:
        cabina = st.number_input("🚪 Cabina (venta unitaria)", 0.0, step=0.01, format="%.2f")
    with cCost:
        costo_cabina = st.number_input("🏷️ Costo de Cabina (costo unitario)", 0.0, step=0.01, format="%.2f")




    parts_df = load_parts()
    ctx = {"P":P,
        "F":F,
        "machine_room":machine,
        "door_type":door_key,
        "control_type":control,
        "encoder":encoder,
        "hydraulic_cylinder": hydraulic_cylinder
        }
    if "show_preview" not in st.session_state:
        st.session_state.show_preview = False

    include_civil = st.checkbox("✏️ Incluir tabla de Trabajos de Obra Civil", value=False)
    shipping = precio_envio

    # 1) Unpack both totals
    lines, total_iva, total_venta, cap = compute_lines(ctx, parts_df, cabina, shipping, costo_cabina)
    if st.button("🔍 Previsualizar Invoice"):
        st.session_state.show_preview = not st.session_state.show_preview

    # ── Toggle for enabling the editable grid ────────────────────────────────────────
    # Toggle for enabling the editable grid
    edit_preview = st.checkbox(
        "✏️ Habilitar edición de la tabla de invoice",
        value=False,
        key="edit_preview_toggle"
    )

    if st.session_state.show_preview:
        # 1) Build your base DataFrame
        df_base = pd.DataFrame(lines)
        if "qty" in df_base.columns:
            df_base = df_base.rename(columns={"qty": "Qty"})

        st.subheader("📄 Previsualización de Invoice")

        # 2) Show editor only in edit mode
        if edit_preview:
            df_edit = st.data_editor(
                df_base,
                num_rows="dynamic",
                use_container_width=True,
                column_config={
                    "Description":      st.column_config.TextColumn("Descripción", disabled=False),
                    "Qty":              st.column_config.NumberColumn("Cantidad",),
                    "Costo Unitario":   st.column_config.TextColumn("Costo Unitario", disabled=True),
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

        # 3) Drop entirely blank rows
        working_df = working_df.dropna(
            subset=["Description", "Qty", "Unit Price (VTA)"],
            how="all"
        ).copy()

        # 4) Coerce Qty → float
        working_df["Qty"] = working_df["Qty"].fillna(0).astype(float)

        # 5) Coerce & parse Unit Price (VTA) **as strings first**  
        working_df["Unit Price (VTA)"] = (
            working_df["Unit Price (VTA)"]
                .fillna("0")          # fill blanks
                .astype(str)          # ensure it's text
                .str.replace(r"[\$,]", "", regex=True)
                .astype(float)        # safe cast to float
        )

        # 6) Recalculate IVA and line totals
        working_df["Unit Price (IVA)"] = working_df["Unit Price (VTA)"] * 1.15
        working_df["Line Total (VTA)"] = working_df["Qty"] * working_df["Unit Price (VTA)"]
        working_df["Line Total (IVA)"] = working_df["Qty"] * working_df["Unit Price (IVA)"]

        # 7) Format back to “$X,XXX.XX”
        for col in ["Unit Price (VTA)", "Unit Price (IVA)", "Line Total (VTA)", "Line Total (IVA)"]:
            working_df[col] = working_df[col].map(lambda x: f"${x:,.2f}")
        
        for c in ["Costo Unitario", "Total Costo"]:
            working_df[c] = working_df[c].map(
                lambda x: f"${float(str(x).replace('$','').replace(',','')):,.2f}"
            )

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
    # --- Upload new images and save to session ---
        with st.form("image_form"):
            up = st.file_uploader(
                "Sube 1–4 imágenes", type=["png","jpg","jpeg"], accept_multiple_files=True
            )
            img_title = st.text_input("Título para estas imágenes (opcional)")
            img_desc  = st.text_area("Descripción corta (opcional)")

            submitted = st.form_submit_button("Guardar imágenes")
            if submitted:
                imgs = []
                for f in (up or [])[:4]:
                    imgs.append({
                        "title": img_title.strip(),
                        "desc":  img_desc.strip(),
                        "bytes": f.read()
                    })
                if imgs:
                    st.session_state.custom_images = imgs
                    st.success("Imágenes guardadas.")
                else:
                    st.info("No subiste imágenes.")

        # --- Library of previously saved images (ALWAYS visible) ---
        st.markdown("##### 📚 Usar imágenes guardadas anteriormente")
        prev_imgs = get_recent_images(limit=24)

        if not prev_imgs:
            st.info("No hay imágenes guardadas todavía.")
        else:
            cols = st.columns(4)
            for i, im in enumerate(prev_imgs):
                with cols[i % 4]:
                    st.image(im["image_bytes"], use_container_width=True)
                    t = (im.get("title") or "").strip()
                    d = (im.get("description") or "").strip()
                    if t or d:
                        st.caption(f"**{t}** — {d}".strip(" —"))
                    st.checkbox("Usar", key=f"use_prev_{im['id']}")

            selected = [im for im in prev_imgs if st.session_state.get(f"use_prev_{im['id']}", False)]
            if st.button("➕ Añadir seleccionadas a esta factura", key="add_selected_prev"):
                if not selected:
                    st.info("No hay imágenes seleccionadas.")
                else:
                    current = st.session_state.get("custom_images", []) or []
                    add = [
                        {"title": im.get("title") or "", "desc": im.get("description") or "", "bytes": im["image_bytes"]}
                        for im in selected
                    ]
                    merged = (current + add)[:4]   # cap at 4
                    st.session_state.custom_images = merged
                    st.success(f"Se añadieron {len(merged) - len(current)} imagen(es) a esta factura.")


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




    

    # WORD-DOC BUTTON
    if st.button("📝 Generar Invoice en Word"):
            is_hydraulic = bool(hydraulic_cylinder)
            tpl_path = TEMPLATE_HYDRAULIC if is_hydraulic else TEMPLATE_TRACTION
            tpl = DocxTemplate(tpl_path)

            # --- Images: required for BOTH modes (no defaults) ---
            custom = st.session_state.get("custom_images") or []
            if not custom:
                st.error("Agrega al menos 1 imagen (máx 4) antes de generar.")
                st.stop()

            images_info = []
            for ci in custom[:4]:
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


