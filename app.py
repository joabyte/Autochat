from flask import Flask, request, jsonify, render_template_string, redirect, session, url_for
import anthropic, os, requests as req, json, hashlib, hmac, secrets
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ── Config ─────────────────────────────────────────────────────
META_APP_ID     = os.environ.get("META_APP_ID", "1545752993549364")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "6a539a0b3149a0fcf3002cef40658bf0")
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "autochat2024")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
BASE_URL        = os.environ.get("BASE_URL", "https://autochat-ll2x.onrender.com")

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── In-memory DB (reemplazar con Supabase en producción) ────────
USERS     = {}   # email -> {password_hash, name, plan, created_at}
ACCOUNTS  = {}   # user_email -> {fb_token, ig_token, page_id, ig_id, page_name}
FLOWS     = {}   # user_email -> [flow, ...]
CONVS     = {}   # f"{user_email}_{sender_id}" -> [msg, ...]
STATS     = {}   # user_email -> {msgs, flows, contacts}

# ── Admin ───────────────────────────────────────────────────────
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "joaquin.glesias2@gmail.com")
ADMIN_PASS  = os.environ.get("ADMIN_PASS",  "admin1234")

def hash_pass(p): return hashlib.sha256(p.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

def get_user(): return session.get("user")
def get_account(email): return ACCOUNTS.get(email, {})
def get_flows(email): return FLOWS.get(email, [])
def get_stats(email): return STATS.get(email, {"msgs":0,"contacts":0,"bc":0})

# ── Core bot logic ──────────────────────────────────────────────
def ai_reply(text, cid, sys_prompt):
    history = [{"role":"assistant" if m["role"]=="ai" else "user","content":m["content"]}
               for m in CONVS.get(cid,[])[-8:] if m["role"] in ("user","ai")]
    history.append({"role":"user","content":text})
    try:
        r = claude.messages.create(model="claude-sonnet-4-20250514", max_tokens=500,
                                   system=sys_prompt, temperature=0.7, messages=history)
        return r.content[0].text
    except Exception as e:
        return f"Error IA: {e}"

def process_msg(email, sender_id, text, channel):
    cid = f"{email}_{channel}_{sender_id}"
    CONVS.setdefault(cid,[]).append({"role":"user","content":text})
    if email not in STATS: STATS[email] = {"msgs":0,"contacts":0,"bc":0}
    STATS[email]["msgs"] += 1

    flows = get_flows(email)
    flow = next((f for f in flows if f.get("active") and
                 f.get("trigger","").lower() in text.lower()), None)
    replies = []
    sys_prompt = ACCOUNTS.get(email,{}).get("ai_system",
        "Sos un asistente amable y profesional en español rioplatense.")

    if flow:
        for step in flow.get("steps",[]):
            if step["type"] == "ai":
                rep = ai_reply(text, cid, sys_prompt)
                CONVS[cid].append({"role":"ai","content":rep})
                replies.append(rep)
            else:
                c = step["content"]
                if step.get("options"):
                    c += "\n\n" + "\n".join(f"{i+1}. {o}" for i,o in enumerate(step["options"]))
                CONVS[cid].append({"role":"bot","content":c})
                replies.append(c)
    else:
        rep = ai_reply(text, cid, sys_prompt)
        CONVS[cid].append({"role":"ai","content":rep})
        replies.append(rep)
    return replies

def send_meta(token, rid, text):
    if not token: return
    req.post(f"https://graph.facebook.com/v19.0/me/messages?access_token={token}",
             json={"recipient":{"id":rid},"message":{"text":text}}, timeout=8)

def find_user_by_page(page_id):
    for email, acc in ACCOUNTS.items():
        if acc.get("page_id") == page_id:
            return email
    return None

def find_user_by_ig(ig_id):
    for email, acc in ACCOUNTS.items():
        if acc.get("ig_id") == ig_id:
            return email
    return None

# ── Webhooks ────────────────────────────────────────────────────
@app.route("/webhook/messenger", methods=["GET"])
def ms_verify():
    if (request.args.get("hub.mode")=="subscribe" and
        request.args.get("hub.verify_token")==META_VERIFY_TOKEN):
        return request.args.get("hub.challenge"), 200
    return "Forbidden", 403

@app.route("/webhook/messenger", methods=["POST"])
def ms_hook():
    data = request.json or {}
    if data.get("object") == "page":
        for entry in data.get("entry",[]):
            pid = entry.get("id","")
            email = find_user_by_page(pid)
            if not email: continue
            token = ACCOUNTS[email].get("fb_token","")
            for ev in entry.get("messaging",[]):
                sid  = ev.get("sender",{}).get("id","")
                text = (ev.get("message") or {}).get("text","")
                if sid and text and sid != pid:
                    for r in process_msg(email, sid, text, "messenger"):
                        send_meta(token, sid, r)
    return "ok", 200

@app.route("/webhook/instagram", methods=["GET"])
def ig_verify():
    if (request.args.get("hub.mode")=="subscribe" and
        request.args.get("hub.verify_token")==META_VERIFY_TOKEN):
        return request.args.get("hub.challenge"), 200
    return "Forbidden", 403

@app.route("/webhook/instagram", methods=["POST"])
def ig_hook():
    data = request.json or {}
    if data.get("object") == "instagram":
        for entry in data.get("entry",[]):
            ig_id = entry.get("id","")
            email = find_user_by_ig(ig_id)
            if not email: continue
            token = ACCOUNTS[email].get("fb_token","")
            for ev in entry.get("messaging",[]):
                sid  = ev.get("sender",{}).get("id","")
                text = (ev.get("message") or {}).get("text","")
                if sid and text and sid != ig_id:
                    for r in process_msg(email, sid, text, "instagram"):
                        send_meta(token, sid, r)
    return "ok", 200

# ── OAuth Facebook ──────────────────────────────────────────────
@app.route("/auth/facebook")
@login_required
def auth_facebook():
    email = get_user()
    redirect_uri = f"{BASE_URL}/auth/facebook/callback"
    scope = "pages_messaging,instagram_basic,instagram_manage_messages,pages_manage_metadata,pages_read_engagement"
    url = (f"https://www.facebook.com/v19.0/dialog/oauth"
           f"?client_id={META_APP_ID}"
           f"&redirect_uri={redirect_uri}"
           f"&scope={scope}"
           f"&state={email}")
    return redirect(url)

@app.route("/auth/facebook/callback")
def auth_facebook_callback():
    code  = request.args.get("code","")
    email = request.args.get("state","")
    if not code or not email:
        return redirect("/dashboard?error=auth_failed")

    # Obtener access token
    redirect_uri = f"{BASE_URL}/auth/facebook/callback"
    r = req.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "client_id": META_APP_ID, "client_secret": META_APP_SECRET,
        "redirect_uri": redirect_uri, "code": code
    })
    token_data = r.json()
    user_token = token_data.get("access_token","")
    if not user_token:
        return redirect("/dashboard?error=no_token")

    # Obtener páginas del usuario
    r2 = req.get(f"https://graph.facebook.com/v19.0/me/accounts",
                 params={"access_token": user_token})
    pages = r2.json().get("data",[])
    if not pages:
        return redirect("/dashboard?error=no_pages")

    page = pages[0]
    page_token = page.get("access_token","")
    page_id    = page.get("id","")
    page_name  = page.get("name","")

    # Obtener cuenta de Instagram vinculada
    r3 = req.get(f"https://graph.facebook.com/v19.0/{page_id}",
                 params={"fields":"instagram_business_account","access_token":page_token})
    ig_data = r3.json().get("instagram_business_account",{})
    ig_id   = ig_data.get("id","")

    # Guardar en accounts
    if email not in ACCOUNTS:
        ACCOUNTS[email] = {}
    ACCOUNTS[email].update({
        "fb_token":  page_token,
        "page_id":   page_id,
        "page_name": page_name,
        "ig_id":     ig_id,
        "connected": True,
        "connected_at": datetime.now().strftime("%d/%m/%Y %H:%M")
    })

    # Suscribir página al webhook
    req.post(f"https://graph.facebook.com/v19.0/{page_id}/subscribed_apps",
             params={"access_token": page_token,
                     "subscribed_fields": "messages,messaging_postbacks"})
    if ig_id:
        req.post(f"https://graph.facebook.com/v19.0/{ig_id}/subscribed_apps",
                 params={"access_token": page_token,
                         "subscribed_fields": "messages,messaging_postbacks"})

    return redirect("/dashboard?success=connected")

