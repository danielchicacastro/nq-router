"""
Lucid Router — TradingView Webhook → Tradovate Account Rotator
============================================================
Recibe señales de TradingView y las ejecuta en la cuenta activa de Lucid.
Rota automáticamente cuando:
  - 6 wins seguidos   → pausa la cuenta hasta mañana, pasa a la siguiente
  - 1 SL tocado       → pausa la cuenta hasta mañana, pasa a la siguiente
A medianoche Madrid: resetea el estado de todas las cuentas.

Despliegue: Railway.app o Render.com (gratis, 0 VPS)
"""

from flask import Flask, request, jsonify
import requests
import json
import os
import logging
from datetime import datetime, date
import pytz

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────
# Estas variables las configuras en Railway/Render como variables de entorno

TRADOVATE_USER     = os.environ.get("TRADOVATE_USER",     "tu_email@ejemplo.com")
TRADOVATE_PASS     = os.environ.get("TRADOVATE_PASS",     "tu_contraseña")
TRADOVATE_DEVICE   = os.environ.get("TRADOVATE_DEVICE",   "lucid-router-001")
TRADOVATE_CID      = os.environ.get("TRADOVATE_CID",      "")   # Client ID de la API
TRADOVATE_SECRET   = os.environ.get("TRADOVATE_SECRET",   "")   # Client Secret
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET",     "cambiar_esto_por_clave_segura")

# Modo live o demo — IMPORTANTE: usar "live" solo cuando estés seguro
TRADOVATE_ENV      = os.environ.get("TRADOVATE_ENV",      "demo")  # "demo" | "live"

ACCOUNTS = [
    "LFE05079702600002",   # Cuenta 1: $1,147 profit, 3 días
    "LFE05079702600004",   # Cuenta 2: limpia
    "LFE05079702600003",   # Cuenta 3: -$602
]

MAX_WINS_BEFORE_ROTATE = 6
MADRID_TZ = pytz.timezone("Europe/Madrid")
TICKER = "NQ"   # o MNQ para micro


# ── Estado en memoria (persiste mientras el servidor está activo) ─────────────
# En producción puedes usar Redis o un archivo JSON simple

state = {
    "active_idx":       0,         # índice en ACCOUNTS
    "wins_today":       0,         # wins consecutivos en la sesión actual
    "paused_accounts":  set(),     # cuentas pausadas hoy
    "last_reset_date":  None,      # fecha del último reset
    "access_token":     None,
    "token_expiry":     None,
    "account_ids":      {},        # nombre → id numérico de Tradovate
}


def get_tradovate_url(path: str) -> str:
    base = "https://live.tradovateapi.com/v1" if TRADOVATE_ENV == "live" \
           else "https://demo.tradovateapi.com/v1"
    return f"{base}{path}"


