"""
fetch_data.py - ZAS Chile - Factura vs Boleta + Deuda por Talento
Fuentes:
  - Odoo (JSON-RPC): ventas Campanas Chile
  - Google Sheets 1: % fee ZAS por talento por mes
  - Google Sheets 2: pagos realizados por ZAS a talentos (filtro ZAS CHILE)
"""

import json, os, http.cookiejar, urllib.request, unicodedata, re
from datetime import datetime, timezone
from collections import defaultdict

ODOO_URL  = "https://zas-talent.odoo.com"
ODOO_DB   = "zas-talent"
ODOO_USER = "martina.boazzo@zastalents.com"
ODOO_PASS = os.environ.get("ODOO_PASSWORD", "")

# Sheet 1: % fee por talento
SHEET_FEES_ID = "1V6JhXxa8de9MvAVwEv9lyEz4fji5rNQQqMkWZueSXrA"
# Sheet 2: pagos a talentos
SHEET_PAGOS_ID = "1xnXxQ30BgcxSmCT9opUeHLRIGOZpe4d4yB59hohMESw"

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

FX_CLP = {
    "2024-01":969,"2024-02":981,"2024-03":988,"2024-04":960,"2024-05":897,
    "2024-06":931,"2024-07":943,"2024-08":938,"2024-09":942,"2024-10":952,
    "2024-11":971,"2024-12":997,"2025-01":1010,"2025-02":998,"2025-03":990,
    "2025-04":969,"2025-05":944,"2025-06":951,"2025-07":953,"2025-08":945,
    "2025-09":955,"2025-10":963,"2025-11":971,"2025-12":981,"2026-01":988,
    "2026-02":970,"2026-03":960,"2026-04":945,"2026-05":935,"2026-06":942,
    "2026-07":948,"2026-08":945,"2026-09":950,"2026-10":955,"2026-11":960,
    "2026-12":965,
}

# ── Odoo ──────────────────────────────────────────────────────────────────────
_cj     = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))

def odoo_call(endpoint, payload):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        ODOO_URL + endpoint, data=data,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with _opener.open(req, timeout=120) as r:
        return json.loads(r.read())

def odoo_auth():
    print("  Autenticando en Odoo...")
    r = odoo_call("/web/session/authenticate", {
        "jsonrpc":"2.0","method":"call","id":1,
        "params":{"db":ODOO_DB,"login":ODOO_USER,"password":ODOO_PASS}
    })
    uid = r["result"]["uid"]
    if not uid:
        raise Exception("Autenticacion fallida")
    print(f"  OK UID={uid}")

def search_read(model, domain, fields, batch=500):
    all_recs, offset = [], 0
    while True:
        r = odoo_call("/web/dataset/call_kw", {
            "jsonrpc":"2.0","method":"call","id":2,
            "params":{
                "model":model,"method":"search_read","args":[domain],
                "kwargs":{
                    "fields":fields,"limit":batch,"offset":offset,
                    "context":{"allowed_company_ids":[1,2,3,4,5,6]}
                }
            }
        })
        recs = r.get("result")
        if recs is None:
            raise Exception(f"Error {model}: {r.get('error')}")
        all_recs.extend(recs)
        print(f"    {model}: {len(all_recs)}...", end="\r")
        if len(recs) < batch:
            break
        offset += batch
    print(f"    {model}: {len(all_recs)} registros        ")
    return all_recs

# ── Google Sheets ─────────────────────────────────────────────────────────────
def fetch_sheet(sheet_id, range_name):
    """Lee un rango de Google Sheets via API publica (requiere GOOGLE_API_KEY)"""
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
           f"/values/{urllib.request.quote(range_name)}?key={GOOGLE_API_KEY}")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("values", [])

