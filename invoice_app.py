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

# ── Configuration ─────────────────────────────────────────────────────────────
HERE     = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(HERE, "elevators.db")
TEMPLATE = os.path.join(HERE, "invoice_template.docx")
IMG_DIR  = os.path.join(HERE, "images")

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")
engine   = sa.create_engine(DATABASE_URL)
    

# regex for weight fallback
WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kg", flags=re.IGNORECASE)



# ── Long descriptions for manuales vs automaticas
DOOR_DESCRIPTIONS = {
    "manuales": [
        # 4 items now
        "PAREDES Y PUERTAS: Fabricadas en acero inoxidable 430 cepillado dos lados panorámicos. TECHO Fabricado en acero inoxidable cepillado. ILUMINACION ecológica LED PANELES FRONTALES fabricados en acero inoxidable 430, cepillado. PANEL SUPERIOR DE CABINA fabricado en acero inoxidable 430 cepillado. PASAMANOS en acero inoxidable. Piso vinil. ",
        "PANEL Fabricado en acero inoxidable cepillado. Su diseño es producto de un estudio con el fin de obtener seguridad y rapidez en la localización de los botones de piso o de operación de puertas. INDICADOR DE PISOS: Con flecha direccional que indica la posición actual del ascensor y el sentido de viaje de este.BOTONES DE LLAMADAS: ovalados con sistema braille para no videntes, los cuales se encuentra la numeración del piso, iluminándose el momento que registra la llamada.",
        "PUERTAS MANUALES ABATIBLES.",
        "PANEL: Fabricado en acero inoxidable. BOTONES DE LLAMADAS: ovalados son sistema braille para no videntes, los cuales se encuentra la numeración de piso, iluminándose el momento que se registra la llamada.."
    ],
    "automaticas": [
        # 4 items
        "PAREDES Y PUERTAS: Fabricadas en acero inoxidable 430 cepillado fabricación nacional. TECHO Fabricado en acero inoxidable cepillado ILUMINACION LED. PANELES FRONTALES Fabricados en acero inoxidable 430 cepillado. PANEL SUPERIOR DE CABINA Fabricado en acero inoxidable 430 cepillado. PASAMANOS En acero inoxidable piso porcelanato a gusto del cliente adquirido e instalado por el mismo Espejo  a mediocuerpo.",
        "PANEL Fabricado en acero inoxidable cepillado. Su diseño es producto de un estudio con el fin de obtener seguridad y rapidez en la localización de los botones de piso o de operación de puertas. INDICADOR DE PISOS: Con flecha direccional que indica la posición actual del ascensor y el sentido de viaje de este. BOTONES DE LLAMADAS: Circulares con sistema braille para no videntes, los cuales se encuentra la numeración del piso, iluminándose el momento que registra la llamada. BOTONES DE OPERACIÓN DE PUERTAS:   Ubicados bajo los botones de llamadas de cabina. Permiten controlar la apertura y cerrada de puertas de acuerdo a los requerimientos del usuario. BOTON DE EMERGENCIA: Ubicado sobre los botones de llamadas de cabina.",
        "MARCOS, PUERTAS Y PANEL SUPERIOR: Fabricados en acero inoxidable cepillado Marco en tipo angosto de 5 cm. APERTURA DE PUERTAS: laterales automáticas.MEDIDAS DE ENTRADA: 800 X 2100.",
        "PANEL: Fabricado en acero inoxidable. BOTONES DE LLAMADAS: Circulares son sistema braille para no videntes, los cuales se encuentra la numeración de piso, iluminándose el momento que se registra la llamada. INDICADORES DE POSICION: Muestran la ubicación actual de los ascensores y la dirección de viaje de estos."
    ]
}

MOTOR_IMAGES = {
    True:  ("motor_with_room.jpg", "Máquina de tracción 1:1  VVVF, maca Canon o Akis dependiendo el inventario. Frenos electromagnéticos de doble zapata con sistema de frenado incorporado en el control de maniobras."),
    False: ("motor_without_room.jpg", "Máquina de tracción 2:1 de imanes permanentes VVVF. Marca Montanari (italiana). Frenos electromagnéticos de doble zapata con sistema de frenado incorporado en el control de   maniobras.")
}
CONTROL_IMAGES = {
    "Monarch": (
        "control_monarch.jpg",
        "CONTROL MONARCH: tarjeta de cabina NICE L-C-4007 5.5 kW 220V con instalación."
    ),
    "Heytech": (
        "control_heytech.jpg",
        "CONTROL HEYTECH: Integrado con propio acceso, conformado por un variador VVVF VECTORIAL marca HEYTECH controlando la velocidad del ascensor. Protección de sobre voltaje, falta de fase. Motor de aceleración y desaceleración especiales para ascensores, sistema de nivelación directa. Varias opciones programables como puerta normalmente abierta, retorno a piso principal, tiempo de ahorro. Comunicación serial con   cabina y botoneras de hall."
    ),
}