@app.route("/auth/disconnect", methods=["POST"])
@login_required
def auth_disconnect():
    email = get_user()
    if email in ACCOUNTS:
        ACCOUNTS[email] = {}
    return redirect("/dashboard")

# ── Auth routes ─────────────────────────────────────────────────
@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        d = request.json or request.form
        email = d.get("email","").strip().lower()
        name  = d.get("name","").strip()
        pwd   = d.get("password","").strip()
        if not email or not pwd or not name:
            return jsonify({"ok":False,"msg":"Completá todos los campos"}), 400
        if email in USERS:
            return jsonify({"ok":False,"msg":"Email ya registrado"}), 400
        USERS[email] = {"name":name,"password":hash_pass(pwd),
                        "plan":"free","created":datetime.now().strftime("%d/%m/%Y")}
        FLOWS[email] = [
            {"id":"bienvenida","name":"Bienvenida","trigger":"hola",
             "steps":[{"type":"message","content":"👋 ¡Hola! ¿En qué te puedo ayudar?"},
                      {"type":"ai","content":""}],"active":True}
        ]
        return jsonify({"ok":True})
    return render_template_string(HTML_REGISTER)

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        d = request.json or request.form
        email = d.get("email","").strip().lower()
        pwd   = d.get("password","").strip()
        # Admin
        if email == ADMIN_EMAIL and pwd == ADMIN_PASS:
            session["user"] = email
            session["is_admin"] = True
            return jsonify({"ok":True,"redirect":"/admin"})
        u = USERS.get(email)
        if not u or u["password"] != hash_pass(pwd):
            return jsonify({"ok":False,"msg":"Email o contraseña incorrectos"}), 401
        session["user"] = email
        session["is_admin"] = False
        return jsonify({"ok":True,"redirect":"/dashboard"})
    return render_template_string(HTML_LOGIN)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ── API ─────────────────────────────────────────────────────────
@app.route("/api/account")
@login_required
def api_account():
    email = get_user()
    acc = get_account(email)
    return jsonify({"connected": acc.get("connected",False),
                    "page_name": acc.get("page_name",""),
                    "ig_id":     acc.get("ig_id",""),
                    "connected_at": acc.get("connected_at","")})

@app.route("/api/flows", methods=["GET"])
@login_required
def api_flows_get():
    return jsonify(get_flows(get_user()))

@app.route("/api/flows", methods=["POST"])
@login_required
def api_flows_post():
    email = get_user()
    d = request.json or {}
    flow = {"id": f"flow_{int(datetime.now().timestamp())}",
            "name": d.get("name",""), "trigger": d.get("trigger","").lower(),
            "steps": d.get("steps",[]), "active": True}
    FLOWS.setdefault(email,[]).append(flow)
    return jsonify({"ok":True,"flow":flow})

@app.route("/api/flows/<fid>", methods=["DELETE"])
@login_required
def api_flow_delete(fid):
    email = get_user()
    FLOWS[email] = [f for f in FLOWS.get(email,[]) if f["id"] != fid]
    return jsonify({"ok":True})

@app.route("/api/flows/<fid>/toggle", methods=["POST"])
@login_required
def api_flow_toggle(fid):
    email = get_user()
    for f in FLOWS.get(email,[]):
        if f["id"] == fid:
            f["active"] = not f["active"]
    return jsonify({"ok":True})

