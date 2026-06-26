"""
fetch_data.py
Lee órdenes y líneas de ZAS CHILE desde Odoo vía Claude MCP
y genera data.json en la raíz del repo.

Requiere variables de entorno:
  ANTHROPIC_API_KEY
  ODOO_EMAIL
  ODOO_PASSWORD
"""

import json
import os
import sys
from datetime import datetime, timezone
import urllib.request
import urllib.error

CLAUDE_API = "https://api.anthropic.com/v1/messages"
MCP_URL    = "https://zas-agent-api.quantumsur.ai/api/mcp"
MODEL      = "claude-sonnet-4-6"

# Dólar observado promedio mensual BCCH (serie F073)
FX = {
    "2024-01":969,"2024-02":981,"2024-03":988,"2024-04":960,"2024-05":897,
    "2024-06":931,"2024-07":943,"2024-08":938,"2024-09":942,"2024-10":952,
    "2024-11":971,"2024-12":997,"2025-01":1010,"2025-02":998,"2025-03":990,
    "2025-04":969,"2025-05":944,"2025-06":951,"2025-07":953,"2025-08":945,
    "2025-09":955,"2025-10":963,"2025-11":971,"2025-12":981,"2026-01":988,
    "2026-02":970,"2026-03":960,"2026-04":945,"2026-05":935,"2026-06":942,
    "2026-07":948,"2026-08":945,"2026-09":950,"2026-10":955,"2026-11":960,
    "2026-12":965,
}

def mes_key(fecha):
    return fecha[:7] if fecha else "0000-00"

def to_clp(monto, moneda, fecha):
    if not moneda or moneda == "CLP":
        return float(monto)
    if moneda == "USD":
        rate = FX.get(mes_key(fecha), 950)
        return float(monto) * rate
    return float(monto)

def parse_talento(nombre):
    if not nombre:
        return None
    idx = nombre.find("(")
    raw = nombre[:idx].strip() if idx > 0 else nombre.strip()
    return raw.upper()

def llamar_claude(system, user):
    api_key  = os.environ.get("ANTHROPIC_API_KEY", "")
    email    = os.environ.get("ODOO_EMAIL", "")
    password = os.environ.get("ODOO_PASSWORD", "")

    if not api_key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY")
    if not email or not password:
        raise RuntimeError("Faltan ODOO_EMAIL o ODOO_PASSWORD")

    payload = json.dumps({
        "model": MODEL,
        "max_tokens": 8000,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "mcp_servers": [{
            "type": "url",
            "url": MCP_URL,
            "name": "zas",
            "authorization_token": f"{email}:{password}"
        }]
    }).encode()

    req = urllib.request.Request(
        CLAUDE_API,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "mcp-client-2025-04-04"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code}: {body[:300]}")

def extraer_rows(data):
    rows = []
    for blk in data.get("content", []):
        if blk.get("type") != "mcp_tool_result":
            continue
        try:
            parsed = json.loads(blk.get("content", [{}])[0].get("text", "{}"))
            rows.extend(parsed.get("result", {}).get("rows", []))
        except Exception:
            pass
    return rows

def fetch_ordenes():
    print("→ Fetching órdenes ZAS CHILE...")
    data = llamar_claude(
        "Eres un extractor de datos Odoo. Usá la herramienta MCP odoo_search_rows directamente sin preguntar.",
        """Llamá odoo_search_rows con:
- model: "sale.order"
- filters: [{"field":"x_studio_bu_1","operator":"eq","value":"ZAS CHILE"}]
- order: "odoo_id_desc"
- limit: 50"""
    )
    rows = extraer_rows(data)
    print(f"   {len(rows)} órdenes recibidas")
    return rows

def fetch_lineas(ids):
    if not ids:
        return []
    print(f"→ Fetching líneas para {len(ids)} órdenes...")
    data = llamar_claude(
        "Eres un extractor de datos Odoo. Usá la herramienta MCP odoo_search_rows directamente.",
        f"""Llamá odoo_search_rows con:
- model: "sale.order.line"
- filters: [{{"field":"order_id","operator":"in","value":[{','.join(map(str, ids))}]}}]
- order: "odoo_id_asc"
- limit: 100"""
    )
    rows = extraer_rows(data)
    print(f"   {len(rows)} líneas recibidas")
    return rows

