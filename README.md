# Lucid Router

## Qué hace
Recibe señales de TradingView y las ejecuta en tus 3 cuentas LucidFlex 50K,
rotando automáticamente:
- **6 wins seguidos** → pausa la cuenta activa hasta mañana, pasa a la siguiente
- **1 SL tocado**     → pausa la cuenta activa hasta mañana, pasa a la siguiente
- **Medianoche Madrid** → resetea el estado, todas las cuentas disponibles de nuevo

## Stack
TradingView → Webhook → Lucid Router (Railway.app gratis) → Tradovate API → Lucid

---

## Instalación paso a paso

### Paso 1 — Obtener credenciales API de Tradovate

1. Ve a https://trader.tradovate.com
2. Menú → Settings → API Access
3. Crea una nueva aplicación → anota el **Client ID** y **Client Secret**
4. También necesitas tu email y contraseña de Tradovate

### Paso 2 — Subir el router a Railway (gratis)

1. Crea cuenta en https://railway.app (gratis, no pide tarjeta)
2. New Project → Deploy from GitHub repo
   - O bien: New Project → Deploy from local directory
   - Sube los archivos de esta carpeta
3. En Railway, ve a Variables y añade:
   ```
   TRADOVATE_USER     = tu_email
   TRADOVATE_PASS     = tu_contraseña
   TRADOVATE_CID      = tu_client_id
   TRADOVATE_SECRET   = tu_client_secret
   TRADOVATE_DEVICE   = lucid-router-001
   WEBHOOK_SECRET     = una_clave_larga_aleatoria_ej_xK9mP2qR7nL4wZ1
   TRADOVATE_ENV      = demo   ← cambiar a "live" cuando estés listo
   ```
4. Railway te da una URL pública tipo: `https://tu-app.up.railway.app`

### Paso 3 — Configurar las alertas en TradingView

En tu indicador IFVG Institutional Framework, las alertas ya están en el código.
Solo tienes que:

1. En TradingView → Alerts → Create Alert
2. Condition: IFVG Institutional Framework → cualquier alerta
3. En el campo de mensaje (webhook message), pon:
```json
{
  "secret": "tu_clave_webhook_secreta",
  "action": "{{strategy.order.action}}",
  "ticker": "{{ticker}}",
  "price": "{{close}}",
  "comment": "{{strategy.order.comment}}"
}
```
4. Webhook URL: `https://tu-app.up.railway.app/webhook`
5. Activa "Send webhook notification"

**IMPORTANTE**: Necesitas TradingView Essential ($12.95/mes) para webhooks.

### Paso 4 — Alertas de resultado (SL/TP)

Necesitas dos alertas adicionales en TradingView para que el router sepa
cuándo rotar:

**Alerta TP:**
```json
{"secret": "tu_clave", "action": "tp_hit", "ticker": "{{ticker}}"}
```
Condition: IFVG Institutional Framework → "Take Profit Hit"

**Alerta SL:**
```json
{"secret": "tu_clave", "action": "sl_hit", "ticker": "{{ticker}}"}
```
Condition: IFVG Institutional Framework → "Stop Loss Hit"

---

## Endpoints útiles

| URL | Método | Qué hace |
|-----|--------|----------|
| `/webhook` | POST | Recibe señales de TradingView |
| `/status`  | GET  | Ver estado actual (cuenta activa, wins, pausadas) |
| `/reset`   | POST | Reset manual de emergencia |
| `/health`  | GET  | Verificar que el servidor está vivo |

### Ver estado actual
```bash
curl https://tu-app.up.railway.app/status
```
Respuesta:
```json
{
  "active_account": "LFE05079702600004",
  "wins_today": 2,
  "paused_accounts": ["LFE05079702600002"],
  "all_accounts": ["LFE05079702600002", "LFE05079702600004", "LFE05079702600003"]
}
```

### Reset de emergencia
```bash
curl -X POST https://tu-app.up.railway.app/reset \
  -H "Content-Type: application/json" \
  -d '{"secret": "tu_clave"}'
```

---

## Prueba antes de ir a live

1. Pon `TRADOVATE_ENV=demo` en Railway
2. Envía una señal de prueba manual:
```bash
curl -X POST https://tu-app.up.railway.app/webhook \
  -H "Content-Type: application/json" \
  -d '{"secret":"tu_clave","action":"buy","ticker":"NQ","price":"21000","comment":"Test"}'
```
3. Verifica en Tradovate demo que la orden llegó a `LFE05079702600002`
4. Simula un TP:
```bash
curl -X POST https://tu-app.up.railway.app/webhook \
  -H "Content-Type: application/json" \
  -d '{"secret":"tu_clave","action":"tp_hit","ticker":"NQ"}'
```
5. Verifica que `wins_today` subió a 1
6. Cuando todo funcione en demo → cambia `TRADOVATE_ENV=live`

---

## Coste total
- Railway.app: **GRATIS** (hobby plan, 500 horas/mes)
- TradingView Essential: **$12.95/mes** (necesario para webhooks)
- Tradovate: **GRATIS** (la API no tiene coste adicional)
- **Total: $12.95/mes**

