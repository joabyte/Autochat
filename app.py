# ╔══════════════════════════════════════════════════════════════╗
# ║          FlowBot Pro — Tu propio ManyChat con IA             ║
# ║  Flask + Claude API + Messenger + WhatsApp + Instagram       ║
# ║  Solo copiá este archivo, instalá deps y corré               ║
# ╚══════════════════════════════════════════════════════════════╝
#
# pip install flask anthropic requests gunicorn
#
# Variables de entorno en Render:
#   ANTHROPIC_API_KEY      — obligatoria
#   META_PAGE_TOKEN        — Facebook Messenger + Instagram
#   META_VERIFY_TOKEN      — token que inventás (ej: flowbot2024)
#   META_APP_SECRET        — app secret de Meta
#   TWILIO_ACCOUNT_SID     — para WhatsApp
#   TWILIO_AUTH_TOKEN      — para WhatsApp
#   TWILIO_WHATSAPP_FROM   — ej: whatsapp:+14155238886

from flask import Flask, request, jsonify, render_template_string
import anthropic, os, requests as req
from datetime import datetime

app    = Flask(__name__)
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

META_PAGE_TOKEN   = os.environ.get("META_PAGE_TOKEN", "")
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "flowbot2024")
TWILIO_SID        = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN      = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM       = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

# ── Storage en memoria ──────────────────────────────────────────
contacts  = {}
convs     = {}
flows_db  = {
    "bienvenida": {
        "id": "bienvenida", "name": "Bienvenida", "trigger": "hola",
        "steps": [
            {"type": "message", "content": "👋 ¡Hola! Bienvenido/a. ¿En qué te puedo ayudar?"},
            {"type": "options", "content": "Elegí una opción:", "options": ["📦 Productos", "💬 Hablar con IA", "📞 Contacto"]}
        ], "active": True
    },
    "productos": {
        "id": "productos", "name": "Catálogo", "trigger": "productos",
        "steps": [
            {"type": "message", "content": "🛍️ Nuestro catálogo:"},
            {"type": "message", "content": "✦ Producto A — $1.500\n✦ Producto B — $2.300\n✦ Producto C — $890"}
        ], "active": True
    }
}
broadcasts = []
ch_stats   = {"messenger": 0, "whatsapp": 0, "instagram": 0, "web": 0}
ai_cfg     = {
    "name": "FlowBot IA",
    "system": "Sos un asistente de atención al cliente amable, profesional y en español rioplatense. Respondés de forma concisa y útil.",
    "temp": 0.7
}

# ── Lógica central ──────────────────────────────────────────────
def get_contact(pid, name, channel):
    key = f"{channel}_{pid}"
    if key not in contacts:
        contacts[key] = {"id": key, "name": name, "phone": pid, "tags": [channel],
                         "channel": channel, "last": datetime.now().strftime("%d/%m/%Y %H:%M")}
    else:
        contacts[key]["last"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    return key

def add_msg(cid, msg):
    convs.setdefault(cid, []).append(msg)

def ai_reply(text, cid):
    history = [{"role": "assistant" if m["role"] == "ai" else "user", "content": m["content"]}
               for m in convs.get(cid, [])[-8:] if m["role"] in ("user", "ai")]
    history.append({"role": "user", "content": text})
    try:
        r = claude.messages.create(model="claude-sonnet-4-20250514", max_tokens=600,
                                   system=ai_cfg["system"], temperature=float(ai_cfg["temp"]),
                                   messages=history)
        return r.content[0].text
    except Exception as e:
        return f"Error: {e}"

def process(sender_id, name, text, channel):
    cid = get_contact(sender_id, name, channel)
    ch_stats[channel] = ch_stats.get(channel, 0) + 1
    add_msg(cid, {"role": "user", "content": text})
    flow = next((f for f in flows_db.values() if f["active"] and f["trigger"].lower() in text.lower()), None)
    replies = []
    if flow:
        for step in flow["steps"]:
            if step["type"] == "ai":
                rep = ai_reply(text, cid); add_msg(cid, {"role": "ai", "content": rep}); replies.append(rep)
            else:
                c = step["content"]
                if step.get("options"): c += "\n\n" + "\n".join(f"{i+1}. {o}" for i, o in enumerate(step["options"]))
                add_msg(cid, {"role": "bot", "content": c}); replies.append(c)
    else:
        rep = ai_reply(text, cid); add_msg(cid, {"role": "ai", "content": rep}); replies.append(rep)
    return replies

# ── Messenger ───────────────────────────────────────────────────
def send_messenger(rid, text):
    if not META_PAGE_TOKEN: return
    req.post(f"https://graph.facebook.com/v19.0/me/messages?access_token={META_PAGE_TOKEN}",
             json={"recipient": {"id": rid}, "message": {"text": text}}, timeout=8)

@app.route("/webhook/messenger", methods=["GET"])
def messenger_verify():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == META_VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Forbidden", 403

@app.route("/webhook/messenger", methods=["POST"])
def messenger_hook():
    data = request.json or {}
    if data.get("object") == "page":
        for entry in data.get("entry", []):
            for ev in entry.get("messaging", []):
                sid  = ev.get("sender", {}).get("id", "")
                text = (ev.get("message") or {}).get("text", "")
                if sid and text:
                    [send_messenger(sid, r) for r in process(sid, f"FB_{sid[-4:]}", text, "messenger")]
    return "ok", 200

# ── Instagram ───────────────────────────────────────────────────
@app.route("/webhook/instagram", methods=["GET"])
def instagram_verify():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == META_VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Forbidden", 403

@app.route("/webhook/instagram", methods=["POST"])
def instagram_hook():
    data = request.json or {}
    if data.get("object") == "instagram":
        for entry in data.get("entry", []):
            for ev in entry.get("messaging", []):
                sid  = ev.get("sender", {}).get("id", "")
                text = (ev.get("message") or {}).get("text", "")
                if sid and text:
                    for r in process(sid, f"IG_{sid[-4:]}", text, "instagram"):
                        req.post(f"https://graph.facebook.com/v19.0/me/messages?access_token={META_PAGE_TOKEN}",
                                 json={"recipient": {"id": sid}, "message": {"text": r}}, timeout=8)
    return "ok", 200

# ── WhatsApp / Twilio ───────────────────────────────────────────
@app.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_hook():
    frm  = request.form.get("From", "").replace("whatsapp:", "")
    body = request.form.get("Body", "").strip()
    name = request.form.get("ProfileName", f"WA_{frm[-4:]}")
    if frm and body:
        for r in process(frm, name, body, "whatsapp"):
            req.post(f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
                     data={"From": TWILIO_FROM, "To": f"whatsapp:{frm}", "Body": r},
                     auth=(TWILIO_SID, TWILIO_TOKEN), timeout=8)
    return '<?xml version="1.0"?><Response></Response>', 200, {"Content-Type": "text/xml"}

# ── API interna ─────────────────────────────────────────────────
@app.route("/api/chat", methods=["POST"])
def api_chat():
    d = request.json or {}
    try:
        r = claude.messages.create(model="claude-sonnet-4-20250514", max_tokens=800,
                                   system=d.get("system", ai_cfg["system"]),
                                   temperature=float(d.get("temperature", ai_cfg["temp"])),
                                   messages=d.get("messages", []))
        return jsonify({"reply": r.content[0].text, "ok": True})
    except Exception as e:
        return jsonify({"reply": str(e), "ok": False}), 500

@app.route("/api/stats")
def api_stats():
    return jsonify({"contacts": len(contacts),
                    "active_flows": sum(1 for f in flows_db.values() if f["active"]),
                    "total_msgs": sum(len(v) for v in convs.values()),
                    "broadcasts": len(broadcasts), "channels": ch_stats})

@app.route("/api/ai-config", methods=["POST"])
def api_ai_config():
    d = request.json or {}
    ai_cfg.update({k: d[k] for k in ("name","system","temp") if k in d})
    return jsonify({"ok": True})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": "3.0"})

# ── HTML ─────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FlowBot Pro</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;500;600;700;800&family=JetBrains+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
/* ─── Reset & Base ─── */
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
  --ink:#0a0a0c;--paper:#f5f4f0;--warm:#ede9e0;--line:#d8d4ca;
  --accent:#1a1a2e;--electric:#4f46e5;--electric-light:#818cf8;
  --msg:#25d366;--fb:#0866ff;--ig:#e1306c;--web:#4f46e5;
  --red:#ef4444;--amber:#f59e0b;--emerald:#10b981;
  --r:10px;--r2:16px;--r3:24px;
  --shadow:0 1px 3px rgba(0,0,0,.06),0 4px 16px rgba(0,0,0,.04);
  --shadow-lg:0 8px 32px rgba(0,0,0,.08),0 2px 8px rgba(0,0,0,.04);
}
html,body{height:100%;font-family:'Syne',sans-serif;background:var(--paper);color:var(--ink);overflow:hidden}
a{color:var(--electric);text-decoration:none}
a:hover{text-decoration:underline}

