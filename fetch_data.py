"""
fetch_data.py - ZAS Chile - Factura vs Boleta
Fuente: Odoo (JSON-RPC) - ventas Campanas Chile
"""

import json, os, http.cookiejar, urllib.request, unicodedata, re
from datetime import datetime, timezone
from collections import defaultdict

ODOO_URL  = "https://zas-talent.odoo.com"
ODOO_DB   = "zas-talent"
ODOO_USER = "martina.boazzo@zastalents.com"
ODOO_PASS = os.environ.get("ODOO_PASSWORD", "")

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

def parse_talento(nombre):
    if not nombre:
        return None
    idx = nombre.find("(")
    raw = nombre[:idx].strip() if idx > 0 else nombre.strip()
    return raw.upper()

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
def procesar(orders, lines):
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

    # Resumen por talento por mes + detalle de ordenes
    talentos_mes = defaultdict(lambda: defaultdict(lambda: {"f":0.0,"b":0.0,"ordenes":[]}))

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

        # Guardar detalle de la orden para el drilldown
        if tipo in ("Factura", "Boleta"):
            talentos_mes[talento][mes]["ordenes"].append({
                "nombre": nombre,
                "orden":  ord_name,
                "tipo":   tipo,
                "monto_clp":  round(sub_clp),
                "monto_orig": round(sub_orig),
                "moneda": moneda,
                "fecha":  fecha,
            })

    # Convertir a dicts planos
    talentos_out = {}
    for t, meses in talentos_mes.items():
        talentos_out[t] = {}
        for m, vals in meses.items():
            talentos_out[t][m] = {
                "f": vals["f"],
                "b": vals["b"],
                "ordenes": vals["ordenes"],
            }
    return dict(general), talentos_out

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=== fetch_data.py - ZAS Chile ===")
    print(f"Inicio: {datetime.now(timezone.utc).isoformat()}")

    if not ODOO_PASS:
        raise RuntimeError("Falta ODOO_PASSWORD")

    odoo_auth()

    # Leer Odoo
    orders = fetch_ordenes()
    order_ids = [o["id"] for o in orders]
    lines = fetch_lineas(order_ids)

    print(f"\n  Procesando {len(orders)} ordenes, {len(lines)} lineas...")
    general, talentos = procesar(orders, lines)

    output = {
        "updated_at":    datetime.now(timezone.utc).isoformat(),
        "total_ordenes": len(orders),
        "general":       general,
        "talentos":      talentos,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n  ✓ data.json generado")
    print(f"  Meses: {sorted(general.keys())}")
    print(f"  Talentos: {len(talentos)}")

if __name__ == "__main__":
    main()