@app.route("/api/stats")
@login_required
def api_stats():
    email = get_user()
    acc = get_account(email)
    st  = get_stats(email)
    convs_count = sum(1 for k in CONVS if k.startswith(email+"_"))
    return jsonify({"msgs": st.get("msgs",0), "contacts": convs_count,
                    "flows": len([f for f in get_flows(email) if f.get("active")]),
                    "connected": acc.get("connected",False),
                    "page_name": acc.get("page_name",""),
                    "ig_id": acc.get("ig_id","")})

@app.route("/api/ai-config", methods=["POST"])
@login_required
def api_ai_config():
    email = get_user()
    d = request.json or {}
    ACCOUNTS.setdefault(email,{})["ai_system"] = d.get("system","")
    return jsonify({"ok":True})

@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    email = get_user()
    d = request.json or {}
    sys_prompt = ACCOUNTS.get(email,{}).get("ai_system",
        "Sos un asistente amable en español rioplatense.")
    try:
        r = claude.messages.create(model="claude-sonnet-4-20250514", max_tokens=600,
                                   system=d.get("system", sys_prompt),
                                   temperature=float(d.get("temperature",0.7)),
                                   messages=d.get("messages",[]))
        return jsonify({"reply":r.content[0].text,"ok":True})
    except Exception as e:
        return jsonify({"reply":str(e),"ok":False}), 500

# ── Admin ───────────────────────────────────────────────────────
@app.route("/admin")
@login_required
def admin():
    if not session.get("is_admin"):
        return redirect("/dashboard")
    return render_template_string(HTML_ADMIN,
        users=USERS, accounts=ACCOUNTS, stats=STATS,
        total_users=len(USERS),
        total_connected=sum(1 for a in ACCOUNTS.values() if a.get("connected")),
        total_msgs=sum(s.get("msgs",0) for s in STATS.values()))

# ── Dashboard ───────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    return render_template_string(HTML_DASHBOARD)

@app.route("/")
def index():
    if "user" in session:
        return redirect("/dashboard")
    return redirect("/login")

@app.route("/health")
def health():
    return jsonify({"status":"ok","users":len(USERS),"version":"4.0-saas"})

# ══════════════════════════════════════════════════════════════
#  HTML TEMPLATES
# ══════════════════════════════════════════════════════════════

HTML_LOGIN = """<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autochat — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--ink:#0a0a0c;--paper:#f5f4f0;--warm:#ede9e0;--line:#d8d4ca;--el:#4f46e5;--em:#10b981;--red:#ef4444}
body{min-height:100vh;background:var(--ink);display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif}
.card{background:var(--paper);border-radius:20px;padding:40px 36px;width:92%;max-width:400px;box-shadow:0 20px 60px rgba(0,0,0,.4)}
.logo{text-align:center;margin-bottom:28px}
.logo-mark{width:52px;height:52px;background:var(--el);border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:24px;margin:0 auto 12px;box-shadow:0 0 30px rgba(79,70,229,.5)}
.logo h1{font-size:22px;font-weight:800;letter-spacing:-.5px}
.logo p{font-size:13px;color:rgba(10,10,12,.45);margin-top:4px}
.field{margin-bottom:16px}
.field label{font-size:11px;font-weight:700;color:rgba(10,10,12,.45);letter-spacing:.6px;text-transform:uppercase;margin-bottom:6px;display:block}
.field input{width:100%;background:#fff;border:1.5px solid var(--line);border-radius:9px;padding:11px 14px;font-size:14px;font-family:'Syne',sans-serif;outline:none;transition:border .15s;color:var(--ink)}
.field input:focus{border-color:var(--el)}
.btn{width:100%;background:var(--el);color:#fff;border:none;border-radius:9px;padding:13px;font-size:14px;font-weight:700;cursor:pointer;font-family:'Syne',sans-serif;transition:all .15s;margin-top:4px}
.btn:hover{background:#4338ca;transform:translateY(-1px)}
.err{background:#fef2f2;border:1px solid #fee2e2;color:var(--red);border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:14px;display:none}
.footer{text-align:center;margin-top:20px;font-size:13px;color:rgba(10,10,12,.4)}
.footer a{color:var(--el);text-decoration:none;font-weight:600}
</style></head><body>
<div class="card">
  <div class="logo">
    <div class="logo-mark">⚡</div>
    <h1>Autochat</h1>
    <p>Tu ManyChat con IA</p>
  </div>
  <div class="err" id="err"></div>
  <div class="field"><label>Email</label><input id="email" type="email" placeholder="tu@email.com"></div>
  <div class="field"><label>Contraseña</label><input id="pwd" type="password" placeholder="••••••••" onkeypress="if(event.key==='Enter')login()"></div>
  <button class="btn" onclick="login()">Entrar</button>
  <div class="footer">¿No tenés cuenta? <a href="/register">Registrate gratis</a></div>
</div>
<script>
async function login(){
  const email=document.getElementById('email').value;
  const pwd=document.getElementById('pwd').value;
  const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pwd})});
  const d=await r.json();
  if(d.ok) window.location=d.redirect;
  else{const e=document.getElementById('err');e.textContent=d.msg;e.style.display='block';}
}
</script></body></html>"""

