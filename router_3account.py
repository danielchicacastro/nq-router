"""
Lucid Router 3-Account — TradingView Webhook → Tradovate
=========================================================
Cuenta A (LONDON)  → todos los trades de Londres — SL=25, TP=12.5 (RR 0.5)
Cuenta B (NY CONT) → solo JJ NY Continuation    — SL variable (25 o 50 según webhook)
Cuenta C (NY MR)   → solo JJ NY Mean Reversion  — SL variable (25 o 50 según webhook)

Para sustituir una cuenta quemada: cambia ACCOUNT_LONDON / ACCOUNT_NY_CONT / ACCOUNT_NY_MR
en las variables de entorno de Railway. Sin tocar código.

Formato webhook esperado desde TradingView:
{
  "secret":  "tu_clave",
  "action":  "buy" | "sell",
  "comment": "IFVG Long" | "NY CONT Long" | "NY MR Long" | ...,
  "sl":      25 | 50,      ← añadido por el indicador para señales JJ
  "tp":      12.5 | 37.5 | 75   ← añadido por el indicador para señales JJ
}
Para señales de Londres el router usa SL/TP fijos (no necesita sl/tp en el webhook).
"""

from flask import Flask, request, jsonify
import requests
import os
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Configuración ──────────────────────────────────────────────────────────────
TRADOVATE_USER   = os.environ.get("TRADOVATE_USER",   "")
TRADOVATE_PASS   = os.environ.get("TRADOVATE_PASS",   "")
TRADOVATE_DEVICE = os.environ.get("TRADOVATE_DEVICE", "lucid-router-001")
TRADOVATE_CID    = os.environ.get("TRADOVATE_CID",    "")
TRADOVATE_SECRET = os.environ.get("TRADOVATE_SECRET", "")
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET",   "cambiar_esto")
TRADOVATE_ENV    = os.environ.get("TRADOVATE_ENV",    "demo")

# Las 3 cuentas — para sustituir una cuenta quemada, cambia SOLO la variable correspondiente
ACCOUNT_LONDON   = os.environ.get("ACCOUNT_LONDON",   "LFE05079702600002")
ACCOUNT_NY_CONT  = os.environ.get("ACCOUNT_NY_CONT",  "LFE05079702600004")
ACCOUNT_NY_MR    = os.environ.get("ACCOUNT_NY_MR",    "LFE05079702600003")

# SL/TP fijos de Londres (RR 0.5, Eval mode)
LONDON_SL = 25.0
LONDON_TP = 12.5

TICKER = "NQ"

# Comments que identifican cada tipo de señal
LONDON_COMMENTS = {
    "IFVG Long", "IFVG Short",
    "CPB Long Limit", "CPB Short Limit",
    "CPB MKT Long", "CPB MKT Short",
    "BOS Long", "BOS Short",
    "DISP Long", "DISP Short",
    "BOS+DISP Long", "BOS+DISP Short",
}
NY_CONT_COMMENTS = {"NY CONT Long", "NY CONT Short"}
NY_MR_COMMENTS   = {"NY MR Long",   "NY MR Short"}

state = {"access_token": None, "account_ids": {}}


def get_url(path):
    base = "https://live.tradovateapi.com/v1" if TRADOVATE_ENV == "live"            else "https://demo.tradovateapi.com/v1"
    return f"{base}{path}"