def tradovate_login() -> bool:
    """Obtiene un access token de Tradovate."""
    payload = {
        "name":       TRADOVATE_USER,
        "password":   TRADOVATE_PASS,
        "appId":      "lucid-router",
        "appVersion": "1.0",
        "deviceId":   TRADOVATE_DEVICE,
        "cid":        TRADOVATE_CID,
        "sec":        TRADOVATE_SECRET,
    }
    try:
        r = requests.post(get_tradovate_url("/auth/accesstokenrequest"),
                          json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "accessToken" in data:
            state["access_token"]  = data["accessToken"]
            state["token_expiry"]  = data.get("expirationTime")
            log.info("Login Tradovate OK")
            return True
        log.error(f"Login fallido: {data}")
        return False
    except Exception as e:
        log.error(f"Login error: {e}")
        return False


def get_headers() -> dict:
    if not state["access_token"]:
        tradovate_login()
    return {
        "Authorization": f"Bearer {state['access_token']}",
        "Content-Type":  "application/json",
    }


def resolve_account_ids():
    """Resuelve los nombres de cuenta a IDs numéricos de Tradovate."""
    try:
        r = requests.get(get_tradovate_url("/account/list"),
                         headers=get_headers(), timeout=10)
        r.raise_for_status()
        accounts = r.json()
        for acc in accounts:
            name = acc.get("name", "")
            if name in ACCOUNTS:
                state["account_ids"][name] = acc["id"]
                log.info(f"Cuenta resuelta: {name} → id={acc['id']}")
    except Exception as e:
        log.error(f"Error resolviendo cuentas: {e}")


def midnight_reset():
    """Resetea el estado al inicio de cada día Madrid."""
    today = datetime.now(MADRID_TZ).date()
    if state["last_reset_date"] != today:
        log.info(f"Reset diario — nueva fecha: {today}")
        state["wins_today"]      = 0
        state["paused_accounts"] = set()
        # Busca la primera cuenta no pausada (todas deberían estar disponibles)
        state["active_idx"]      = 0
        state["last_reset_date"] = today


def active_account_name() -> str | None:
    """Devuelve el nombre de la cuenta activa, o None si todas pausadas."""
    for i in range(len(ACCOUNTS)):
        idx  = (state["active_idx"] + i) % len(ACCOUNTS)
        name = ACCOUNTS[idx]
        if name not in state["paused_accounts"]:
            state["active_idx"] = idx
            return name
    return None  # todas pausadas


def rotate_account(reason: str):
    """Pausa la cuenta activa y pasa a la siguiente."""
    current = ACCOUNTS[state["active_idx"]]
    state["paused_accounts"].add(current)
    state["wins_today"] = 0
    log.info(f"ROTACIÓN [{reason}] — {current} pausada hoy")

    # Busca la siguiente disponible
    for i in range(1, len(ACCOUNTS) + 1):
        idx  = (state["active_idx"] + i) % len(ACCOUNTS)
        name = ACCOUNTS[idx]
        if name not in state["paused_accounts"]:
            state["active_idx"] = idx
            log.info(f"Cuenta activa ahora: {name} (wins_today=0)")
            return
    log.warning("TODAS LAS CUENTAS PAUSADAS — no se operará hasta mañana")


def place_order(account_name: str, action: str, qty: int = 1,
                sl_pts: float = 50.0, tp_pts: float = 25.0) -> dict:
    """
    Manda una orden bracket a Tradovate.
    action: "buy" | "sell"
    sl_pts / tp_pts: puntos NQ (1 punto NQ = $20)
    """
    account_id = state["account_ids"].get(account_name)
    if not account_id:
        return {"error": f"ID no encontrada para {account_name}"}

    side = "Buy" if action == "buy" else "Sell"

    # Precio de mercado como referencia para el bracket
    # Tradovate acepta Market + OSO (bracket) en un solo call
    order_body = {
        "accountSpec":  account_name,
        "accountId":    account_id,
        "action":       side,
        "symbol":       TICKER,
        "orderQty":     qty,
        "orderType":    "Market",
        "isAutomated":  True,   # requerido por CME para órdenes automatizadas
        "bracket1":  {           # Stop Loss
            "action":    "Sell" if side == "Buy" else "Buy",
            "orderType": "Stop",
            "stopPrice": f"@-{sl_pts}",   # relativo al fill
        },
        "bracket2":  {           # Take Profit
            "action":    "Sell" if side == "Buy" else "Buy",
            "orderType": "Limit",
            "limitPrice": f"@+{tp_pts}",  # relativo al fill
        },
    }

    try:
        r = requests.post(get_tradovate_url("/order/placeOrder"),
                          headers=get_headers(),
                          json=order_body, timeout=10)
        r.raise_for_status()
        result = r.json()
        log.info(f"Orden enviada a {account_name}: {side} {qty} {TICKER} → {result}")
        return result
    except requests.HTTPError as e:
        # Token expirado → relogin y reintento
        if e.response.status_code == 401:
            log.warning("Token expirado, relogin...")
            tradovate_login()
            r = requests.post(get_tradovate_url("/order/placeOrder"),
                              headers=get_headers(),
                              json=order_body, timeout=10)
            return r.json()
        log.error(f"Error HTTP orden: {e}")
        return {"error": str(e)}
    except Exception as e:
        log.error(f"Error orden: {e}")
        return {"error": str(e)}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Recibe alertas de TradingView.
    Formato esperado (ya está en tu indicador):
    {
      "secret":  "tu_clave_secreta",
      "action":  "buy" | "sell" | "sl_hit" | "tp_hit",
      "ticker":  "NQ",
      "price":   "{{close}}",
      "comment": "IFVG Long | Confirmation Pullback"
    }
    """
    # Verificar secret para seguridad
    data = request.get_json(force=True, silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        log.warning(f"Webhook con secret inválido desde {request.remote_addr}")
        return jsonify({"error": "unauthorized"}), 401

    action  = data.get("action", "").lower()
    comment = data.get("comment", "")
    log.info(f"Webhook recibido: action={action} comment={comment}")

    # Reset diario si es un nuevo día Madrid
    midnight_reset()

    # ── Resultado de trade cerrado ────────────────────────────────────────────
    if action in ("tp_hit", "sl_hit"):
        if action == "tp_hit":
            state["wins_today"] += 1
            log.info(f"TP HIT — wins consecutivos: {state['wins_today']}")
            if state["wins_today"] >= MAX_WINS_BEFORE_ROTATE:
                rotate_account(f"{MAX_WINS_BEFORE_ROTATE} wins seguidos")
        else:  # sl_hit
            log.info("SL HIT — rotando cuenta")
            rotate_account("SL tocado")
        return jsonify({"status": "ok", "action": action,
                        "wins_today": state["wins_today"],
                        "active_account": active_account_name()})

    # ── Nueva entrada ─────────────────────────────────────────────────────────
    if action in ("buy", "sell"):
        account = active_account_name()
        if not account:
            log.warning("Todas las cuentas pausadas hoy — señal ignorada")
            return jsonify({"status": "skipped",
                            "reason": "all accounts paused today"})

        # SL=50 pts, TP=25 pts → RR 0.5 (configuración Londres)
        result = place_order(account, action, qty=1,
                             sl_pts=50.0, tp_pts=25.0)
        return jsonify({"status": "order_sent",
                        "account": account,
                        "order": result,
                        "wins_today": state["wins_today"]})

    return jsonify({"status": "ignored", "action": action})


@app.route("/status", methods=["GET"])
def status():
    """Ver el estado actual del router."""
    midnight_reset()
    return jsonify({
        "active_account":   active_account_name(),
        "active_idx":       state["active_idx"],
        "wins_today":       state["wins_today"],
        "paused_accounts":  list(state["paused_accounts"]),
        "all_accounts":     ACCOUNTS,
        "env":              TRADOVATE_ENV,
        "last_reset":       str(state["last_reset_date"]),
    })


@app.route("/reset", methods=["POST"])
def manual_reset():
    """Reset manual del estado (para pruebas o emergencias)."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    state["wins_today"]      = 0
    state["paused_accounts"] = set()
    state["active_idx"]      = 0
    state["last_reset_date"] = None
    log.info("Reset manual ejecutado")
    return jsonify({"status": "reset_ok"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ── Arranque ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("=== Lucid Router arrancando ===")
    log.info(f"Entorno Tradovate: {TRADOVATE_ENV}")
    log.info(f"Cuentas configuradas: {ACCOUNTS}")

    # Login inicial y resolución de IDs
    if tradovate_login():
        resolve_account_ids()
        log.info(f"IDs resueltas: {state['account_ids']}")
    else:
        log.error("Login inicial fallido — verifica credenciales")

    midnight_reset()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