HTML_REGISTER = """<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autochat — Registro</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--ink:#0a0a0c;--paper:#f5f4f0;--warm:#ede9e0;--line:#d8d4ca;--el:#4f46e5;--em:#10b981;--red:#ef4444}
body{min-height:100vh;background:var(--ink);display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif}
.card{background:var(--paper);border-radius:20px;padding:40px 36px;width:92%;max-width:400px;box-shadow:0 20px 60px rgba(0,0,0,.4)}
.logo{text-align:center;margin-bottom:28px}
.logo-mark{width:52px;height:52px;background:var(--el);border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:24px;margin:0 auto 12px;box-shadow:0 0 30px rgba(79,70,229,.5)}
.logo h1{font-size:22px;font-weight:800;letter-spacing:-.5px}
.logo p{font-size:13px;color:rgba(10,10,12,.45);margin-top:4px}
.field{margin-bottom:16px}
.field label{font-size:11px;font-weight:700;color:rgba(10,10,12,.45);letter-spacing:.6px;text-transform:uppercase;margin-bottom:6px;display:block}
.field input{width:100%;background:#fff;border:1.5px solid var(--line);border-radius:9px;padding:11px 14px;font-size:14px;font-family:'Syne',sans-serif;outline:none;transition:border .15s;color:var(--ink)}
.field input:focus{border-color:var(--el)}
.btn{width:100%;background:var(--el);color:#fff;border:none;border-radius:9px;padding:13px;font-size:14px;font-weight:700;cursor:pointer;font-family:'Syne',sans-serif;transition:all .15s;margin-top:4px}
.btn:hover{background:#4338ca;transform:translateY(-1px)}
.err{background:#fef2f2;border:1px solid #fee2e2;color:var(--red);border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:14px;display:none}
.ok-msg{background:#f0fdf4;border:1px solid #bbf7d0;color:var(--em);border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:14px;display:none}
.footer{text-align:center;margin-top:20px;font-size:13px;color:rgba(10,10,12,.4)}
.footer a{color:var(--el);text-decoration:none;font-weight:600}
</style></head><body>
<div class="card">
  <div class="logo">
    <div class="logo-mark">⚡</div>
    <h1>Autochat</h1>
    <p>Creá tu cuenta gratis</p>
  </div>
  <div class="err" id="err"></div>
  <div class="ok-msg" id="ok"></div>
  <div class="field"><label>Nombre</label><input id="name" placeholder="Tu nombre"></div>
  <div class="field"><label>Email</label><input id="email" type="email" placeholder="tu@email.com"></div>
  <div class="field"><label>Contraseña</label><input id="pwd" type="password" placeholder="Mínimo 6 caracteres"></div>
  <button class="btn" onclick="register()">Crear cuenta</button>
  <div class="footer">¿Ya tenés cuenta? <a href="/login">Iniciá sesión</a></div>
</div>
<script>
async function register(){
  const name=document.getElementById('name').value;
  const email=document.getElementById('email').value;
  const pwd=document.getElementById('pwd').value;
  if(pwd.length<6){showErr('La contraseña debe tener al menos 6 caracteres');return;}
  const r=await fetch('/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,email,password:pwd})});
  const d=await r.json();
  if(d.ok){document.getElementById('ok').textContent='✅ Cuenta creada. Redirigiendo...';document.getElementById('ok').style.display='block';setTimeout(()=>window.location='/login',1500);}
  else showErr(d.msg);
}
function showErr(m){const e=document.getElementById('err');e.textContent=m;e.style.display='block';}
</script></body></html>"""

HTML_ADMIN = """<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autochat — Admin</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--ink:#0a0a0c;--paper:#f5f4f0;--warm:#ede9e0;--line:#d8d4ca;--el:#4f46e5;--em:#10b981;--red:#ef4444}
body{background:var(--ink);color:#fff;font-family:'Syne',sans-serif;min-height:100vh;padding:24px}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}
.header h1{font-size:20px;font-weight:800}
a{color:#818cf8;text-decoration:none}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:24px}
.stat{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:14px;padding:18px}
.stat-label{font-size:11px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.stat-num{font-size:28px;font-weight:800;font-family:'JetBrains Mono',monospace;color:var(--el)}
table{width:100%;border-collapse:collapse;font-size:13px;background:rgba(255,255,255,.04);border-radius:12px;overflow:hidden}
th{text-align:left;padding:10px 14px;font-size:10px;font-weight:700;color:rgba(255,255,255,.3);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid rgba(255,255,255,.08)}
td{padding:11px 14px;border-bottom:1px solid rgba(255,255,255,.05);color:rgba(255,255,255,.8)}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:700}
.badge-on{background:#d1fae5;color:#065f46}.badge-off{background:rgba(255,255,255,.1);color:rgba(255,255,255,.4)}
</style></head><body>
<div class="header">
  <h1>⚡ Admin — Autochat</h1>
  <a href="/logout">Salir</a>
</div>
<div class="stats">
  <div class="stat"><div class="stat-label">Usuarios</div><div class="stat-num">{{total_users}}</div></div>
  <div class="stat"><div class="stat-label">Conectados</div><div class="stat-num">{{total_connected}}</div></div>
  <div class="stat"><div class="stat-label">Mensajes</div><div class="stat-num">{{total_msgs}}</div></div>
</div>
<table>
  <thead><tr><th>Email</th><th>Nombre</th><th>Plan</th><th>Conectado</th><th>Msgs</th><th>Creado</th></tr></thead>
  <tbody>
  {% for email, u in users.items() %}
  <tr>
    <td>{{email}}</td>
    <td>{{u.name}}</td>
    <td><span class="badge badge-on">{{u.plan}}</span></td>
    <td>{% if accounts.get(email,{}).get('connected') %}<span class="badge badge-on">✅ {{accounts[email].get('page_name','')}}</span>{% else %}<span class="badge badge-off">No</span>{% endif %}</td>
    <td>{{stats.get(email,{}).get('msgs',0)}}</td>
    <td>{{u.created}}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
</body></html>"""