@st.cache_data
def load_parts():
    with engine.connect() as conn:
        df = pd.read_sql("SELECT * FROM parts_rules", conn)
    df.columns = [c.strip() for c in df.columns]
    return df

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
st.title("🏗️ Casa del Ascensor")

# --- Simple password gate ---
REQUIRED_PW = os.getenv("APP_PASSWORD")
if REQUIRED_PW:
    # keep users logged in during session
    if "authed" not in st.session_state:
        st.session_state.authed = False

    with st.sidebar:
        st.subheader("🔒 Acceso")
        pw = st.text_input("Password", type="password")
        if st.button("Entrar"):
            st.session_state.authed = (pw == REQUIRED_PW)
            if not st.session_state.authed:
                st.error("Password incorrecto")

    if not st.session_state.authed:
        st.stop()   # block the rest of the app until correct

# ── Admin: edit costo/venta ─────────────────────────────────────────────────────
with st.sidebar:
    st.header("🔧 Admin")

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
customer = st.text_input("Cliente")
date_str = st.text_input("Fecha", pd.Timestamp("today").strftime("%d/%m/%Y"))
ubicacion       = st.text_input("Ubicación")
P        = st.number_input("Número de personas",1,17,1)
F        = st.number_input("Número de paradas",1,50,1)
control  = st.radio("Tipo de control", ("Monarch","Heytech"))
encoder  = st.radio("¿Con encoder?", ("Sí","No"))=="Sí" if control=="Heytech" else False
machine  = st.radio("¿Con cuarto de máquinas?", ("Sí","No"))=="Sí"
precio_envio = st.number_input(
    "Precio de envío (USD)",
    min_value=0.0,
    value=0.0,
    step=1.0,
    format="%.2f"
)

cabina   = st.number_input("🚪 Cabina (venta unitaria)",0.0, step=0.01, format="%.2f")
costo_cabina = st.number_input("🏷️ Costo de Cabina (costo unitario)", 0.0, step=0.01, format="%.2f")
if P <= 3 and F <= 3:
    hydraulic_cylinder = st.radio(
        "¿Incluir cilindro hidráulico BTD-55?", ("Sí","No")) == "Sí"
else:
    hydraulic_cylinder = False
#remove this stray branch that sets an unused variable:
if not hydraulic_cylinder and P == 3 and F <= 3:
    machine_room = True


door_opt = st.radio("Puertas", ("manuales","automáticas"))
door_key = "manuales" if door_opt=="manuales" else "automaticas"
door_text= f"Puertas {door_opt}"

door_descs = DOOR_DESCRIPTIONS[door_key]

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




# WORD-DOC BUTTON
if st.button("📝 Generar Invoice en Word"):
    tpl = DocxTemplate(TEMPLATE)

    # build images_info as before
    images_info = []
    for idx, desc in enumerate(door_descs, start=1):
        fname    = f"{door_key}_{idx}.jpg"
        img_path = os.path.join(IMG_DIR, fname)
        img      = InlineImage(tpl, img_path, width=Mm(50))
        images_info.append({"image": img, "text": desc})

    #motor image
    motor_file, motor_desc = MOTOR_IMAGES[machine]
    motor_img = InlineImage(tpl, os.path.join(IMG_DIR, motor_file), width=Mm(50))
    images_info.append({"image": motor_img, "text": motor_desc})

    # control image
    fn, desc = CONTROL_IMAGES[control]
    ctrl_img = InlineImage(tpl,
                           os.path.join(IMG_DIR, fn),
                           width=Mm(50))
    
    
        

    # prepare context
    context = {
        "date":             date_str,
        "customer_name":    customer,
        "floors":           F,
        "persons":          P,
        "door_text":        door_text,
        "machine_room_text":"Con cuarto de máquinas" if machine else "Sin cuarto de máquinas",
        "encoder_text":     "Con encoder" if encoder else "No encoder",
        "capacity":         f"{cap:.0f} kg" if cap else "n/a",
        "ubicacion":        ubicacion,
        "shipping_cost":    f"${precio_envio:,.2f}",
        "lines":            st.session_state.get("invoice_lines", lines),
        "grand_total":      f"${st.session_state.get('total_iva_edited', total_iva):,.2f}",
        "grand_venta":      f"${st.session_state.get('total_venta_edited', total_venta):,.2f}",
        "images":           images_info,
        "motor_text":       motor_desc,
        "motor_image":      motor_img,
        "control_text":      desc,
        "control_image":    ctrl_img,
        "include_civil":    include_civil
    }

    port = int(os.getenv("PORT", "8501"))

    # render and offer download
    tpl.render(context)
    buf = io.BytesIO()
    tpl.save(buf)
    buf.seek(0)
    st.download_button(
        "📄 Descargar Invoice", buf,
        file_name=f"Invoice_{customer}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

   