/* ─── Layout ─── */
.shell{display:flex;height:100vh;overflow:hidden}

/* ─── Sidebar ─── */
.sidebar{
  width:230px;flex-shrink:0;
  background:var(--ink);
  display:flex;flex-direction:column;
  position:relative;overflow:hidden;
}
.sidebar::before{
  content:'';position:absolute;top:-80px;left:-80px;
  width:280px;height:280px;
  background:radial-gradient(circle,rgba(79,70,229,.25) 0%,transparent 70%);
  pointer-events:none;
}
.logo{
  padding:28px 22px 22px;
  display:flex;align-items:center;gap:12px;
  border-bottom:1px solid rgba(255,255,255,.06);
}
.logo-mark{
  width:36px;height:36px;flex-shrink:0;
  background:var(--electric);
  border-radius:10px;
  display:flex;align-items:center;justify-content:center;
  font-size:18px;
  box-shadow:0 0 20px rgba(79,70,229,.5);
}
.logo-name{font-size:17px;font-weight:800;color:#fff;letter-spacing:-.4px;line-height:1}
.logo-name small{display:block;font-size:10px;font-weight:400;color:rgba(255,255,255,.35);letter-spacing:.5px;margin-top:2px}

nav{flex:1;padding:14px 10px;overflow-y:auto}
.nav-section{font-size:9px;font-weight:600;color:rgba(255,255,255,.25);letter-spacing:1.5px;text-transform:uppercase;padding:12px 14px 6px}
.nav-item{
  display:flex;align-items:center;gap:10px;
  padding:9px 14px;border-radius:8px;
  cursor:pointer;font-size:13px;font-weight:500;
  color:rgba(255,255,255,.45);
  transition:all .15s;margin-bottom:1px;
  position:relative;
}
.nav-item:hover{color:rgba(255,255,255,.85);background:rgba(255,255,255,.05)}
.nav-item.active{color:#fff;background:rgba(255,255,255,.1)}
.nav-item.active::before{
  content:'';position:absolute;left:0;top:50%;transform:translateY(-50%);
  width:3px;height:18px;background:var(--electric);border-radius:0 3px 3px 0;
}
.nav-item .ico{font-size:16px;width:18px;text-align:center;opacity:.8}
.nav-badge{margin-left:auto;background:var(--electric);color:#fff;font-size:9px;font-weight:700;padding:2px 6px;border-radius:20px;font-family:'JetBrains Mono',monospace}

.sidebar-foot{padding:16px;border-top:1px solid rgba(255,255,255,.06)}
.tier-card{
  background:linear-gradient(135deg,rgba(79,70,229,.3),rgba(129,140,248,.15));
  border:1px solid rgba(79,70,229,.3);
  border-radius:10px;padding:12px;
  font-size:11px;color:rgba(255,255,255,.7);text-align:center;
}
.tier-card strong{display:block;color:#fff;font-size:13px;font-weight:700;margin-bottom:2px}

/* ─── Main ─── */
.main{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--paper)}

.topbar{
  height:58px;flex-shrink:0;
  background:var(--paper);
  border-bottom:1px solid var(--line);
  display:flex;align-items:center;padding:0 28px;gap:16px;
}
.topbar h1{font-size:18px;font-weight:700;flex:1;letter-spacing:-.3px}
.topbar-actions{display:flex;gap:8px}

/* ─── Buttons ─── */
.btn{
  display:inline-flex;align-items:center;gap:6px;
  padding:8px 16px;border-radius:8px;
  font-size:13px;font-weight:600;cursor:pointer;border:none;
  transition:all .15s;font-family:'Syne',sans-serif;line-height:1;
  white-space:nowrap;
}
.btn-primary{background:var(--electric);color:#fff;box-shadow:0 2px 8px rgba(79,70,229,.3)}
.btn-primary:hover{background:#4338ca;transform:translateY(-1px);box-shadow:0 4px 14px rgba(79,70,229,.4)}
.btn-outline{background:transparent;color:var(--ink);border:1.5px solid var(--line)}
.btn-outline:hover{border-color:var(--electric);color:var(--electric)}
.btn-ghost{background:transparent;color:rgba(10,10,12,.45);border:none}
.btn-ghost:hover{background:var(--warm);color:var(--ink)}
.btn-danger{background:#fef2f2;color:var(--red);border:1px solid #fee2e2}
.btn-danger:hover{background:#fee2e2}
.btn-success{background:#f0fdf4;color:var(--emerald);border:1px solid #bbf7d0}
.btn-sm{padding:5px 11px;font-size:12px}
.btn-xs{padding:3px 8px;font-size:11px}

/* ─── Pages ─── */
.page{display:none;flex:1;overflow-y:auto;padding:26px 28px}
.page.active{display:block}
.page-grid{display:grid;gap:20px}

/* ─── Cards ─── */
.card{background:#fff;border:1px solid var(--line);border-radius:var(--r2);box-shadow:var(--shadow)}
.card-header{padding:18px 20px 0;display:flex;align-items:center;justify-content:space-between}
.card-header h3{font-size:14px;font-weight:700;letter-spacing:-.2px}
.card-body{padding:20px}
.card-body.pt0{padding-top:0}

/* ─── Stat cards ─── */
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
.stat{
  background:#fff;border:1px solid var(--line);
  border-radius:var(--r2);padding:18px 20px;
  position:relative;overflow:hidden;
  box-shadow:var(--shadow);
}
.stat::after{
  content:'';position:absolute;top:-20px;right:-20px;
  width:80px;height:80px;border-radius:50%;
  opacity:.06;
}
.stat.s-electric::after{background:var(--electric)}
.stat.s-emerald::after{background:var(--emerald)}
.stat.s-fb::after{background:var(--fb)}
.stat.s-msg::after{background:var(--msg)}
.stat.s-ig::after{background:var(--ig)}
.stat-label{font-size:11px;font-weight:600;color:rgba(10,10,12,.4);letter-spacing:.5px;text-transform:uppercase;margin-bottom:8px}
.stat-num{font-size:30px;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1;letter-spacing:-1px}
.stat-num.c-electric{color:var(--electric)}
.stat-num.c-emerald{color:var(--emerald)}
.stat-num.c-fb{color:var(--fb)}
.stat-num.c-msg{color:var(--msg)}
.stat-num.c-ig{color:var(--ig)}
.stat-num.c-amber{color:var(--amber)}
.stat-sub{font-size:11px;color:rgba(10,10,12,.35);margin-top:4px}

/* ─── Quick actions ─── */
.quick-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.quick-card{
  background:#fff;border:1.5px solid var(--line);border-radius:var(--r2);
  padding:18px 16px;cursor:pointer;transition:all .18s;text-align:center;
  box-shadow:var(--shadow);
}
.quick-card:hover{border-color:var(--electric);transform:translateY(-2px);box-shadow:var(--shadow-lg)}
.quick-card:active{transform:translateY(0)}
.quick-ico{font-size:26px;margin-bottom:8px;line-height:1}
.quick-title{font-size:13px;font-weight:700;margin-bottom:2px}
.quick-desc{font-size:11px;color:rgba(10,10,12,.4)}

/* ─── Channels ─── */
.channels-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.ch-card{
  background:#fff;border:2px solid var(--line);border-radius:var(--r2);
  padding:22px;transition:border-color .2s;
  box-shadow:var(--shadow);
}
.ch-card.is-live{border-color:var(--emerald)}
.ch-card.is-partial{border-color:var(--amber)}
.ch-head{display:flex;align-items:center;gap:14px;margin-bottom:20px}
.ch-ico{
  width:48px;height:48px;border-radius:14px;
  display:flex;align-items:center;justify-content:center;
  font-size:22px;flex-shrink:0;
}
.ch-ico.fb{background:linear-gradient(135deg,#1877f2,#0866ff)}
.ch-ico.wa{background:linear-gradient(135deg,#25d366,#128c7e)}
.ch-ico.ig{background:linear-gradient(135deg,#f09433,#e6683c,#dc2743,#cc2366,#bc1888)}
.ch-info-name{font-size:15px;font-weight:800;letter-spacing:-.3px}
.ch-pill{
  display:inline-flex;align-items:center;gap:4px;
  font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px;margin-top:4px;
}
.ch-pill.live{background:#d1fae5;color:#065f46}
.ch-pill.warn{background:#fef3c7;color:#92400e}
.ch-pill.off{background:var(--warm);color:rgba(10,10,12,.4)}
.ch-pill::before{content:'●';font-size:7px;margin-right:1px}

.url-row{display:flex;gap:6px;align-items:center;margin-top:6px}
.url-box{
  flex:1;background:var(--warm);border:1px solid var(--line);border-radius:7px;
  padding:7px 10px;font-size:11px;color:rgba(10,10,12,.5);
  font-family:'JetBrains Mono',monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.env-table{background:var(--ink);border-radius:var(--r2);padding:18px;margin-top:6px}
.env-row{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.06);font-family:'JetBrains Mono',monospace;font-size:12px}
.env-row:last-child{border-bottom:none}
.env-key{color:var(--electric-light)}
.env-val{color:rgba(255,255,255,.35);font-size:11px}

/* ─── Form controls ─── */
.field{margin-bottom:16px}
.field-label{font-size:11px;font-weight:700;color:rgba(10,10,12,.45);letter-spacing:.6px;text-transform:uppercase;margin-bottom:6px;display:block}
.field-input,.field-textarea,.field-select{
  width:100%;background:#fff;border:1.5px solid var(--line);border-radius:8px;
  padding:9px 12px;color:var(--ink);font-size:14px;
  font-family:'Syne',sans-serif;outline:none;transition:border .15s;
}
.field-input:focus,.field-textarea:focus,.field-select:focus{border-color:var(--electric)}
.field-textarea{resize:vertical;min-height:90px;line-height:1.5}
.field-select option{background:#fff}
.field-hint{font-size:11px;color:rgba(10,10,12,.35);margin-top:5px;line-height:1.5}

/* ─── Flow cards ─── */
.flows-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;margin-bottom:24px}
.flow-card{
  background:#fff;border:1.5px solid var(--line);border-radius:var(--r2);
  padding:18px;cursor:pointer;transition:all .18s;
  box-shadow:var(--shadow);
}
.flow-card:hover{border-color:var(--electric);transform:translateY(-2px);box-shadow:var(--shadow-lg)}
.flow-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}
.flow-name{font-size:14px;font-weight:700;letter-spacing:-.2px}
.flow-trigger{font-size:11px;color:rgba(10,10,12,.4);margin-top:2px;font-family:'JetBrains Mono',monospace}
.badge{padding:3px 8px;border-radius:20px;font-size:10px;font-weight:700;letter-spacing:.3px}
.badge-on{background:#d1fae5;color:#065f46}
.badge-off{background:var(--warm);color:rgba(10,10,12,.4)}
.flow-meta{font-size:12px;color:rgba(10,10,12,.35);margin-bottom:12px}
.flow-actions{display:flex;gap:7px}

.step-builder{background:var(--warm);border:1.5px solid var(--line);border-radius:var(--r2);padding:20px;max-width:660px}
.steps-list{display:flex;flex-direction:column;gap:9px;margin:14px 0}
.step-item{
  background:#fff;border:1.5px solid var(--line);border-radius:10px;
  padding:13px;display:flex;align-items:flex-start;gap:11px;
}
.step-badge{
  background:var(--electric);color:#fff;
  width:24px;height:24px;border-radius:7px;
  display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:700;flex-shrink:0;margin-top:2px;
  font-family:'JetBrains Mono',monospace;
}
.step-type-label{font-size:9px;font-weight:700;color:var(--electric);letter-spacing:.8px;text-transform:uppercase;margin-bottom:5px}
.step-del{background:none;border:none;color:rgba(10,10,12,.25);cursor:pointer;font-size:18px;padding:0 3px;line-height:1;transition:color .1s}
.step-del:hover{color:var(--red)}
.add-steps{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}

/* ─── Chat ─── */
.chat-shell{
  display:grid;grid-template-columns:240px 1fr;
  height:calc(100vh - 58px);
  border-top:1px solid var(--line);
}
.contacts-col{
  background:#fff;border-right:1px solid var(--line);
  display:flex;flex-direction:column;overflow:hidden;
}
.contacts-search{padding:12px;border-bottom:1px solid var(--line)}
.contacts-search input{
  width:100%;background:var(--warm);border:none;border-radius:8px;
  padding:8px 12px;font-size:13px;font-family:'Syne',sans-serif;outline:none;color:var(--ink);
}
.contacts-list{flex:1;overflow-y:auto}
.conv-item{
  padding:11px 14px;border-bottom:1px solid var(--line);
  cursor:pointer;transition:background .1s;
}
.conv-item:hover{background:var(--warm)}
.conv-item.active{background:#eef2ff}
.conv-name{font-size:13px;font-weight:600;display:flex;align-items:center;gap:6px;margin-bottom:3px}
.conv-last{font-size:11px;color:rgba(10,10,12,.35);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ch-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.ch-dot.messenger{background:var(--fb)}
.ch-dot.whatsapp{background:var(--msg)}
.ch-dot.instagram{background:var(--ig)}
.ch-dot.web{background:var(--electric)}

.chat-col{display:flex;flex-direction:column;background:var(--paper)}
.chat-top{
  padding:14px 20px;background:#fff;border-bottom:1px solid var(--line);
  display:flex;align-items:center;gap:12px;flex-shrink:0;
}
.chat-av{
  width:38px;height:38px;border-radius:12px;
  display:flex;align-items:center;justify-content:center;
  font-weight:800;font-size:15px;flex-shrink:0;color:#fff;
}
.chat-av.fb{background:linear-gradient(135deg,#1877f2,#0866ff)}
.chat-av.wa{background:linear-gradient(135deg,#25d366,#128c7e)}
.chat-av.ig{background:linear-gradient(135deg,#f09433,#dc2743)}
.chat-av.web{background:linear-gradient(135deg,var(--electric),var(--electric-light))}
.chat-cname{font-size:14px;font-weight:700;letter-spacing:-.2px}
.chat-online{font-size:11px;color:var(--emerald);margin-top:1px}

.chat-msgs{flex:1;overflow-y:auto;padding:18px 20px;display:flex;flex-direction:column;gap:10px}
.chat-empty{margin:auto;text-align:center;color:rgba(10,10,12,.25);font-size:13px}

.bubble{max-width:70%;padding:10px 14px;border-radius:14px;font-size:14px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.bubble.user{
  background:var(--electric);color:#fff;align-self:flex-end;
  border-bottom-right-radius:4px;
  box-shadow:0 2px 8px rgba(79,70,229,.25);
}
.bubble.bot{
  background:#fff;border:1px solid var(--line);align-self:flex-start;
  border-bottom-left-radius:4px;box-shadow:var(--shadow);
}
.bubble.ai{
  background:linear-gradient(135deg,#eef2ff,#f5f3ff);
  border:1px solid #c7d2fe;align-self:flex-start;
  border-bottom-left-radius:4px;box-shadow:var(--shadow);
}
.ai-label{font-size:9px;font-weight:700;color:var(--electric);letter-spacing:.8px;text-transform:uppercase;margin-bottom:5px}
.typing-wrap{display:flex;gap:4px;padding:10px 14px;background:#fff;border:1px solid var(--line);border-radius:14px;border-bottom-left-radius:4px;width:fit-content;box-shadow:var(--shadow)}
.dot{width:6px;height:6px;background:rgba(10,10,12,.2);border-radius:50%;animation:bob .9s infinite}
.dot:nth-child(2){animation-delay:.2s}.dot:nth-child(3){animation-delay:.4s}
@keyframes bob{0%,80%,100%{transform:translateY(0)}40%{transform:translateY(-6px)}}

.chat-input-bar{
  padding:12px 16px;background:#fff;border-top:1px solid var(--line);
  display:flex;gap:9px;flex-shrink:0;
}
.chat-input-bar input{
  flex:1;background:var(--warm);border:1.5px solid var(--line);border-radius:9px;
  padding:9px 14px;font-size:14px;font-family:'Syne',sans-serif;outline:none;color:var(--ink);
  transition:border .15s;
}
.chat-input-bar input:focus{border-color:var(--electric);background:#fff}

/* ─── Table ─── */
.tbl-wrap{overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
thead tr{border-bottom:2px solid var(--line)}
th{text-align:left;padding:10px 14px;font-size:10px;font-weight:700;color:rgba(10,10,12,.35);text-transform:uppercase;letter-spacing:.7px}
td{padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:hover td{background:var(--warm)}
.tag{display:inline-flex;align-items:center;gap:3px;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:700;margin-right:3px}
.tag.messenger{background:#dbeafe;color:#1d4ed8}
.tag.whatsapp{background:#dcfce7;color:#15803d}
.tag.instagram{background:#fce7f3;color:#9d174d}
.tag.web{background:#ede9fe;color:#5b21b6}
.tag.default{background:var(--warm);color:rgba(10,10,12,.5)}

/* ─── Broadcast ─── */
.bc-form{background:#fff;border:1px solid var(--line);border-radius:var(--r2);padding:24px;max-width:620px;box-shadow:var(--shadow)}
.bc-history-item{background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:14px;margin-bottom:10px;box-shadow:var(--shadow)}
.bc-hist-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.bc-hist-name{font-size:14px;font-weight:700}
.bc-hist-meta{font-size:11px;color:rgba(10,10,12,.35);font-family:'JetBrains Mono',monospace}
.bc-hist-msg{font-size:13px;color:rgba(10,10,12,.5);line-height:1.4}

/* ─── AI Config ─── */
.ai-form{background:#fff;border:1px solid var(--line);border-radius:var(--r2);padding:24px;max-width:620px;box-shadow:var(--shadow)}
.range-track{-webkit-appearance:none;appearance:none;width:100%;height:4px;background:var(--line);border-radius:4px;outline:none;cursor:pointer}
.range-track::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:var(--electric);cursor:pointer;box-shadow:0 0 0 3px rgba(79,70,229,.15)}
.test-box{background:var(--warm);border:1.5px solid var(--line);border-radius:var(--r);padding:16px;margin-top:16px}
.test-response{margin-top:12px;padding:13px;background:#fff;border:1px solid var(--line);border-radius:9px;font-size:14px;line-height:1.55;display:none}

/* ─── Modal ─── */
.overlay{display:none;position:fixed;inset:0;background:rgba(10,10,12,.4);backdrop-filter:blur(4px);z-index:100;align-items:center;justify-content:center}
.overlay.open{display:flex}
.modal{background:#fff;border-radius:var(--r2);padding:28px;width:92%;max-width:460px;box-shadow:0 20px 60px rgba(0,0,0,.15)}
.modal h2{font-size:18px;font-weight:800;margin-bottom:20px;letter-spacing:-.3px}
.modal-actions{display:flex;gap:9px;margin-top:20px;justify-content:flex-end}

/* ─── Toast ─── */
.toast{
  position:fixed;bottom:22px;right:22px;
  background:var(--ink);color:#fff;
  padding:11px 18px;border-radius:10px;
  font-weight:600;font-size:13px;z-index:9999;
  transform:translateY(80px);opacity:0;transition:all .3s;
  display:flex;align-items:center;gap:8px;
  box-shadow:0 8px 24px rgba(0,0,0,.2);
}
.toast.show{transform:translateY(0);opacity:1}

/* ─── Info callout ─── */
.callout{
  background:#eef2ff;border:1px solid #c7d2fe;border-radius:var(--r);
  padding:13px 16px;font-size:13px;color:rgba(10,10,12,.65);line-height:1.6;margin-bottom:18px;
}
.callout strong{color:var(--ink)}
.callout code{background:#c7d2fe;color:var(--electric);padding:1px 6px;border-radius:5px;font-family:'JetBrains Mono',monospace;font-size:12px}

/* ─── Section title ─── */
.section-title{font-size:15px;font-weight:800;letter-spacing:-.3px;margin-bottom:14px;display:flex;align-items:center;gap:8px}
.section-title span{font-size:13px;font-weight:500;color:rgba(10,10,12,.35)}

/* ─── Scrollbar ─── */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}

/* ─── Responsive ─── */
@media(max-width:900px){
  .sidebar{width:200px}
  .stats-row{grid-template-columns:repeat(2,1fr)}
  .quick-grid{grid-template-columns:repeat(2,1fr)}
  .channels-grid{grid-template-columns:1fr}
}
@media(max-width:680px){
  .sidebar{display:none}
  .chat-shell{grid-template-columns:1fr}
  .contacts-col{display:none}
  .stats-row{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body>
<div class="shell">

<!-- ══ SIDEBAR ══ -->
<aside class="sidebar">
  <div class="logo">
    <div class="logo-mark">⚡</div>
    <div class="logo-name">FlowBot<small>PRO v3</small></div>
  </div>
  <nav>
    <div class="nav-section">Principal</div>
    <div class="nav-item active" onclick="go('dashboard',this)"><span class="ico">◼</span> Dashboard</div>
    <div class="nav-item" onclick="go('channels',this)"><span class="ico">◈</span> Canales<span class="nav-badge" id="nb-channels">0/3</span></div>
    <div class="nav-section">Automatización</div>
    <div class="nav-item" onclick="go('flows',this)"><span class="ico">⇄</span> Flujos</div>
    <div class="nav-item" onclick="go('chat',this)"><span class="ico">◎</span> Chat</div>
    <div class="nav-item" onclick="go('broadcast',this)"><span class="ico">▶</span> Broadcast</div>
    <div class="nav-section">Datos</div>
    <div class="nav-item" onclick="go('contacts',this)"><span class="ico">⊞</span> Contactos</div>
    <div class="nav-item" onclick="go('ai',this)"><span class="ico">◉</span> IA Config</div>
  </nav>
  <div class="sidebar-foot">
    <div class="tier-card"><strong>✦ FlowBot Pro</strong>Claude · Messenger · WA · IG</div>
  </div>
</aside>

<!-- ══ MAIN ══ -->
<div class="main">
  <div class="topbar">
    <h1 id="page-title">Dashboard</h1>
    <div class="topbar-actions">
      <button class="btn btn-outline btn-sm" onclick="go('flows',document.querySelectorAll('.nav-item')[4])">+ Flujo</button>
      <button class="btn btn-primary btn-sm" onclick="openM('m-contact')">+ Contacto</button>
    </div>
  </div>

  <!-- ── DASHBOARD ── -->
  <div id="page-dashboard" class="page active">
    <div class="stats-row">
      <div class="stat s-electric">
        <div class="stat-label">Contactos</div>
        <div class="stat-num c-electric" id="s-c">0</div>
        <div class="stat-sub">registrados</div>
      </div>
      <div class="stat s-emerald">
        <div class="stat-label">Flujos activos</div>
        <div class="stat-num c-emerald" id="s-f">2</div>
        <div class="stat-sub">automáticos</div>
      </div>
      <div class="stat">
        <div class="stat-label">Mensajes</div>
        <div class="stat-num" id="s-m">0</div>
        <div class="stat-sub">todos los canales</div>
      </div>
      <div class="stat">
        <div class="stat-label">Broadcasts</div>
        <div class="stat-num c-amber" id="s-b">0</div>
        <div class="stat-sub">campañas</div>
      </div>
    </div>

    <div class="stats-row" style="grid-template-columns:repeat(4,1fr);margin-bottom:20px">
      <div class="stat s-fb" style="border-left:3px solid var(--fb)">
        <div class="stat-label">Messenger</div>
        <div class="stat-num c-fb" id="s-ms" style="font-size:22px">0</div>
        <div class="stat-sub">mensajes</div>
      </div>
      <div class="stat s-msg" style="border-left:3px solid var(--msg)">
        <div class="stat-label">WhatsApp</div>
        <div class="stat-num c-msg" id="s-wa" style="font-size:22px">0</div>
        <div class="stat-sub">mensajes</div>
      </div>
      <div class="stat s-ig" style="border-left:3px solid var(--ig)">
        <div class="stat-label">Instagram</div>
        <div class="stat-num c-ig" id="s-ig" style="font-size:22px">0</div>
        <div class="stat-sub">mensajes</div>
      </div>
      <div class="stat s-electric" style="border-left:3px solid var(--electric)">
        <div class="stat-label">Web / API</div>
        <div class="stat-num c-electric" id="s-wb" style="font-size:22px">0</div>
        <div class="stat-sub">mensajes</div>
      </div>
    </div>

    <div class="quick-grid">
      <div class="quick-card" onclick="go('channels',document.querySelectorAll('.nav-item')[1])">
        <div class="quick-ico">◈</div>
        <div class="quick-title">Conectar canales</div>
        <div class="quick-desc">FB · WhatsApp · Instagram</div>
      </div>
      <div class="quick-card" onclick="go('flows',document.querySelectorAll('.nav-item')[4])">
        <div class="quick-ico">⇄</div>
        <div class="quick-title">Crear flujo</div>
        <div class="quick-desc">Automatizá respuestas</div>
      </div>
      <div class="quick-card" onclick="go('broadcast',document.querySelectorAll('.nav-item')[5])">
        <div class="quick-ico">▶</div>
        <div class="quick-title">Broadcast</div>
        <div class="quick-desc">Mensajes masivos</div>
      </div>
      <div class="quick-card" onclick="go('ai',document.querySelectorAll('.nav-item')[7])">
        <div class="quick-ico">◉</div>
        <div class="quick-title">Config IA</div>
        <div class="quick-desc">Personalidad del bot</div>
      </div>
    </div>
  </div>

  <!-- ── CANALES ── -->
  <div id="page-channels" class="page">
    <div class="callout">
      <strong>¿Cómo conectar?</strong> Completá las credenciales en Render como variables de entorno y pegá las URLs de webhook en cada plataforma.
      Tu URL base: <code id="base-url">cargando…</code>
    </div>
    <div class="channels-grid">

      <!-- Messenger -->
      <div class="ch-card" id="cc-ms">
        <div class="ch-head">
          <div class="ch-ico fb">💬</div>
          <div>
            <div class="ch-info-name">Facebook Messenger</div>
            <div class="ch-pill off" id="cp-ms">No configurado</div>
          </div>
        </div>
        <div class="field">
          <label class="field-label">Page Access Token</label>
          <input class="field-input" id="f-pt" placeholder="EAABsb…" oninput="chkCh()">
        </div>
        <div class="field">
          <label class="field-label">Verify Token</label>
          <input class="field-input" id="f-vt" value="flowbot2024" oninput="chkCh()">
        </div>
        <div class="field">
          <label class="field-label">Webhook URL</label>
          <div class="url-row">
            <div class="url-box" id="u-ms">/webhook/messenger</div>
            <button class="btn btn-outline btn-xs" onclick="cpUrl('u-ms')">Copiar</button>
          </div>
        </div>
        <div class="callout" style="font-size:12px;margin:0">
          <strong>Pasos:</strong> developers.facebook.com → App → Messenger → pegar Webhook URL + Verify Token → suscribir <code>messages</code> → copiar Page Token a Render
        </div>
      </div>

      <!-- WhatsApp -->
      <div class="ch-card" id="cc-wa">
        <div class="ch-head">
          <div class="ch-ico wa">📱</div>
          <div>
            <div class="ch-info-name">WhatsApp via Twilio</div>
            <div class="ch-pill off" id="cp-wa">No configurado</div>
          </div>
        </div>
        <div class="field">
          <label class="field-label">Account SID</label>
          <input class="field-input" id="f-sid" placeholder="ACxxxxxxxxx" oninput="chkCh()">
        </div>
        <div class="field">
          <label class="field-label">Auth Token</label>
          <input class="field-input" id="f-tok" placeholder="••••••••" type="password" oninput="chkCh()">
        </div>
        <div class="field">
          <label class="field-label">Número Twilio</label>
          <input class="field-input" id="f-frm" value="whatsapp:+14155238886" oninput="chkCh()">
        </div>
        <div class="field">
          <label class="field-label">Webhook URL</label>
          <div class="url-row">
            <div class="url-box" id="u-wa">/webhook/whatsapp</div>
            <button class="btn btn-outline btn-xs" onclick="cpUrl('u-wa')">Copiar</button>
          </div>
        </div>
        <div class="callout" style="font-size:12px;margin:0">
          <strong>Pasos:</strong> twilio.com → Sandbox WhatsApp → pegar Webhook URL → copiar SID + Token a Render
        </div>
      </div>

      <!-- Instagram -->
      <div class="ch-card" id="cc-ig">
        <div class="ch-head">
          <div class="ch-ico ig">📸</div>
          <div>
            <div class="ch-info-name">Instagram DMs</div>
            <div class="ch-pill off" id="cp-ig">No configurado</div>
          </div>
        </div>
        <div class="field">
          <label class="field-label">Mismo token que Messenger</label>
          <input class="field-input" placeholder="Usa el META_PAGE_TOKEN de arriba" disabled style="opacity:.45">
        </div>
        <div class="field">
          <label class="field-label">Webhook URL</label>
          <div class="url-row">
            <div class="url-box" id="u-ig">/webhook/instagram</div>
            <button class="btn btn-outline btn-xs" onclick="cpUrl('u-ig')">Copiar</button>
          </div>
        </div>
        <div class="callout" style="font-size:12px;margin:0">
          <strong>Pasos:</strong> App Meta → agregar Instagram → conectar cuenta Business → pegar Webhook URL + mismo Verify Token → suscribir <code>messages</code>
        </div>
      </div>

    </div>

    <div class="section-title" style="margin-top:24px">Variables de entorno en Render</div>
    <div class="env-table" style="max-width:660px">
      <div class="env-row"><span class="env-key">ANTHROPIC_API_KEY</span><span class="env-val">sk-ant-… ← obligatoria</span></div>
      <div class="env-row"><span class="env-key">META_PAGE_TOKEN</span><span class="env-val">EAAB… (Messenger + IG)</span></div>
      <div class="env-row"><span class="env-key">META_VERIFY_TOKEN</span><span class="env-val">flowbot2024</span></div>
      <div class="env-row"><span class="env-key">META_APP_SECRET</span><span class="env-val">app secret de Meta</span></div>
      <div class="env-row"><span class="env-key">TWILIO_ACCOUNT_SID</span><span class="env-val">ACxxx…</span></div>
      <div class="env-row"><span class="env-key">TWILIO_AUTH_TOKEN</span><span class="env-val">tu auth token</span></div>
      <div class="env-row"><span class="env-key">TWILIO_WHATSAPP_FROM</span><span class="env-val">whatsapp:+14155238886</span></div>
    </div>
  </div>

  <!-- ── FLUJOS ── -->
  <div id="page-flows" class="page">
    <div id="flows-grid" class="flows-grid"></div>
    <div class="section-title">Crear nuevo flujo <span>— configurá pasos automáticos</span></div>
    <div class="step-builder">
      <div class="field"><label class="field-label">Nombre del flujo</label><input id="nf-n" class="field-input" placeholder="Ej: Bienvenida VIP"></div>
      <div class="field"><label class="field-label">Trigger (palabra clave)</label><input id="nf-t" class="field-input" placeholder="Ej: hola · precio · info"><div class="field-hint">Cuando el usuario escriba esta palabra, se activa el flujo</div></div>
      <div class="field">
        <label class="field-label">Pasos del flujo</label>
        <div id="new-steps" class="steps-list"></div>
        <div class="add-steps">
          <button class="btn btn-outline btn-sm" onclick="addStep('message')">+ Mensaje</button>
          <button class="btn btn-outline btn-sm" onclick="addStep('options')">+ Opciones</button>
          <button class="btn btn-primary btn-sm" onclick="addStep('ai')">◉ Respuesta IA</button>
        </div>
      </div>
      <button class="btn btn-primary" onclick="saveFlow()">Guardar flujo</button>
    </div>
  </div>

  <!-- ── CHAT ── -->
  <div id="page-chat" class="page" style="padding:0">
    <div class="chat-shell">
      <div class="contacts-col">
        <div class="contacts-search"><input id="srch" placeholder="Buscar conversación…" oninput="filterConvs(this.value)"></div>
        <div id="conv-list" class="contacts-list"></div>
      </div>
      <div class="chat-col">
        <div class="chat-top">
          <div class="chat-av web" id="chat-av">?</div>
          <div>
            <div class="chat-cname" id="chat-cn">Seleccioná un contacto</div>
            <div class="chat-online">● en línea</div>
          </div>
        </div>
        <div id="chat-msgs" class="chat-msgs"><div class="chat-empty">Seleccioná o creá un contacto para empezar</div></div>
        <div class="chat-input-bar">
          <input id="chat-in" placeholder="Escribí como si fueras el usuario…" onkeypress="if(event.key==='Enter')sendMsg()">
          <button class="btn btn-primary" onclick="sendMsg()">Enviar</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ── CONTACTOS ── -->
  <div id="page-contacts" class="page">
    <div class="card">
      <div class="card-body" style="padding:0">
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Nombre</th><th>Canal</th><th>ID / Tel</th><th>Tags</th><th>Msgs</th><th>Último contacto</th><th></th></tr></thead>
            <tbody id="ctbl"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- ── BROADCAST ── -->
  <div id="page-broadcast" class="page">
    <div class="bc-form">
      <div class="field"><label class="field-label">Nombre de campaña</label><input id="bc-n" class="field-input" placeholder="Promo de lanzamiento…"></div>
      <div class="field"><label class="field-label">Mensaje</label><textarea id="bc-m" class="field-textarea" placeholder="El mensaje que recibirán tus contactos…"></textarea></div>
      <div class="field">
        <label class="field-label">Canal destino</label>
        <select id="bc-c" class="field-select">
          <option value="all">Todos los canales</option>
          <option value="web">Solo web / simulador</option>
          <option value="messenger">Solo Messenger</option>
          <option value="whatsapp">Solo WhatsApp</option>
          <option value="instagram">Solo Instagram</option>
        </select>
      </div>
      <div style="display:flex;gap:9px;flex-wrap:wrap">
        <button class="btn btn-primary" onclick="doBroadcast()">▶ Enviar broadcast</button>
        <button class="btn btn-outline" onclick="aiGenBC()">◉ Generar con IA</button>
      </div>
    </div>
    <div class="section-title" style="margin-top:24px">Historial <span id="bc-count"></span></div>
    <div id="bc-hist"></div>
  </div>

  <!-- ── IA CONFIG ── -->
  <div id="page-ai" class="page">
    <div class="ai-form">
      <div class="section-title">Configuración del asistente IA</div>
      <div class="field"><label class="field-label">Nombre del bot</label><input id="ai-n" class="field-input" value="FlowBot IA"></div>
      <div class="field">
        <label class="field-label">System prompt — personalidad del bot</label>
        <textarea id="ai-s" class="field-textarea" style="min-height:120px">Sos un asistente de atención al cliente amable, profesional y en español rioplatense. Respondés de forma concisa y útil. Podés ayudar con consultas, información de productos y soporte general.</textarea>
        <div class="field-hint">Este texto define cómo responde el bot. Personalizalo con el nombre de tu negocio, tono y reglas específicas.</div>
      </div>
      <div class="field">
        <label class="field-label">Temperatura — <span id="tv" style="color:var(--electric);font-family:'JetBrains Mono',monospace">0.7</span></label>
        <input id="ai-t" type="range" min="0" max="1" step="0.1" value="0.7" class="range-track" oninput="document.getElementById('tv').textContent=this.value">
        <div class="field-hint">0 = respuestas exactas y consistentes · 1 = más creativo y variado</div>
      </div>
      <button class="btn btn-primary" onclick="saveAI()">Guardar configuración</button>
      <div class="test-box">
        <div class="section-title" style="font-size:13px;margin:0 0 10px">Probar IA</div>
        <div style="display:flex;gap:9px">
          <input id="ai-ti" class="field-input" placeholder="Escribí algo para probar el bot…">
          <button class="btn btn-primary" onclick="testAI()">Probar</button>
        </div>
        <div id="ai-tr" class="test-response"></div>
      </div>
    </div>
  </div>

</div><!-- /main -->
</div><!-- /shell -->

<!-- MODAL: nuevo contacto -->
<div id="m-contact" class="overlay">
  <div class="modal">
    <h2>Nuevo contacto</h2>
    <div class="field"><label class="field-label">Nombre</label><input id="m-cn" class="field-input" placeholder="María García"></div>
    <div class="field"><label class="field-label">Teléfono o ID</label><input id="m-cp" class="field-input" placeholder="+54 11 1234-5678"></div>
    <div class="field"><label class="field-label">Tags (coma separados)</label><input id="m-ct" class="field-input" placeholder="cliente, vip, newsletter"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeM('m-contact')">Cancelar</button>
      <button class="btn btn-primary" onclick="saveContact()">Guardar</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
/* ── State ── */
let C  = JSON.parse(localStorage.getItem('fb_c')||'{}');
let V  = JSON.parse(localStorage.getItem('fb_v')||'{}');
let FL = JSON.parse(localStorage.getItem('fb_f')||'null') || {
  bienvenida:{id:'bienvenida',name:'Bienvenida',trigger:'hola',steps:[{type:'message',content:'👋 ¡Hola! Bienvenido/a. ¿En qué te puedo ayudar?'},{type:'options',content:'Elegí una opción:',options:['📦 Productos','💬 Hablar con IA','📞 Contacto']}],active:true},
  productos:{id:'productos',name:'Catálogo',trigger:'productos',steps:[{type:'message',content:'🛍️ Nuestro catálogo:'},{type:'message',content:'✦ Producto A — $1.500\n✦ Producto B — $2.300\n✦ Producto C — $890'}],active:true}
};
let BC = JSON.parse(localStorage.getItem('fb_b')||'[]');
let AI = JSON.parse(localStorage.getItem('fb_ai')||'{"name":"FlowBot IA","system":"Sos un asistente amable en español rioplatense.","temp":0.7}');
let CS = JSON.parse(localStorage.getItem('fb_cs')||'{"messenger":0,"whatsapp":0,"instagram":0,"web":0}');
let NS = [];       // new steps buffer
let CUR = null;    // current contact id
let TOTAL = 0;

/* ── Nav ── */
const TITLES = {dashboard:'Dashboard',channels:'Canales',flows:'Flujos',chat:'Chat',contacts:'Contactos',broadcast:'Broadcast',ai:'IA Config'};
function go(id,el){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById('page-'+id).classList.add('active');
  if(el) el.classList.add('active');
  document.getElementById('page-title').textContent = TITLES[id]||id;
  if(id==='dashboard')  updateStats();
  if(id==='channels')   initCh();
  if(id==='flows')      renderFlows();
  if(id==='chat')       renderConvList();
  if(id==='contacts')   renderTable();
  if(id==='broadcast')  renderBCHist();
  if(id==='ai'){ document.getElementById('ai-n').value=AI.name; document.getElementById('ai-s').value=AI.system; document.getElementById('ai-t').value=AI.temp; document.getElementById('tv').textContent=AI.temp; }
}

/* ── Stats ── */
function updateStats(){
  document.getElementById('s-c').textContent = Object.keys(C).length;
  document.getElementById('s-f').textContent = Object.values(FL).filter(f=>f.active).length;
  document.getElementById('s-m').textContent = Object.values(V).reduce((a,v)=>a+v.length,0);
  document.getElementById('s-b').textContent = BC.length;
  document.getElementById('s-ms').textContent = CS.messenger||0;
  document.getElementById('s-wa').textContent = CS.whatsapp||0;
  document.getElementById('s-ig').textContent = CS.instagram||0;
  document.getElementById('s-wb').textContent = CS.web||0;
  // channel badge
  const live = [document.getElementById('f-pt')?.value, document.getElementById('f-sid')?.value].filter(Boolean).length;
  document.getElementById('nb-channels').textContent = live+'/3';
}

/* ── Channels ── */
function initCh(){
  const base = window.location.origin;
  document.getElementById('base-url').textContent = base;
  document.getElementById('u-ms').textContent = base+'/webhook/messenger';
  document.getElementById('u-wa').textContent = base+'/webhook/whatsapp';
  document.getElementById('u-ig').textContent = base+'/webhook/instagram';
  chkCh();
  fetch('/api/stats').then(r=>r.json()).then(d=>{ if(d.channels){ Object.assign(CS,d.channels); localStorage.setItem('fb_cs',JSON.stringify(CS)); updateStats(); } }).catch(()=>{});
}
function chkCh(){
  const pt  = document.getElementById('f-pt')?.value||'';
  const sid = document.getElementById('f-sid')?.value||'';
  setCS('ms', pt  ? 'live':'off', pt  ? '● Configurado':'● No configurado');
  setCS('ig', pt  ? 'live':'off', pt  ? '● Configurado (usa token de Messenger)':'● No configurado');
  setCS('wa', sid ? 'live':'off', sid ? '● Configurado':'● No configurado');
  const live = [pt,sid].filter(Boolean).length;
  document.getElementById('nb-channels').textContent = (live>0?live:0)+'/3';
}
function setCS(k,cls,txt){
  const el=document.getElementById('cp-'+k), card=document.getElementById('cc-'+k);
  if(!el)return;
  el.className='ch-pill '+cls; el.textContent=txt;
  if(card) card.style.borderColor=cls==='live'?'var(--emerald)':cls==='warn'?'var(--amber)':'var(--line)';
}
function cpUrl(id){
  const t=document.getElementById(id)?.textContent||'';
  navigator.clipboard?.writeText(t).then(()=>toast('✓ URL copiada')).catch(()=>fbCopy(t));
}
function fbCopy(t){ const ta=document.createElement('textarea');ta.value=t;document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta);toast('✓ URL copiada'); }

/* ── Flows ── */
function renderFlows(){
  const g=document.getElementById('flows-grid'); g.innerHTML='';
  Object.values(FL).forEach(f=>{
    const d=document.createElement('div'); d.className='flow-card';
    d.innerHTML=`<div class="flow-top"><div><div class="flow-name">${f.name}</div><div class="flow-trigger">trigger: "${f.trigger}"</div></div><span class="badge ${f.active?'badge-on':'badge-off'}">${f.active?'ACTIVO':'PAUSA'}</span></div><div class="flow-meta">${f.steps.length} paso(s)</div><div class="flow-actions"><button class="btn btn-outline btn-sm" onclick="togFlow('${f.id}')">⏸ Pausar/Activar</button><button class="btn btn-danger btn-sm" onclick="delFlow('${f.id}')">✕ Borrar</button></div>`;
    g.appendChild(d);
  });
}
function togFlow(id){ FL[id].active=!FL[id].active; saveFL(); renderFlows(); }
function delFlow(id){ if(confirm('¿Eliminar este flujo?')){ delete FL[id]; saveFL(); renderFlows(); } }
function saveFL(){ localStorage.setItem('fb_f',JSON.stringify(FL)); }

function addStep(type){
  NS.push({type,content:'',options:type==='options'?['Opción 1','Opción 2']:[]});
  renderNS();
}
function renderNS(){
  const c=document.getElementById('new-steps'); c.innerHTML='';
  NS.forEach((s,i)=>{
    const d=document.createElement('div'); d.className='step-item';
    let inner;
    if(s.type==='message') inner=`<input class="field-input" style="margin-top:4px" value="${s.content.replace(/"/g,'&quot;')}" oninput="NS[${i}].content=this.value" placeholder="Texto del mensaje…">`;
    else if(s.type==='ai') inner=`<div style="margin-top:4px;font-size:13px;color:var(--electric);font-weight:500">◉ Claude responderá con IA aquí</div>`;
    else inner=`<input class="field-input" style="margin-top:4px" value="${s.content.replace(/"/g,'&quot;')}" oninput="NS[${i}].content=this.value" placeholder="Texto de la opción…">`;
    d.innerHTML=`<div class="step-badge">${i+1}</div><div style="flex:1"><div class="step-type-label">${s.type==='ai'?'IA CLAUDE':s.type}</div>${inner}</div><button class="step-del" onclick="NS.splice(${i},1);renderNS()">×</button>`;
    c.appendChild(d);
  });
}
function saveFlow(){
  const n=document.getElementById('nf-n').value.trim();
  const t=document.getElementById('nf-t').value.trim().toLowerCase();
  if(!n||!t||!NS.length){ toast('⚠ Completá todos los campos'); return; }
  const id='flow_'+Date.now();
  FL[id]={id,name:n,trigger:t,steps:[...NS],active:true};
  saveFL(); NS=[]; renderNS();
  document.getElementById('nf-n').value=''; document.getElementById('nf-t').value='';
  renderFlows(); toast('✓ Flujo guardado');
}

/* ── Chat ── */
function renderConvList(filter=''){
  const list=document.getElementById('conv-list'); list.innerHTML='';
  const all=Object.values(C).filter(c=>!filter||c.name.toLowerCase().includes(filter.toLowerCase()));
  if(!all.length){ list.innerHTML='<div style="padding:16px;font-size:13px;color:rgba(10,10,12,.35)">Sin contactos aún</div>'; return; }
  all.forEach(c=>{
    const cv=V[c.id]||[];
    const last=cv.length?cv[cv.length-1].content.substring(0,42):'Sin mensajes';
    const d=document.createElement('div'); d.className='conv-item'+(CUR===c.id?' active':'');
    d.innerHTML=`<div class="conv-name"><span class="ch-dot ${c.channel||'web'}"></span>${c.name}</div><div class="conv-last">${last}</div>`;
    d.onclick=()=>openChat(c.id); list.appendChild(d);
  });
}
function filterConvs(v){ renderConvList(v); }
function openChat(id){
  CUR=id; const c=C[id];
  const ch=c.channel||'web';
  const av=document.getElementById('chat-av');
  av.textContent=c.name[0].toUpperCase(); av.className='chat-av '+ch;
  document.getElementById('chat-cn').textContent=`${c.name} · ${ch.toUpperCase()}`;
  renderMsgs(); renderConvList(document.getElementById('srch').value);
}
function renderMsgs(){
  const box=document.getElementById('chat-msgs'); box.innerHTML='';
  const cv=V[CUR]||[];
  if(!cv.length){ box.innerHTML='<div class="chat-empty">Mandá un mensaje para iniciar</div>'; return; }
  cv.forEach(m=>{
    const d=document.createElement('div'); d.className='bubble '+m.role;
    if(m.role==='ai') d.innerHTML=`<div class="ai-label">◉ FlowBot IA</div>${escHtml(m.content)}`;
    else d.textContent=m.content;
    box.appendChild(d);
  });
  box.scrollTop=box.scrollHeight;
}
function escHtml(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function sendMsg(){
  if(!CUR){ toast('⚠ Seleccioná un contacto'); return; }
  const inp=document.getElementById('chat-in'); const txt=inp.value.trim(); if(!txt) return; inp.value='';
  addM(CUR,{role:'user',content:txt}); CS.web=(CS.web||0)+1; localStorage.setItem('fb_cs',JSON.stringify(CS));
  renderMsgs();
  const flow=Object.values(FL).find(f=>f.active&&txt.toLowerCase().includes(f.trigger.toLowerCase()));
  if(flow){
    for(const s of flow.steps){
      if(s.type==='ai'){ await runAI(txt); }
      else{ await sleep(320); let c=s.content; if(s.options) c+='\n\n'+s.options.map((o,i)=>`${i+1}. ${o}`).join('\n'); addM(CUR,{role:'bot',content:c}); renderMsgs(); }
    }
  } else { await runAI(txt); }
}
async function runAI(txt){
  showTyping();
  try{
    const cv=V[CUR]||[];
    const msgs=cv.filter(m=>m.role==='user'||m.role==='ai').slice(-8).map(m=>({role:m.role==='ai'?'assistant':'user',content:m.content}));
    msgs.push({role:'user',content:txt});
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:msgs,system:AI.system,temperature:parseFloat(AI.temp)})});
    const d=await r.json(); hideTyping();
    addM(CUR,{role:'ai',content:d.reply||'Sin respuesta'}); renderMsgs();
  }catch(e){ hideTyping(); addM(CUR,{role:'bot',content:'❌ Error de conexión'}); renderMsgs(); }
}
function addM(id,msg){ if(!V[id]) V[id]=[]; V[id].push(msg); localStorage.setItem('fb_v',JSON.stringify(V)); }
function showTyping(){ const b=document.getElementById('chat-msgs'); const d=document.createElement('div'); d.id='typ'; d.className='typing-wrap'; d.innerHTML='<div class="dot"></div><div class="dot"></div><div class="dot"></div>'; b.appendChild(d); b.scrollTop=b.scrollHeight; }
function hideTyping(){ document.getElementById('typ')?.remove(); }
function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }

/* ── Contacts ── */
function renderTable(){
  const tb=document.getElementById('ctbl'); tb.innerHTML='';
  const all=Object.values(C);
  if(!all.length){ tb.innerHTML='<tr><td colspan="7" style="text-align:center;padding:32px;color:rgba(10,10,12,.3)">Sin contactos registrados</td></tr>'; return; }
  all.forEach(c=>{
    const cv=V[c.id]||[]; const tr=document.createElement('tr'); const ch=c.channel||'web';
    tr.innerHTML=`<td><strong>${c.name}</strong></td><td><span class="tag ${ch}">${ch}</span></td><td style="font-family:'JetBrains Mono',monospace;font-size:11px;color:rgba(10,10,12,.4)">${c.phone||'—'}</td><td>${(c.tags||[]).filter(t=>t!==ch).map(t=>`<span class="tag default">${t}</span>`).join('')}</td><td style="font-family:'JetBrains Mono',monospace;font-size:12px">${cv.length}</td><td style="font-size:12px;color:rgba(10,10,12,.4)">${c.last||'—'}</td><td><button class="btn btn-danger btn-xs" onclick="delContact('${c.id}')">✕</button></td>`;
    tb.appendChild(tr);
  });
}
function saveContact(){
  const n=document.getElementById('m-cn').value.trim(); if(!n){ toast('⚠ Nombre requerido'); return; }
  const id='c_'+Date.now();
  C[id]={id,name:n,phone:document.getElementById('m-cp').value.trim(),tags:document.getElementById('m-ct').value.split(',').map(t=>t.trim()).filter(Boolean),channel:'web',last:new Date().toLocaleDateString('es-AR')};
  localStorage.setItem('fb_c',JSON.stringify(C));
  closeM('m-contact'); ['m-cn','m-cp','m-ct'].forEach(i=>document.getElementById(i).value='');
  toast('✓ Contacto guardado'); updateStats();
}
function delContact(id){ if(confirm('¿Eliminar contacto?')){ delete C[id]; localStorage.setItem('fb_c',JSON.stringify(C)); renderTable(); updateStats(); } }

/* ── Broadcast ── */
async function doBroadcast(){
  const n=document.getElementById('bc-n').value.trim(), m=document.getElementById('bc-m').value.trim();
  if(!n||!m){ toast('⚠ Completá los campos'); return; }
  const ch=document.getElementById('bc-c').value;
  const dest=Object.values(C).filter(c=>ch==='all'||c.channel===ch);
  if(!dest.length){ toast('⚠ No hay contactos en ese canal'); return; }
  dest.forEach(c=>addM(c.id,{role:'bot',content:`📢 ${m}`}));
  BC.unshift({name:n,msg:m,date:new Date().toLocaleDateString('es-AR'),count:dest.length,ch});
  localStorage.setItem('fb_b',JSON.stringify(BC));
  renderBCHist(); toast(`✓ Enviado a ${dest.length} contacto(s)`);
  document.getElementById('bc-n').value=''; document.getElementById('bc-m').value='';
}
async function aiGenBC(){
  const hint=document.getElementById('bc-m').value||'campaña de marketing';
  toast('◉ Generando con IA…');
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:[{role:'user',content:'Generá UN mensaje de broadcast para chatbot sobre: '+hint+'. Solo el texto del mensaje, sin comillas ni explicaciones.'}],system:'Sos experto en marketing digital. Respondés solo con el texto del mensaje.',temperature:0.9})});
    const d=await r.json(); document.getElementById('bc-m').value=d.reply||'';
  }catch(e){ toast('❌ Error con IA'); }
}
function renderBCHist(){
  const h=document.getElementById('bc-hist'); h.innerHTML='';
  document.getElementById('bc-count').textContent=BC.length?`— ${BC.length} campaña(s)`:'';
  if(!BC.length){ h.innerHTML='<div style="font-size:13px;color:rgba(10,10,12,.3)">Sin broadcasts aún</div>'; return; }
  BC.forEach(b=>{
    const d=document.createElement('div'); d.className='bc-history-item';
    d.innerHTML=`<div class="bc-hist-head"><span class="bc-hist-name">${b.name}</span><span class="bc-hist-meta">${b.date} · ${b.count} c. · <span class="tag ${b.ch||'web'}">${b.ch||'all'}</span></span></div><div class="bc-hist-msg">${b.msg.substring(0,120)}${b.msg.length>120?'…':''}</div>`;
    h.appendChild(d);
  });
}

/* ── AI Config ── */
function saveAI(){
  AI={name:document.getElementById('ai-n').value,system:document.getElementById('ai-s').value,temp:document.getElementById('ai-t').value};
  localStorage.setItem('fb_ai',JSON.stringify(AI));
  fetch('/api/ai-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(AI)}).catch(()=>{});
  toast('✓ Configuración guardada');
}
async function testAI(){
  const t=document.getElementById('ai-ti').value.trim(); if(!t){ toast('⚠ Escribí algo'); return; }
  const el=document.getElementById('ai-tr'); el.style.display='block'; el.textContent='◉ Pensando…';
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:[{role:'user',content:t}],system:document.getElementById('ai-s').value,temperature:parseFloat(document.getElementById('ai-t').value)})});
    const d=await r.json(); el.innerHTML='<span style="color:var(--electric);font-weight:700;font-size:11px;letter-spacing:.5px;text-transform:uppercase">◉ Respuesta IA</span><br><br>'+escHtml(d.reply||'Sin respuesta');
  }catch(e){ el.textContent='❌ Error de conexión'; }
}

/* ── Modal ── */
function openM(id){ document.getElementById(id).classList.add('open'); }
function closeM(id){ document.getElementById(id).classList.remove('open'); }
document.querySelectorAll('.overlay').forEach(o=>o.addEventListener('click',e=>{ if(e.target===o) o.classList.remove('open'); }));

/* ── Toast ── */
function toast(msg){ const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),2800); }

/* ── Init ── */
updateStats(); renderFlows();
fetch('/api/stats').then(r=>r.json()).then(d=>{ if(d.channels){ Object.assign(CS,d.channels); localStorage.setItem('fb_cs',JSON.stringify(CS)); updateStats(); } }).catch(()=>{});
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