HTML_DASHBOARD = r"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autochat</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{--ink:#0a0a0c;--paper:#f5f4f0;--warm:#ede9e0;--line:#d8d4ca;--el:#4f46e5;--el2:#818cf8;--wa:#25d366;--fb:#0866ff;--ig:#e1306c;--red:#ef4444;--amber:#f59e0b;--em:#10b981;--sh:0 1px 3px rgba(0,0,0,.06),0 4px 16px rgba(0,0,0,.04)}
html,body{height:100%;font-family:'Syne',sans-serif;background:var(--paper);color:var(--ink);overflow:hidden}
.shell{display:flex;height:100vh}
.sb{width:228px;flex-shrink:0;background:var(--ink);display:flex;flex-direction:column;position:relative;overflow:hidden}
.sb::before{content:'';position:absolute;top:-80px;left:-80px;width:280px;height:280px;background:radial-gradient(circle,rgba(79,70,229,.22) 0%,transparent 70%);pointer-events:none}
.logo{padding:24px 20px 18px;display:flex;align-items:center;gap:11px;border-bottom:1px solid rgba(255,255,255,.07)}
.lm{width:34px;height:34px;background:var(--el);border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:17px;box-shadow:0 0 18px rgba(79,70,229,.55)}
.ln{font-size:16px;font-weight:800;color:#fff;letter-spacing:-.4px;line-height:1}
.ln small{display:block;font-size:9px;font-weight:400;color:rgba(255,255,255,.3);letter-spacing:.6px;margin-top:2px}
nav{flex:1;padding:12px 9px;overflow-y:auto}
.ns{font-size:9px;font-weight:600;color:rgba(255,255,255,.22);letter-spacing:1.5px;text-transform:uppercase;padding:11px 13px 5px}
.ni{display:flex;align-items:center;gap:9px;padding:9px 13px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;color:rgba(255,255,255,.42);transition:all .14s;margin-bottom:1px;position:relative}
.ni:hover{color:rgba(255,255,255,.82);background:rgba(255,255,255,.05)}
.ni.on{color:#fff;background:rgba(255,255,255,.1)}
.ni.on::before{content:'';position:absolute;left:0;top:50%;transform:translateY(-50%);width:3px;height:17px;background:var(--el);border-radius:0 3px 3px 0}
.ni .ico{font-size:15px;width:17px;text-align:center;opacity:.75}
.sbf{padding:14px;border-top:1px solid rgba(255,255,255,.06)}
.user-card{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:9px;padding:11px}
.user-name{font-size:13px;font-weight:700;color:#fff}
.user-plan{font-size:10px;color:rgba(255,255,255,.4);margin-top:2px}
.logout{display:block;text-align:center;margin-top:8px;font-size:11px;color:rgba(255,255,255,.3);text-decoration:none}
.logout:hover{color:rgba(255,255,255,.6)}
.main{flex:1;display:flex;flex-direction:column;min-width:0}
.tp{height:56px;flex-shrink:0;background:var(--paper);border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 26px;gap:14px}
.tp h1{font-size:17px;font-weight:700;flex:1;letter-spacing:-.3px}
.btn{display:inline-flex;align-items:center;gap:5px;padding:7px 15px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;border:none;transition:all .14s;font-family:'Syne',sans-serif;line-height:1;white-space:nowrap}
.bp{background:var(--el);color:#fff;box-shadow:0 2px 8px rgba(79,70,229,.28)}.bp:hover{background:#4338ca;transform:translateY(-1px)}
.bo{background:transparent;color:var(--ink);border:1.5px solid var(--line)}.bo:hover{border-color:var(--el);color:var(--el)}
.bd{background:#fef2f2;color:var(--red);border:1px solid #fee2e2}
.bsm{padding:5px 11px;font-size:12px}.bxs{padding:3px 8px;font-size:11px}
.bfb{background:linear-gradient(135deg,#1877f2,#0866ff);color:#fff;border:none}
.bfb:hover{opacity:.9;transform:translateY(-1px)}
.page{display:none;flex:1;overflow-y:auto;padding:24px 26px}.page.active{display:block}
.sr{display:grid;grid-template-columns:repeat(4,1fr);gap:13px;margin-bottom:18px}
.sc{background:#fff;border:1px solid var(--line);border-radius:15px;padding:17px 19px;box-shadow:var(--sh)}
.sl{font-size:11px;font-weight:600;color:rgba(10,10,12,.38);letter-spacing:.5px;text-transform:uppercase;margin-bottom:7px}
.sn{font-size:26px;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1;letter-spacing:-1px}
.cel{color:var(--el)}.cem{color:var(--em)}.cfb{color:var(--fb)}.cam{color:var(--amber)}
.ss2{font-size:11px;color:rgba(10,10,12,.32);margin-top:3px}
/* Connect card */
.conn-card{background:#fff;border:2px solid var(--line);border-radius:16px;padding:24px;max-width:520px;box-shadow:var(--sh)}
.conn-card.connected{border-color:var(--em)}
.conn-head{display:flex;align-items:center;gap:14px;margin-bottom:18px}
.conn-ico{width:52px;height:52px;border-radius:14px;background:linear-gradient(135deg,#1877f2,#0866ff);display:flex;align-items:center;justify-content:center;font-size:24px}
.conn-title{font-size:17px;font-weight:800;letter-spacing:-.3px}
.conn-sub{font-size:13px;color:rgba(10,10,12,.45);margin-top:3px}
.conn-status{display:flex;align-items:center;gap:8px;padding:11px 14px;border-radius:9px;margin-bottom:16px;font-size:13px;font-weight:600}
.conn-status.ok{background:#f0fdf4;color:#065f46}
.conn-status.off{background:var(--warm);color:rgba(10,10,12,.5)}
.what-get{background:var(--warm);border-radius:10px;padding:14px;margin-bottom:16px}
.what-get p{font-size:12px;font-weight:700;color:rgba(10,10,12,.45);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.what-get ul{list-style:none;display:flex;flex-direction:column;gap:5px}
.what-get li{font-size:13px;color:rgba(10,10,12,.65)}
/* Flows */
.fg{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:13px;margin-bottom:22px}
.fc{background:#fff;border:1.5px solid var(--line);border-radius:15px;padding:17px;box-shadow:var(--sh)}
.fc:hover{border-color:var(--el)}
.ftop{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:9px}
.fn{font-size:14px;font-weight:700}.ftr{font-size:11px;color:rgba(10,10,12,.38);margin-top:2px;font-family:'JetBrains Mono',monospace}
.bon{background:#d1fae5;color:#065f46;padding:3px 8px;border-radius:20px;font-size:10px;font-weight:700}
.boff{background:var(--warm);color:rgba(10,10,12,.38);padding:3px 8px;border-radius:20px;font-size:10px;font-weight:700}
.fa{display:flex;gap:7px;margin-top:11px}
.sb3{background:var(--warm);border:1.5px solid var(--line);border-radius:15px;padding:19px;max-width:650px}
.stl{display:flex;flex-direction:column;gap:8px;margin:13px 0}
.sti{background:#fff;border:1.5px solid var(--line);border-radius:9px;padding:12px;display:flex;align-items:flex-start;gap:10px}
.stb{background:var(--el);color:#fff;width:23px;height:23px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex-shrink:0;margin-top:1px}
.stt{font-size:9px;font-weight:700;color:var(--el);letter-spacing:.8px;text-transform:uppercase;margin-bottom:4px}
.std{background:none;border:none;color:rgba(10,10,12,.22);cursor:pointer;font-size:17px;padding:0 3px;line-height:1}.std:hover{color:var(--red)}
.as2{display:flex;gap:7px;flex-wrap:wrap;margin-top:11px}
.fi{margin-bottom:15px}
.fl2{font-size:11px;font-weight:700;color:rgba(10,10,12,.42);letter-spacing:.6px;text-transform:uppercase;margin-bottom:5px;display:block}
.inp,.ta,.sel{width:100%;background:#fff;border:1.5px solid var(--line);border-radius:8px;padding:9px 12px;color:var(--ink);font-size:14px;font-family:'Syne',sans-serif;outline:none;transition:border .14s}
.inp:focus,.ta:focus{border-color:var(--el)}
.ta{resize:vertical;min-height:88px;line-height:1.5}
.fh{font-size:11px;color:rgba(10,10,12,.32);margin-top:4px}
.stitle{font-size:14px;font-weight:800;letter-spacing:-.2px;margin-bottom:13px}
.aif{background:#fff;border:1px solid var(--line);border-radius:15px;padding:22px;max-width:610px;box-shadow:var(--sh)}
.rt{-webkit-appearance:none;appearance:none;width:100%;height:4px;background:var(--line);border-radius:4px;outline:none;cursor:pointer}
.rt::-webkit-slider-thumb{-webkit-appearance:none;width:17px;height:17px;border-radius:50%;background:var(--el);cursor:pointer}
.tbox{background:var(--warm);border:1.5px solid var(--line);border-radius:9px;padding:15px;margin-top:15px}
.tr3{margin-top:11px;padding:12px;background:#fff;border:1px solid var(--line);border-radius:8px;font-size:14px;line-height:1.54;display:none}
.toast{position:fixed;bottom:20px;right:20px;background:var(--ink);color:#fff;padding:10px 17px;border-radius:9px;font-weight:600;font-size:13px;z-index:9999;transform:translateY(80px);opacity:0;transition:all .3s}
.toast.show{transform:translateY(0);opacity:1}
.alert{padding:12px 15px;border-radius:9px;font-size:13px;margin-bottom:16px;display:none}
.alert-ok{background:#f0fdf4;border:1px solid #bbf7d0;color:#065f46}
.alert-err{background:#fef2f2;border:1px solid #fee2e2;color:var(--red)}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
@media(max-width:680px){.sb{display:none}.sr{grid-template-columns:repeat(2,1fr)}}
</style></head><body>
<div class="shell">
<aside class="sb">
  <div class="logo"><div class="lm">⚡</div><div class="ln">Autochat<small>TU MANYCHAT CON IA</small></div></div>
  <nav>
    <div class="ns">Principal</div>
    <div class="ni on" onclick="go('dashboard',this)"><span class="ico">◼</span>Dashboard</div>
    <div class="ni" onclick="go('connect',this)"><span class="ico">◈</span>Conectar redes</div>
    <div class="ns">Automatización</div>
    <div class="ni" onclick="go('flows',this)"><span class="ico">⇄</span>Flujos</div>
    <div class="ns">Configuración</div>
    <div class="ni" onclick="go('ai',this)"><span class="ico">◉</span>IA Config</div>
  </nav>
  <div class="sbf">
    <div class="user-card">
      <div class="user-name" id="sb-name">Cargando...</div>
      <div class="user-plan">Plan Free</div>
    </div>
    <a href="/logout" class="logout">Cerrar sesión</a>
  </div>
</aside>
<div class="main">
  <div class="tp"><h1 id="ptitle">Dashboard</h1></div>

  <!-- DASHBOARD -->
  <div id="page-dashboard" class="page active">
    <div id="alert-area"></div>
    <div class="sr">
      <div class="sc"><div class="sl">Mensajes</div><div class="sn cel" id="s-msgs">0</div><div class="ss2">recibidos</div></div>
      <div class="sc"><div class="sl">Contactos</div><div class="sn cem" id="s-contacts">0</div><div class="ss2">únicos</div></div>
      <div class="sc"><div class="sl">Flujos</div><div class="sn cam" id="s-flows">0</div><div class="ss2">activos</div></div>
      <div class="sc"><div class="sl">Estado</div><div class="sn" id="s-status" style="font-size:14px;margin-top:4px">—</div><div class="ss2">conexión</div></div>
    </div>
    <div style="background:#fff;border:1px solid var(--line);border-radius:15px;padding:22px;max-width:520px;box-shadow:var(--sh)">
      <div class="stitle">🚀 Empezá ahora</div>
      <div style="display:flex;flex-direction:column;gap:11px">
        <div onclick="go('connect',document.querySelectorAll('.ni')[1])" style="background:var(--warm);border:1.5px solid var(--line);border-radius:11px;padding:15px;cursor:pointer;display:flex;align-items:center;gap:13px" onmouseover="this.style.borderColor='var(--el)'" onmouseout="this.style.borderColor='var(--line)'">
          <div style="font-size:24px">🔗</div>
          <div><div style="font-weight:700;font-size:14px">Conectar Instagram / Facebook</div><div style="font-size:12px;color:rgba(10,10,12,.45);margin-top:2px">Un click y tu cuenta queda conectada</div></div>
        </div>
        <div onclick="go('flows',document.querySelectorAll('.ni')[3])" style="background:var(--warm);border:1.5px solid var(--line);border-radius:11px;padding:15px;cursor:pointer;display:flex;align-items:center;gap:13px" onmouseover="this.style.borderColor='var(--el)'" onmouseout="this.style.borderColor='var(--line)'">
          <div style="font-size:24px">⇄</div>
          <div><div style="font-weight:700;font-size:14px">Crear flujo automático</div><div style="font-size:12px;color:rgba(10,10,12,.45);margin-top:2px">Respuestas con palabras clave + IA</div></div>
        </div>
        <div onclick="go('ai',document.querySelectorAll('.ni')[5])" style="background:var(--warm);border:1.5px solid var(--line);border-radius:11px;padding:15px;cursor:pointer;display:flex;align-items:center;gap:13px" onmouseover="this.style.borderColor='var(--el)'" onmouseout="this.style.borderColor='var(--line)'">
          <div style="font-size:24px">◉</div>
          <div><div style="font-weight:700;font-size:14px">Configurar IA</div><div style="font-size:12px;color:rgba(10,10,12,.45);margin-top:2px">Personalizá cómo responde tu bot</div></div>
        </div>
      </div>
    </div>
  </div>

  <!-- CONECTAR REDES -->
  <div id="page-connect" class="page">
    <div class="stitle">Conectar tus redes sociales</div>
    <div class="conn-card" id="conn-card">
      <div class="conn-head">
        <div class="conn-ico">f</div>
        <div>
          <div class="conn-title">Facebook + Instagram</div>
          <div class="conn-sub">Conectá una vez y controlás ambas desde Autochat</div>
        </div>
      </div>
      <div class="conn-status off" id="conn-status">
        ● No conectado
      </div>
      <div class="what-get">
        <p>Al conectar podés:</p>
        <ul>
          <li>✅ Responder DMs de Instagram automáticamente</li>
          <li>✅ Responder mensajes de Facebook Messenger</li>
          <li>✅ Activar flujos con palabras clave</li>
          <li>✅ Respuestas con IA (Claude)</li>
          <li>✅ Estadísticas de conversaciones</li>
        </ul>
      </div>
      <div id="conn-actions">
        <a href="/auth/facebook" class="btn bfb" style="display:inline-flex;text-decoration:none;width:100%;justify-content:center;padding:13px">
          f &nbsp; Conectar con Facebook
        </a>
      </div>
      <div id="conn-info" style="display:none;margin-top:14px">
        <div style="font-size:13px;color:rgba(10,10,12,.5);margin-bottom:10px">
          Página conectada: <strong id="conn-page"></strong><br>
          Instagram ID: <strong id="conn-ig"></strong><br>
          Conectado: <strong id="conn-date"></strong>
        </div>
        <form method="POST" action="/auth/disconnect" style="margin:0">
          <button type="submit" class="btn bd bsm">Desconectar</button>
        </form>
      </div>
    </div>

    <div style="margin-top:22px;background:var(--warm);border:1px solid var(--line);border-radius:12px;padding:16px;max-width:520px">
      <div style="font-size:12px;font-weight:700;color:rgba(10,10,12,.45);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px">¿Cómo funciona?</div>
      <div style="font-size:13px;color:rgba(10,10,12,.55);line-height:1.7">
        1. Tocás "Conectar con Facebook"<br>
        2. Autorizás Autochat en Meta<br>
        3. Elegís tu página de Facebook<br>
        4. Tu Instagram vinculado se conecta solo<br>
        5. ¡Listo! El bot empieza a responder
      </div>
    </div>
  </div>

  <!-- FLUJOS -->
  <div id="page-flows" class="page">
    <div id="fg" class="fg"></div>
    <div class="stitle">Crear nuevo flujo</div>
    <div class="sb3">
      <div class="fi"><label class="fl2">Nombre</label><input id="nfn" class="inp" placeholder="Ej: Bienvenida"></div>
      <div class="fi"><label class="fl2">Trigger (palabra clave)</label><input id="nft" class="inp" placeholder="Ej: hola · precio · info"><div class="fh">Cuando alguien escriba esta palabra se activa el flujo</div></div>
      <div class="fi">
        <label class="fl2">Pasos</label>
        <div id="nsteps" class="stl"></div>
        <div class="as2">
          <button class="btn bo bsm" onclick="addSt('message')">+ Mensaje</button>
          <button class="btn bo bsm" onclick="addSt('options')">+ Opciones</button>
          <button class="btn bp bsm" onclick="addSt('ai')">◉ Respuesta IA</button>
        </div>
      </div>
      <button class="btn bp" onclick="saveFlow()">Guardar flujo</button>
    </div>
  </div>

  <!-- IA CONFIG -->
  <div id="page-ai" class="page">
    <div class="aif">
      <div class="stitle">Configuración del asistente IA</div>
      <div class="fi"><label class="fl2">System prompt — cómo responde tu bot</label>
        <textarea id="ais" class="ta" style="min-height:130px">Sos un asistente de atención al cliente amable, profesional y en español rioplatense. Respondés de forma concisa y útil.</textarea>
        <div class="fh">Personalizalo con el nombre de tu negocio, productos y tono.</div>
      </div>
      <div class="fi">
        <label class="fl2">Temperatura — <span id="tv" style="color:var(--el);font-family:'JetBrains Mono',monospace">0.7</span></label>
        <input id="ait" type="range" min="0" max="1" step="0.1" value="0.7" class="rt" oninput="document.getElementById('tv').textContent=this.value">
        <div class="fh">0 = exacto y consistente · 1 = creativo y variado</div>
      </div>
      <button class="btn bp" onclick="saveAI()">Guardar</button>
      <div class="tbox">
        <div class="stitle" style="font-size:13px;margin:0 0 9px">Probar IA</div>
        <div style="display:flex;gap:8px"><input id="aiti" class="inp" placeholder="Escribí algo para probar..."><button class="btn bp" onclick="testAI()">Probar</button></div>
        <div id="aitr" class="tr3"></div>
      </div>
    </div>
  </div>

</div></div>
<div class="toast" id="toast"></div>
<script>
const TT={dashboard:'Dashboard',connect:'Conectar redes',flows:'Flujos',ai:'IA Config'};
let NS=[];

function go(id,el){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.ni').forEach(n=>n.classList.remove('on'));
  document.getElementById('page-'+id).classList.add('active');
  if(el)el.classList.add('on');
  document.getElementById('ptitle').textContent=TT[id]||id;
  if(id==='dashboard')loadStats();
  if(id==='connect')loadAccount();
  if(id==='flows')loadFlows();
}

async function loadStats(){
  const r=await fetch('/api/stats');
  const d=await r.json();
  document.getElementById('s-msgs').textContent=d.msgs||0;
  document.getElementById('s-contacts').textContent=d.contacts||0;
  document.getElementById('s-flows').textContent=d.flows||0;
  const st=document.getElementById('s-status');
  if(d.connected){st.textContent='✅ '+d.page_name;st.style.color='var(--em)';}
  else{st.textContent='Sin conectar';st.style.color='rgba(10,10,12,.4)';}
  // check URL params
  const p=new URLSearchParams(window.location.search);
  const aa=document.getElementById('alert-area');
  if(p.get('success')==='connected'){aa.innerHTML='<div class="alert alert-ok" style="display:block">✅ ¡Cuenta conectada exitosamente! El bot ya está activo.</div>';}
  if(p.get('error')){aa.innerHTML='<div class="alert alert-err" style="display:block">❌ Error al conectar: '+p.get('error')+'</div>';}
}

async function loadAccount(){
  const r=await fetch('/api/account');
  const d=await r.json();
  const card=document.getElementById('conn-card');
  const status=document.getElementById('conn-status');
  const info=document.getElementById('conn-info');
  const actions=document.getElementById('conn-actions');
  if(d.connected){
    card.classList.add('connected');
    status.className='conn-status ok';
    status.textContent='✅ Conectado';
    document.getElementById('conn-page').textContent=d.page_name||'—';
    document.getElementById('conn-ig').textContent=d.ig_id||'No detectado';
    document.getElementById('conn-date').textContent=d.connected_at||'—';
    info.style.display='block';
    actions.style.display='none';
  } else {
    card.classList.remove('connected');
    status.className='conn-status off';
    status.textContent='● No conectado';
    info.style.display='none';
    actions.style.display='block';
  }
}

async function loadFlows(){
  const r=await fetch('/api/flows');
  const flows=await r.json();
  const g=document.getElementById('fg');g.innerHTML='';
  flows.forEach(f=>{
    const d=document.createElement('div');d.className='fc';
    d.innerHTML=`<div class="ftop"><div><div class="fn">${f.name}</div><div class="ftr">trigger: "${f.trigger}"</div></div><span class="${f.active?'bon':'boff'}">${f.active?'ACTIVO':'PAUSA'}</span></div><div style="font-size:12px;color:rgba(10,10,12,.32);margin-bottom:11px">${f.steps.length} paso(s)</div><div class="fa"><button class="btn bo bsm" onclick="togFlow('${f.id}')">⏸ Toggle</button><button class="btn bd bsm" onclick="delFlow('${f.id}')">✕ Borrar</button></div>`;
    g.appendChild(d);
  });
}

async function togFlow(id){
  await fetch('/api/flows/'+id+'/toggle',{method:'POST'});
  loadFlows();toast('✓ Actualizado');
}
async function delFlow(id){
  if(!confirm('¿Eliminar?'))return;
  await fetch('/api/flows/'+id,{method:'DELETE'});
  loadFlows();toast('✓ Eliminado');
}

function addSt(t){NS.push({type:t,content:'',options:t==='options'?['Opción 1','Opción 2']:[]});rNS();}
function rNS(){
  const c=document.getElementById('nsteps');c.innerHTML='';
  NS.forEach((s,i)=>{
    const d=document.createElement('div');d.className='sti';
    const inner=s.type==='message'?`<input class="inp" style="margin-top:4px" value="${s.content.replace(/"/g,'&quot;')}" oninput="NS[${i}].content=this.value" placeholder="Texto del mensaje...">`:s.type==='ai'?`<div style="margin-top:4px;font-size:13px;color:var(--el);font-weight:500">◉ Claude responderá automáticamente</div>`:`<input class="inp" style="margin-top:4px" value="${s.content.replace(/"/g,'&quot;')}" oninput="NS[${i}].content=this.value" placeholder="Texto...">`;
    d.innerHTML=`<div class="stb">${i+1}</div><div style="flex:1"><div class="stt">${s.type==='ai'?'IA':s.type}</div>${inner}</div><button class="std" onclick="NS.splice(${i},1);rNS()">×</button>`;
    c.appendChild(d);
  });
}
async function saveFlow(){
  const n=document.getElementById('nfn').value.trim();
  const t=document.getElementById('nft').value.trim().toLowerCase();
  if(!n||!t||!NS.length){toast('⚠ Completá todos los campos');return;}
  await fetch('/api/flows',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:n,trigger:t,steps:[...NS]})});
  NS=[];rNS();document.getElementById('nfn').value='';document.getElementById('nft').value='';
  loadFlows();toast('✓ Flujo guardado');
}

async function saveAI(){
  const system=document.getElementById('ais').value;
  await fetch('/api/ai-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({system})});
  toast('✓ Config guardada');
}
async function testAI(){
  const t=document.getElementById('aiti').value.trim();if(!t){toast('⚠ Escribí algo');return;}
  const el=document.getElementById('aitr');el.style.display='block';el.textContent='◉ Pensando...';
  const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({messages:[{role:'user',content:t}],temperature:parseFloat(document.getElementById('ait').value)})});
  const d=await r.json();
  el.innerHTML='<span style="font-size:10px;font-weight:700;color:var(--el);text-transform:uppercase;letter-spacing:.5px">◉ RESPUESTA IA</span><br><br>'+d.reply;
}

function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600);}

// Init
loadStats();
fetch('/api/account').then(r=>r.json()).then(d=>{
  if(d.connected)document.getElementById('sb-name').textContent='✅ '+d.page_name;
}).catch(()=>{});
</script></body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Crear admin por defecto
    USERS[ADMIN_EMAIL] = {"name":"Admin","password":hash_pass(ADMIN_PASS),"plan":"admin","created":datetime.now().strftime("%d/%m/%Y")}
    app.run(host="0.0.0.0", port=port, debug=False)