def login():
    payload = {
        "name": TRADOVATE_USER, "password": TRADOVATE_PASS,
        "appId": "lucid-3router", "appVersion": "1.0",
        "deviceId": TRADOVATE_DEVICE, "cid": TRADOVATE_CID, "sec": TRADOVATE_SECRET,
    }
    try:
        r = requests.post(get_url("/auth/accesstokenrequest"), json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "accessToken" in data:
            state["access_token"] = data["accessToken"]
            log.info("Login Tradovate OK")
            return True
        log.error(f"Login fallido: {data}")
        return False
    except Exception as e:
        log.error(f"Login error: {e}")
        return False


def hdrs():
    if not state["access_token"]:
        login()
    return {"Authorization": f"Bearer {state['access_token']}", "Content-Type": "application/json"}


def resolve_accounts():
    try:
        r = requests.get(get_url("/account/list"), headers=hdrs(), timeout=10)
        r.raise_for_status()
        for acc in r.json():
            name = acc.get("name", "")
            if name in (ACCOUNT_LONDON, ACCOUNT_NY_CONT, ACCOUNT_NY_MR):
                state["account_ids"][name] = acc["id"]
                log.info(f"Cuenta resuelta: {name} → id={acc['id']}")
    except Exception as e:
        log.error(f"Error resolviendo cuentas: {e}")


def place_order(account_name, action, sl_pts, tp_pts):
    account_id = state["account_ids"].get(account_name)
    if not account_id:
        resolve_accounts()
        account_id = state["account_ids"].get(account_name)
        if not account_id:
            return {"error": f"ID no encontrada para {account_name}"}

    side = "Buy" if action == "buy" else "Sell"
    body = {
        "accountSpec": account_name,
        "accountId":   account_id,
        "action":      side,
        "symbol":      TICKER,
        "orderQty":    1,
        "orderType":   "Market",
        "isAutomated": True,
        "bracket1": {
            "action":    "Sell" if side == "Buy" else "Buy",
            "orderType": "Stop",
            "stopPrice": f"@-{sl_pts}",
        },
        "bracket2": {
            "action":     "Sell" if side == "Buy" else "Buy",
            "orderType":  "Limit",
            "limitPrice": f"@+{tp_pts}",
        },
    }
    try:
        r = requests.post(get_url("/order/placeOrder"), headers=hdrs(), json=body, timeout=10)
        r.raise_for_status()
        result = r.json()
        log.info(f"Orden {side} SL={sl_pts} TP={tp_pts} → {account_name}: {result}")
        return result
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            login()
            r = requests.post(get_url("/order/placeOrder"), headers=hdrs(), json=body, timeout=10)
            return r.json()
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True) or {}

    if data.get("secret") != WEBHOOK_SECRET:
        log.warning(f"Secret inválido desde {request.remote_addr}")
        return jsonify({"error": "unauthorized"}), 401

    action  = data.get("action", "").lower()
    comment = data.get("comment", "")
    log.info(f"Webhook → action={action!r} comment={comment!r}")

    if action not in ("buy", "sell"):
        return jsonify({"status": "ignored", "action": action})

    # ── Londres: SL/TP siempre fijos ─────────────────────────────────────────
    if comment in LONDON_COMMENTS:
        log.info(f"LONDON → {ACCOUNT_LONDON} SL={LONDON_SL} TP={LONDON_TP}")
        res = place_order(ACCOUNT_LONDON, action, LONDON_SL, LONDON_TP)
        return jsonify({"status": "ok", "account": ACCOUNT_LONDON,
                        "type": "LONDON", "sl": LONDON_SL, "tp": LONDON_TP, "result": res})

    # ── JJ NY Continuation: SL/TP vienen en el webhook ───────────────────────
    elif comment in NY_CONT_COMMENTS:
        sl = float(data.get("sl", 25))   # el indicador manda sl=25 o sl=50
        tp = float(data.get("tp", 37.5)) # el indicador manda tp=37.5 o tp=75
        log.info(f"NY CONT → {ACCOUNT_NY_CONT} SL={sl} TP={tp}")
        res = place_order(ACCOUNT_NY_CONT, action, sl, tp)
        return jsonify({"status": "ok", "account": ACCOUNT_NY_CONT,
                        "type": "NY_CONT", "sl": sl, "tp": tp, "result": res})

    # ── JJ NY Mean Reversion: SL/TP vienen en el webhook ─────────────────────
    elif comment in NY_MR_COMMENTS:
        sl = float(data.get("sl", 25))
        tp = float(data.get("tp", 37.5))
        log.info(f"NY MR → {ACCOUNT_NY_MR} SL={sl} TP={tp}")
        res = place_order(ACCOUNT_NY_MR, action, sl, tp)
        return jsonify({"status": "ok", "account": ACCOUNT_NY_MR,
                        "type": "NY_MR", "sl": sl, "tp": tp, "result": res})

    else:
        log.info(f"Comment no reconocido: {comment!r}")
        return jsonify({"status": "no_match", "comment": comment})


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "env":             TRADOVATE_ENV,
        "account_london":  ACCOUNT_LONDON,
        "account_ny_cont": ACCOUNT_NY_CONT,
        "account_ny_mr":   ACCOUNT_NY_MR,
        "resolved_ids":    state["account_ids"],
        "london_sl_tp":    f"SL={LONDON_SL} / TP={LONDON_TP}",
        "token_active":    bool(state["access_token"]),
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    log.info("=== Lucid 3-Account Router arrancando ===")
    log.info(f"Entorno: {TRADOVATE_ENV}")
    log.info(f"Londres ({LONDON_SL}/{LONDON_TP}) → {ACCOUNT_LONDON}")
    log.info(f"NY CONT (variable)               → {ACCOUNT_NY_CONT}")
    log.info(f"NY MR   (variable)               → {ACCOUNT_NY_MR}")
    if login():
        resolve_accounts()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