def fetch_sheet_export(sheet_id, gid="0"):
    """Exporta como CSV si no hay API key disponible (requiere que el sheet sea publico)"""
    url = (f"https://docs.google.com/spreadsheets/d/{sheet_id}"
           f"/export?format=csv&gid={gid}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        content = r.read().decode("utf-8")
    rows = []
    for line in content.split("\n"):
        line = line.strip()
        if line:
            # CSV simple split (sin comillas complejas)
            rows.append([c.strip() for c in line.split(",")])
    return rows

# ── Utilidades ────────────────────────────────────────────────────────────────
def norm(s):
    """Normaliza nombre: mayusculas, sin acentos, sin espacios extra"""
    if not s:
        return ""
    s = str(s).strip().upper()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

def mes_key(fecha):
    return fecha[:7] if fecha else "0000-00"

def to_clp(monto, moneda, fecha):
    moneda = (moneda or "CLP").upper().strip()
    if moneda == "CLP":
        return float(monto or 0)
    if moneda == "USD":
        rate = FX_CLP.get(mes_key(fecha), 950)
        return float(monto or 0) * rate
    # Otras monedas: ignorar (no son ZAS CHILE)
    return float(monto or 0)

def parse_pct(val):
    """Convierte '30.0%' o '30' o 0.3 a float 0.30"""
    if val is None or val == "" or str(val).upper() in ("EXTERNO", "EX ZAS CON AGENCIA", "#N/A"):
        return None
    s = str(val).strip().replace("%", "").replace(",", ".")
    try:
        f = float(s)
        return f / 100 if f > 1 else f
    except:
        return None

def parse_monto(val):
    """Convierte '1,234,567' o '$1.234' a float"""
    if not val:
        return 0.0
    s = str(val).strip().replace("$", "").replace(",", "").replace(".", "")
    try:
        return float(s)
    except:
        # Intentar reemplazando coma decimal
        try:
            return float(str(val).strip().replace("$", "").replace(",", "."))
        except:
            return 0.0

def parse_talento(nombre):
    if not nombre:
        return None
    idx = nombre.find("(")
    raw = nombre[:idx].strip() if idx > 0 else nombre.strip()
    return raw.upper()

# ── Leer fees desde Sheet 1 ───────────────────────────────────────────────────
# Columnas del sheet: Creator | Country | Signing Date | Termination Date | ene-2024 | feb-2024 | ...
# Los meses arrancan en columna index 4
MES_COLS = [
    "ene-2024","feb-2024","mar-2024","abr-2024","may-2024","jun-2024",
    "jul-2024","ago-2024","sep-2024","oct-2024","nov-2024","dic-2024",
    "ene-2025","feb-2025","mar-2025","abr-2025","may-2025","jun-2025",
    "jul-2025","ago-2025","sep-2025","oct-2025","nov-2025","dic-2025",
    "ene-2026","feb-2026","mar-2026","abr-2026","may-2026","jun-2026",
    "jul-2026","ago-2026","sep-2026","oct-2026","nov-2026","dic-2026",
]

def load_fees(sheet_id):
    """
    Retorna dict: { norm(nombre): avg_pct_zas }
    avg_pct_zas es el promedio de los % en la fila (solo valores numericos, ignora EXTERNO/vacio)
    """
    print("  Leyendo fees desde Google Sheets...")
    fees = {}
    try:
        rows = fetch_sheet_export(sheet_id, gid="0")
    except Exception as e:
        print(f"  ⚠ No se pudo leer fees: {e}")
        return fees

    for row in rows:
        if not row or not row[0]:
            continue
        nombre = str(row[0]).strip()
        # Ignorar filas de encabezado
        if nombre.upper() in ("CREATOR", "NO_HEADER", ""):
            continue
        if len(nombre) < 2:
            continue

        # Leer todos los % de la fila (columnas 4 en adelante)
        pcts = []
        for i in range(4, len(row)):
            p = parse_pct(row[i])
            if p is not None:
                pcts.append(p)

        if pcts:
            avg = sum(pcts) / len(pcts)
            key = norm(nombre)
            fees[key] = avg
            print(f"    Fee {nombre}: {avg*100:.1f}% (prom de {len(pcts)} meses)")

    print(f"  {len(fees)} talentos con fee cargados")
    return fees

# ── Leer pagos desde Sheet 2 ──────────────────────────────────────────────────
# Columna F (idx 5): TALENTO, Columna O (idx 14): MONTO NETO, Columna P (idx 15): BU
def load_pagos(sheet_id):
    """
    Retorna dict: { norm(talento): total_clp_pagado }
    Solo filas donde BU == "ZAS CHILE"
    """
    print("  Leyendo pagos desde Google Sheets...")
    pagos = defaultdict(float)
    try:
        # El sheet principal esta en gid=1511391806 segun la URL
        rows = fetch_sheet_export(sheet_id, gid="1511391806")
    except Exception as e:
        print(f"  ⚠ No se pudo leer pagos: {e}")
        return dict(pagos)

    for row in rows:
        if len(row) < 16:
            continue
        talento_raw = row[5].strip() if len(row) > 5 else ""
        monto_raw   = row[14].strip() if len(row) > 14 else ""
        bu_raw      = row[15].strip() if len(row) > 15 else ""
        moneda_raw  = row[12].strip() if len(row) > 12 else "CLP"

        if norm(bu_raw) != "ZAS CHILE":
            continue
        if not talento_raw or talento_raw.upper() in ("TALENTO", "N/A", "EXTERNOS", ""):
            continue

        monto = parse_monto(monto_raw)
        if monto <= 0:
            continue

        # Convertir a CLP
        moneda = moneda_raw.upper().strip()
        clp = to_clp(monto, moneda, "")  # sin fecha usamos rate default

        key = norm(talento_raw)
        pagos[key] += clp

    print(f"  {len(pagos)} talentos con pagos de ZAS CHILE")
    for k, v in list(pagos.items())[:5]:
        print(f"    {k}: ${v:,.0f} CLP")
    return dict(pagos)

# ── Odoo utils ────────────────────────────────────────────────────────────────
def fetch_ordenes():
    print("\n  Descargando ordenes Campanas Chile...")
    for valor in ["Campa\u00f1as Chile", "Campanas Chile"]:
        orders = search_read(
            "sale.order",
            [
                ["x_studio_campaas_1", "=", valor],
                ["x_studio_factura_o_boleta", "in", ["Factura", "Boleta"]],
                ["state", "in", ["sale", "done"]]
            ],
            ["id","name","date_order","currency_id","amount_untaxed",
             "x_studio_factura_o_boleta","state"]
        )
        if orders:
            print(f"  Encontradas con: '{valor}'")
            break
    print(f"  {len(orders)} ordenes")
    return orders

def fetch_lineas(order_ids):
    if not order_ids:
        return []
    print("\n  Descargando lineas...")
    return search_read(
        "sale.order.line",
        [["order_id","in",order_ids],["state","in",["sale","done"]]],
        ["id","order_id","name","price_subtotal","currency_id"]
    )

# ── Procesamiento ─────────────────────────────────────────────────────────────
def procesar(orders, lines, fees, pagos):
    ord_idx = {o["id"]: o for o in orders}

    # Resumen general por mes
    general = defaultdict(lambda: {"f":0.0,"b":0.0,"usd":False})
    for o in orders:
        moneda  = (o.get("currency_id") or [None,"CLP"])[1] or "CLP"
        fecha   = (o.get("date_order") or "")[:10]
        mes     = mes_key(fecha)
        net_clp = to_clp(o.get("amount_untaxed",0), moneda, fecha)
        tipo    = o.get("x_studio_factura_o_boleta","")
        if moneda == "USD":
            general[mes]["usd"] = True
        if   tipo == "Factura": general[mes]["f"] += net_clp
        elif tipo == "Boleta":  general[mes]["b"] += net_clp

    # Resumen por talento por mes + acumulado para deuda + detalle de ordenes
    talentos_mes   = defaultdict(lambda: defaultdict(lambda: {"f":0.0,"b":0.0,"ordenes":[]}))
    talentos_total = defaultdict(float)  # total ventas por talento en CLP

    for l in lines:
        nombre  = l.get("name") or ""
        talento = parse_talento(nombre)
        if not talento or len(talento) < 2:
            continue
        oid    = (l.get("order_id") or [None])[0]
        orden  = ord_idx.get(oid)
        if not orden:
            continue
        moneda  = (orden.get("currency_id") or [None,"CLP"])[1] or "CLP"
        fecha   = (orden.get("date_order") or "")[:10]
        mes     = mes_key(fecha)
        sub_clp = to_clp(l.get("price_subtotal",0), moneda, fecha)
        sub_orig = float(l.get("price_subtotal",0) or 0)
        tipo    = orden.get("x_studio_factura_o_boleta","")
        ord_name = orden.get("name","")

        if   tipo == "Factura": talentos_mes[talento][mes]["f"] += sub_clp
        elif tipo == "Boleta":  talentos_mes[talento][mes]["b"] += sub_clp
        talentos_total[talento] += sub_clp

        # Guardar detalle de la orden (linea) para el drilldown
        if tipo in ("Factura", "Boleta"):
            talentos_mes[talento][mes]["ordenes"].append({
                "nombre": nombre,        # descripcion de la linea (ej: "Chris Moll (Reel Instagram)")
                "orden":  ord_name,      # numero de orden (ej: "S00123")
                "tipo":   tipo,
                "monto_clp":  round(sub_clp),
                "monto_orig": round(sub_orig),
                "moneda": moneda,
                "fecha":  fecha,
            })

    # Calcular deuda por talento
    deudas = {}
    for talento, total_ventas in talentos_total.items():
        key_norm = norm(talento)

        # Buscar fee en el sheet (match normalizado)
        fee_zas_pct = fees.get(key_norm)
        if fee_zas_pct is None:
            # Intentar match parcial
            for k, v in fees.items():
                if key_norm in k or k in key_norm:
                    fee_zas_pct = v
                    break
        if fee_zas_pct is None:
            fee_zas_pct = 0.30  # fallback 30%

        # Pagos ya realizados por ZAS al talento
        pagos_realizados = pagos.get(key_norm, 0.0)
        if pagos_realizados == 0:
            for k, v in pagos.items():
                if key_norm in k or k in key_norm:
                    pagos_realizados = v
                    break

        # Deuda = lo que le corresponde al talento - lo que ya le pagamos
        pct_talento    = 1 - fee_zas_pct
        le_corresponde = total_ventas * pct_talento
        deuda          = le_corresponde - pagos_realizados

        deudas[talento] = {
            "total_ventas":    round(total_ventas),
            "pct_zas":         round(fee_zas_pct * 100, 1),
            "pct_talento":     round(pct_talento * 100, 1),
            "le_corresponde":  round(le_corresponde),
            "pagos_realizados":round(pagos_realizados),
            "deuda":           round(deuda),
        }

    # Convertir a dicts planos (mantener ordenes como lista)
    talentos_out = {}
    for t, meses in talentos_mes.items():
        talentos_out[t] = {}
        for m, vals in meses.items():
            talentos_out[t][m] = {
                "f": vals["f"],
                "b": vals["b"],
                "ordenes": vals["ordenes"],
            }
    return dict(general), talentos_out, deudas

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=== fetch_data.py - ZAS Chile ===")
    print(f"Inicio: {datetime.now(timezone.utc).isoformat()}")

    if not ODOO_PASS:
        raise RuntimeError("Falta ODOO_PASSWORD")

    odoo_auth()

    # Leer sheets
    fees  = load_fees(SHEET_FEES_ID)
    pagos = load_pagos(SHEET_PAGOS_ID)

    # Leer Odoo
    orders = fetch_ordenes()
    order_ids = [o["id"] for o in orders]
    lines = fetch_lineas(order_ids)

    print(f"\n  Procesando {len(orders)} ordenes, {len(lines)} lineas...")
    general, talentos, deudas = procesar(orders, lines, fees, pagos)

    output = {
        "updated_at":    datetime.now(timezone.utc).isoformat(),
        "total_ordenes": len(orders),
        "general":       general,
        "talentos":      talentos,
        "deudas":        deudas,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n  ✓ data.json generado")
    print(f"  Meses: {sorted(general.keys())}")
    print(f"  Talentos: {len(talentos)}")
    print(f"  Deudas calculadas: {len(deudas)}")

if __name__ == "__main__":
    main()