def procesar(raw_ordenes, raw_lineas):
    ordenes = []
    for r in raw_ordenes:
        p = r.get("latestPayload", {})
        if p.get("x_studio_bu_1") != "ZAS CHILE":
            continue
        if p.get("state") in ("cancel", "draft"):
            continue
        moneda   = (p.get("currency_id") or [None, "CLP"])[1] or "CLP"
        fecha    = (p.get("date_order") or "")[:10]
        net_orig = float(p.get("amount_untaxed") or 0)
        net_clp  = to_clp(net_orig, moneda, fecha)
        tipo_raw = p.get("x_studio_factura_o_boleta") or ""
        tipo     = "Factura" if tipo_raw == "Factura" else "Boleta" if tipo_raw == "Boleta" else ""
        ordenes.append({
            "id":       r.get("odooId"),
            "name":     p.get("name", ""),
            "fecha":    fecha,
            "mes":      mes_key(fecha),
            "moneda":   moneda,
            "net_orig": net_orig,
            "net_clp":  net_clp,
            "tipo":     tipo,
            "line_ids": p.get("order_line", []),
        })

    ord_idx = {o["id"]: o for o in ordenes}

    lineas = []
    for r in raw_lineas:
        p       = r.get("latestPayload", {})
        if p.get("display_type"):
            continue
        oid     = (p.get("order_id") or [None])[0]
        ord     = ord_idx.get(oid)
        moneda  = ord["moneda"] if ord else (p.get("currency_id") or [None, "CLP"])[1] or "CLP"
        fecha   = ord["fecha"]  if ord else ""
        sub     = float(p.get("price_subtotal") or 0)
        talento = parse_talento(p.get("name", ""))
        if not talento or len(talento) < 2:
            continue
        lineas.append({
            "order_id": oid,
            "talento":  talento,
            "sub_clp":  to_clp(sub, moneda, fecha),
            "mes":      ord["mes"] if ord else mes_key(fecha),
            "tipo":     ord["tipo"] if ord else "",
        })

    return ordenes, lineas

def agregar_general(ordenes):
    por_mes = {}
    for o in ordenes:
        m = o["mes"]
        if m not in por_mes:
            por_mes[m] = {"f": 0, "b": 0, "n": 0, "usd": False}
        if o["moneda"] == "USD":
            por_mes[m]["usd"] = True
        if   o["tipo"] == "Factura": por_mes[m]["f"] += o["net_clp"]
        elif o["tipo"] == "Boleta":  por_mes[m]["b"] += o["net_clp"]
        else:                        por_mes[m]["n"] += o["net_clp"]
    return por_mes

def agregar_talentos(lineas):
    por_tal = {}
    for l in lineas:
        t, m = l["talento"], l["mes"]
        if t not in por_tal:
            por_tal[t] = {}
        if m not in por_tal[t]:
            por_tal[t][m] = {"f": 0, "b": 0, "n": 0}
        if   l["tipo"] == "Factura": por_tal[t][m]["f"] += l["sub_clp"]
        elif l["tipo"] == "Boleta":  por_tal[t][m]["b"] += l["sub_clp"]
        else:                        por_tal[t][m]["n"] += l["sub_clp"]
    return por_tal

def main():
    print("=== fetch_data.py ===")
    print(f"Inicio: {datetime.now(timezone.utc).isoformat()}")

    raw_ord = fetch_ordenes()

    ids_validos = [
        r.get("odooId") for r in raw_ord
        if r.get("latestPayload", {}).get("x_studio_bu_1") == "ZAS CHILE"
        and r.get("latestPayload", {}).get("state") not in ("cancel", "draft")
    ][:40]

    raw_lin = []
    for i in range(0, len(ids_validos), 20):
        batch = fetch_lineas(ids_validos[i:i+20])
        raw_lin.extend(batch)

    ordenes, lineas = procesar(raw_ord, raw_lin)
    print(f"   {len(ordenes)} órdenes válidas, {len(lineas)} líneas")

    general  = agregar_general(ordenes)
    talentos = agregar_talentos(lineas)

    output = {
        "updated_at":    datetime.now(timezone.utc).isoformat(),
        "total_ordenes": len(ordenes),
        "general":       general,
        "talentos":      talentos,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"✓ data.json generado")
    print(f"  Meses: {sorted(general.keys())}")
    print(f"  Talentos encontrados: {len(talentos)}")

if __name__ == "__main__":
    main()
