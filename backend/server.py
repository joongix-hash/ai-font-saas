from flask import Flask, request, render_template, send_from_directory, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from authlib.integrations.flask_client import OAuth
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv
from datetime import datetime
import os, uuid, io, math, traceback, zipfile, shutil, time, json, requests as http_requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── Module engines ───────────────────────────────────────────────────────────
from modules.sprite_engine import generate_sprite_sheet
from modules.pixel_engine  import convert_to_pixel_art, get_palette_hex
from modules.ui_engine     import generate_9slice

load_dotenv()

app = Flask(__name__)

# Cloud Run terminates TLS and forwards HTTP internally.
# ProxyFix reads X-Forwarded-Proto so Flask/Authlib sees request.scheme == 'https'.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

app.secret_key = os.getenv("SECRET_KEY", "dev-secret-please-change-in-production")

# OAuth 콜백 시 세션 쿠키가 정상 전달되도록 설정
# RENDER(Render.com) 또는 PRODUCTION(Cloud Run 등) 환경변수로 프로덕션 감지
# Cloud Run automatically sets K_SERVICE; also support RENDER and PRODUCTION env vars
_IS_PRODUCTION = bool(os.getenv("RENDER", "") or os.getenv("PRODUCTION", "") or os.getenv("K_SERVICE", ""))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = _IS_PRODUCTION
app.config["SESSION_COOKIE_HTTPONLY"] = True

# ─── HTTP → HTTPS 강제 리다이렉트 (프로덕션 전용) ──────────────────────────────
@app.before_request
def redirect_to_https():
    """Redirect HTTP requests to HTTPS in production.
    Render terminates TLS and sets X-Forwarded-Proto; ProxyFix exposes it as request.scheme.
    This ensures Google indexes only the canonical https:// URL."""
    if _IS_PRODUCTION and request.scheme == "http":
        url = request.url.replace("http://", "https://", 1)
        return redirect(url, code=301)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 클라우드 환경에서는 /tmp 를 사용 (ephemeral), 로컬은 outputs/ 사용
_CLOUD = os.getenv("RENDER", "") or os.getenv("PRODUCTION", "")
OUTPUT_FOLDER = "/tmp/copyfont_outputs" if _CLOUD else os.path.join(BASE_DIR, "outputs")
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ─── Database ────────────────────────────────────────────────────────────────
_db_url = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'copyfont.db')}")
# SQLAlchemy requires postgresql:// not postgres://
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# PostgreSQL용 연결 옵션 (Neon 서버리스 + Cloud Run 대응)
# NullPool: 요청마다 새 연결 생성 — 서버리스 환경에서 끊긴 연결 재사용 방지
if _db_url.startswith("postgresql"):
    from sqlalchemy.pool import NullPool
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "poolclass": NullPool,       # 커넥션 풀 비활성화 (Neon 슬립/Cloud Run 스케일-아웃 대응)
        "connect_args": {
            "connect_timeout": 30,   # Neon 슬립 해제 대기 최대 30초
            "sslmode": "require",
        },
    }

db = SQLAlchemy(app)

# ─── PayPal ──────────────────────────────────────────────────────────────────
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET    = os.getenv("PAYPAL_SECRET", "")
PAYPAL_MODE      = os.getenv("PAYPAL_MODE", "live")  # "live" or "sandbox"
PAYPAL_API_BASE  = "https://api-m.paypal.com" if PAYPAL_MODE == "live" else "https://api-m.sandbox.paypal.com"

def paypal_get_access_token():
    resp = http_requests.post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

def paypal_create_order(pkg, user_id, package_key, return_url, cancel_url):
    token = paypal_get_access_token()
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {
                "currency_code": "USD",
                "value": f"{pkg['price_cents'] / 100:.2f}",
            },
            "description": f"CopyPxl {pkg['name']} Pack – {pkg['desc']}",
            "custom_id": f"{user_id}:{package_key}:{pkg['credits']}",
        }],
        "application_context": {
            "return_url": return_url,
            "cancel_url": cancel_url,
            "brand_name": "CopyPxl",
            "user_action": "PAY_NOW",
        },
    }
    resp = http_requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

def paypal_capture_order(order_id):
    token = paypal_get_access_token()
    resp = http_requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

# ─── Google OAuth ─────────────────────────────────────────────────────────────
oauth = OAuth(app)
google_oauth = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ─── Constants ────────────────────────────────────────────────────────────────
FREE_CREDITS = 10
CREDITS_PER_GENERATION = 1   # font (AI)

# Credit costs per tool
TOOL_CREDITS = {
    "font":   1,   # AI bitmap font (per generation)
    "sprite": 2,   # sprite sheet packing
    "pixel":  1,   # pixel art conversion
    "ui":     1,   # 9-slice generator
}
PRIMARY_MODEL = "models/gemini-3.1-flash-image-preview"

CREDIT_PACKAGES = {
    "entry":    {"name": "Entry",    "price_cents": 499,  "credits": 150,  "price_str": "$4.99",  "desc": "150 Credits",  "per": "$0.033/gen", "tag": ""},
    "standard": {"name": "Standard", "price_cents": 999,  "credits": 400,  "price_str": "$9.99",  "desc": "400 Credits",  "per": "$0.025/gen", "tag": "POPULAR"},
    "pro":      {"name": "Pro",      "price_cents": 1999, "credits": 1000, "price_str": "$19.99", "desc": "1,000 Credits", "per": "$0.020/gen", "tag": ""},
    "ultra":    {"name": "Ultra",    "price_cents": 4999, "credits": 3000, "price_str": "$49.99", "desc": "3,000 Credits", "per": "$0.016/gen", "tag": "BEST VALUE"},
}

MODE_CONFIGS = {
    "numbers": {
        "label": "숫자 (5x4) - 1280x1024",
        "charset": "0123456789+-×÷%.,KMB",
        "cols": 5, "rows": 4, "w": 1280, "h": 1024,
        "instruction": (
            "STRICT 5-COLUMN GRID. COORDINATE MAP:\n"
            "Row1:[0],[1],[2],[3],[4]\n"
            "Row2:[5],[6],[7],[8],[9]\n"
            "Row3:[+],[-],[×],[÷],[%]\n"
            "Row4:[.],[,,],[K],[M],[B]"
        )
    },
    "letters": {
        "label": "영어 대소문자 (8x7) - 2048x1792",
        "charset": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        "cols": 8, "rows": 7, "w": 2048, "h": 1792,
        "instruction": (
            "STRICT 8-COLUMN GRID. COORDINATE MAP:\n"
            "Row1:[A],[B],[C],[D],[E],[F],[G],[H]\n"
            "Row2:[I],[J],[K],[L],[M],[N],[O],[P]\n"
            "Row3:[Q],[R],[S],[T],[U],[V],[W],[X]\n"
            "Row4:[Y],[Z],[a],[b],[c],[d],[e],[f]\n"
            "Row5:[g],[h],[i],[j],[k],[l],[m],[n]\n"
            "Row6:[o],[p],[q],[r],[s],[t],[u],[v]\n"
            "Row7:[w],[x],[y],[z]\n"
            "CRITICAL: EXACTLY 8 cells per row. Do not merge cells."
        )
    },
    "alnum": {
        "label": "전체 통합 (8x10) - 2048x2560",
        "charset": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-×÷%.,KMB",
        "cols": 8, "rows": 10, "w": 2048, "h": 2560,
        "instruction": (
            "STRICT 8-COLUMN GRID. COORDINATE MAP:\n"
            "Row1-3:A-X(8 per row), Row4:[Y],[Z],[a],[b],[c],[d],[e],[f]\n"
            "Row5-6:g-v(8 per row), Row7:[w],[x],[y],[z],[0],[1],[2],[3]\n"
            "Row8:[4],[5],[6],[7],[8],[9],[+],[-]\n"
            "Row9:[×],[÷],[%],[.],[,,],[K],[M],[B]\n"
            "Row10:EMPTY. DO NOT SKIP ANY SYMBOL."
        )
    },
    "custom": {"label": "커스텀 모드", "charset": "", "cols": 8, "rows": 0, "w": 2048, "h": 0}
}

# ─── DB Models ────────────────────────────────────────────────────────────────
class User(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    google_id  = db.Column(db.String(200), unique=True, nullable=False)
    email      = db.Column(db.String(200))
    name       = db.Column(db.String(200))
    picture    = db.Column(db.String(500))
    credits    = db.Column(db.Integer, default=FREE_CREDITS)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Attribution columns (filled at signup time from session.utm_*)
    referrer       = db.Column(db.String(500))
    utm_source     = db.Column(db.String(100))
    utm_medium     = db.Column(db.String(50))
    utm_campaign   = db.Column(db.String(100))
    utm_content    = db.Column(db.String(100))
    utm_term       = db.Column(db.String(100))

class Feedback(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    message    = db.Column(db.Text, nullable=False)
    rating     = db.Column(db.Integer)
    email      = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Payment(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    paypal_order_id = db.Column(db.String(100), unique=True, nullable=False)
    package_key     = db.Column(db.String(50))
    credits         = db.Column(db.Integer)
    amount_cents    = db.Column(db.Integer)   # USD cents
    currency        = db.Column(db.String(10), default="USD")
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

class Coupon(db.Model):
    """Free credit seed/campaign coupon. For influencer collab SEED-XXXX codes."""
    id           = db.Column(db.Integer, primary_key=True)
    code         = db.Column(db.String(50), unique=True, nullable=False, index=True)
    credits      = db.Column(db.Integer, nullable=False)
    max_uses     = db.Column(db.Integer)
    used_count   = db.Column(db.Integer, default=0)
    note         = db.Column(db.String(200))
    expires_at   = db.Column(db.DateTime, nullable=True)
    is_active    = db.Column(db.Boolean, default=True)
    created_by   = db.Column(db.String(200))
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

class CouponRedemption(db.Model):
    """Coupon usage log. One user can redeem the same coupon only once."""
    id           = db.Column(db.Integer, primary_key=True)
    coupon_id    = db.Column(db.Integer, db.ForeignKey("coupon.id"), nullable=False, index=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    credits      = db.Column(db.Integer, nullable=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("coupon_id", "user_id", name="uniq_coupon_user"),)

# ─── Admin access control ─────────────────────────────────────────────────────
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "joongix@gmail.com").split(",") if e.strip()}

def is_admin(user):
    return bool(user and user.email and user.email.lower() in ADMIN_EMAILS)

# DB 테이블 생성 — 빠른 실패 & graceful 처리
# DB가 없어도 서버는 정상 시작됨 (로그인/크레딧 기능만 비활성화)
_db_available = False

def _init_db(max_retries=2, delay=2):
    global _db_available
    for attempt in range(1, max_retries + 1):
        try:
            with app.app_context():
                db.create_all()
                # Idempotent migrations — safe on re-run
                _migrate_user_attribution()
            print(f"[DB] Tables ready (attempt {attempt})")
            _db_available = True
            return
        except Exception as e:
            print(f"[DB] Connection failed (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(delay)
    print("[DB] WARNING: Could not initialize DB. App starting in limited mode (login disabled).")

def _migrate_user_attribution():
    """Add referrer + utm_* columns to user table if missing.
    Postgres uses IF NOT EXISTS; safe to run on every boot."""
    from sqlalchemy import text as sql_text
    cols = [
        ("referrer",      "VARCHAR(500)"),
        ("utm_source",    "VARCHAR(100)"),
        ("utm_medium",    "VARCHAR(50)"),
        ("utm_campaign",  "VARCHAR(100)"),
        ("utm_content",   "VARCHAR(100)"),
        ("utm_term",      "VARCHAR(100)"),
    ]
    is_postgres = str(db.engine.url).startswith("postgresql")
    try:
        with db.engine.begin() as conn:
            if is_postgres:
                for col, ddl in cols:
                    conn.execute(sql_text(f'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS {col} {ddl}'))
            else:
                # SQLite — check columns and add manually
                rows = conn.execute(sql_text("PRAGMA table_info(user)")).fetchall()
                existing = {r[1] for r in rows}
                for col, ddl in cols:
                    if col not in existing:
                        conn.execute(sql_text(f"ALTER TABLE user ADD COLUMN {col} {ddl}"))
        print("[DB] Migration: user attribution columns ensured")
    except Exception as e:
        print(f"[DB] Migration warning (non-fatal): {e}")

_init_db()

# ─── Helpers ──────────────────────────────────────────────────────────────────
def current_user():
    if not _db_available:
        return None
    if "user_id" not in session:
        return None
    try:
        return db.session.get(User, session["user_id"])
    except Exception:
        return None

def safe_rmtree(path, retries=5, delay=0.3):
    for i in range(retries):
        try:
            if os.path.exists(path):
                shutil.rmtree(path)
            return
        except PermissionError:
            if i == retries - 1:
                raise
            time.sleep(delay)

def determine_best_bg_color(img):
    img_small = img.resize((50, 50)).convert("RGB")
    pixels = list(img_small.getdata())
    avg_r = sum(p[0] for p in pixels) // len(pixels)
    avg_g = sum(p[1] for p in pixels) // len(pixels)
    avg_b = sum(p[2] for p in pixels) // len(pixels)
    if avg_g > avg_r and avg_g > avg_b:
        return (255, 0, 255), "#FF00FF", "MAGENTA"
    return (0, 255, 0), "#00FF00", "LIME GREEN"

def remove_background_smart(img_rgba, target_rgb, tolerance):
    img_rgba = img_rgba.convert("RGBA")
    pix = img_rgba.load()
    w, h = img_rgba.size
    tr, tg, tb = target_rgb
    for y in range(h):
        for x in range(w):
            r, g, b, a = pix[x, y]
            dist = math.sqrt((r - tr) ** 2 + (g - tg) ** 2 + (b - tb) ** 2)
            is_bg = (g > r + 15 and g > b + 15) if target_rgb == (0, 255, 0) else (r > g + 15 and b > g + 15)
            if dist < (tolerance / 100) * 441 and is_bg:
                pix[x, y] = (0, 0, 0, 0)
    return img_rgba

def generate_fnt_content(font_name, charset, cols, rows, img_w, img_h):
    cell_w, cell_h = img_w // cols, img_h // rows
    lines = [
        f'info face="{font_name}" size={cell_h} bold=0 italic=0 charset="" unicode=1 stretchH=100 smooth=1 aa=1 padding=0,0,0,0 spacing=1,1',
        f"common lineHeight={cell_h} base={cell_h} scaleW={img_w} scaleH={img_h} pages=1 packed=0",
        f'page id=0 file="font.png"',
        f"chars count={len(charset)}"
    ]
    for i, char in enumerate(charset):
        if i >= (cols * rows):
            break
        col, row = i % cols, i // cols
        x, y = col * cell_w, row * cell_h
        lines.append(f"char id={ord(char)} x={x} y={y} width={cell_w} height={cell_h} xoffset=0 yoffset=0 xadvance={cell_w} page=0 chnl=15")
    return "\n".join(lines)

def generate_sheet_once(client, reference_path, mode_key, bg_name, bg_hex, custom_data=None):
    with Image.open(reference_path) as _ref:
        ref_img = _ref.copy()

    if mode_key == "custom" and custom_data.get("path"):
        with Image.open(custom_data["path"]) as _sheet:
            user_sheet = _sheet.copy()
        target_w, target_h = user_sheet.size
        cols, rows = custom_data["cols"], custom_data["rows"]
        charset = custom_data["charset"]
        prompt = f"Transfer reference style to this sheet. Keep exact layout. Background: {bg_hex}."
        contents = [ref_img, user_sheet, prompt]
    elif mode_key == "custom":
        charset = custom_data["charset"]
        cols = 8
        rows = math.ceil(len(charset) / cols) if len(charset) > 0 else 1
        target_w, target_h = 2048, rows * 256
        prompt = f"Generate 8-column font sheet (256x256 cells) for: {charset}. Background: {bg_hex}."
        contents = [ref_img, prompt]
    else:
        cfg = MODE_CONFIGS[mode_key]
        charset, cols, rows = cfg["charset"], cfg["cols"], cfg["rows"]
        target_w, target_h = cfg["w"], cfg["h"]
        prompt = f"Generate {cols}x{rows} font sheet. Content: {cfg['instruction']}. Style: Match reference. Background: {bg_hex}."
        contents = [ref_img, prompt]

    res = client.models.generate_content(
        model=PRIMARY_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(response_modalities=["IMAGE"])
    )
    raw_img = Image.open(io.BytesIO(res.candidates[0].content.parts[0].inline_data.data)).convert("RGBA")
    return raw_img.resize((target_w, target_h), Image.LANCZOS), charset, cols, rows

def render(template="index.html", **kwargs):
    """공통 템플릿 변수를 자동으로 포함해서 render_template 호출"""
    kwargs.setdefault("mode_configs", MODE_CONFIGS)
    kwargs.setdefault("user", current_user())
    kwargs.setdefault("credit_packages", CREDIT_PACKAGES)
    kwargs.setdefault("paypal_client_id", PAYPAL_CLIENT_ID)
    return render_template(template, **kwargs)

# ─── Auth Routes ──────────────────────────────────────────────────────────────
@app.route("/auth/google")
def auth_google():
    """Manual OAuth redirect — bypasses Authlib authorize_redirect which rewrites
    the scheme to http:// even when given an explicit https:// URI on Cloud Run."""
    if not os.getenv("GOOGLE_CLIENT_ID"):
        return render(error_message="Google OAuth not configured.")
    import secrets, urllib.parse
    session.permanent = True
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    app.logger.info("[OAuth] /auth/google state=%s", state[:8])
    redirect_uri = "https://copypxl.com/auth/callback" if _IS_PRODUCTION else url_for("auth_callback", _external=True)
    params = {
        "response_type": "code",
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return redirect(auth_url)

@app.route("/auth/callback")
def auth_callback():
    """Manual token exchange. Fix 2026-05-07: state mismatch warns but continues
    (SameSite=Lax + cross-site cookie issue); token exchange with client_secret
    provides CSRF-equivalent protection."""
    import urllib.parse
    try:
        returned_state = request.args.get("state", "")
        stored_state = session.pop("oauth_state", None)
        if not stored_state or returned_state != stored_state:
            app.logger.warning("[OAuth] state mismatch (continuing): returned=%s stored=%s",
                               (returned_state or "")[:12], (stored_state or "(none)")[:12])

        code = request.args.get("code")
        if not code:
            error = request.args.get("error", "no_code")
            app.logger.error("[OAuth] callback missing code, error=%s", error)
            return render(error_message=("Login was cancelled or failed (" + error + "). Please try again."))

        redirect_uri = "https://copypxl.com/auth/callback" if _IS_PRODUCTION else url_for("auth_callback", _external=True)

        # Exchange code for tokens
        token_resp = http_requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            err = token_data.get("error", "unknown")
            err_desc = token_data.get("error_description", "")
            app.logger.error("[OAuth] token exchange failed: %s - %s", err, err_desc)
            return render(error_message=("Google authentication server communication failed: " + str(err) + ". Please try again."))

        # Get user info
        userinfo_resp = http_requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        info = userinfo_resp.json()

        user = User.query.filter_by(google_id=info.get("sub")).first()
        if not user:
            user = User(
                google_id=info.get("sub"),
                email=info.get("email"),
                name=info.get("name"),
                picture=info.get("picture"),
                credits=FREE_CREDITS,
                # Attribution: 세션에 저장된 first-touch utm/referrer를 영구 보존
                referrer=session.get("referrer"),
                utm_source=session.get("utm_source"),
                utm_medium=session.get("utm_medium"),
                utm_campaign=session.get("utm_campaign"),
                utm_content=session.get("utm_content"),
                utm_term=session.get("utm_term"),
            )
            db.session.add(user)
            db.session.commit()
            # 한 번 사용한 attribution은 세션에서 제거 (이후 다른 유저와 섞이지 않게)
            for _k in ("referrer", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"):
                session.pop(_k, None)
        session["user_id"] = user.id
        session.permanent = True
        app.logger.info("[OAuth] sign-in success: user_id=%s email=%s", user.id, user.email)
    except Exception as exc:
        app.logger.exception("[OAuth] callback error")
        traceback.print_exc()
        return render(error_message=("Login processing error: " + str(exc)[:120]))
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ─── Payment Routes (PayPal) ──────────────────────────────────────────────────
@app.route("/checkout/<package_key>", methods=["POST"])
def checkout(package_key):
    user = current_user()
    if not user:
        return render(error_message="결제를 위해 먼저 로그인해주세요.")
    if not PAYPAL_CLIENT_ID or not PAYPAL_SECRET:
        return render(error_message="결제 시스템이 아직 준비 중입니다.")
    pkg = CREDIT_PACKAGES.get(package_key)
    if not pkg:
        return "Invalid package", 400
    try:
        return_url = url_for("payment_success", _external=True)
        cancel_url = url_for("index", _external=True)
        order = paypal_create_order(pkg, user.id, package_key, return_url, cancel_url)
        # 승인 URL로 리다이렉트
        approve_url = next(
            link["href"] for link in order["links"] if link["rel"] == "approve"
        )
        return redirect(approve_url)
    except Exception as e:
        traceback.print_exc()
        return render(error_message=f"결제 오류: {str(e)}")

@app.route("/payment/success")
def payment_success():
    order_id = request.args.get("token")  # PayPal returns ?token=ORDER_ID
    if not order_id:
        return render(error_message="결제 정보를 찾을 수 없습니다.")
    try:
        capture = paypal_capture_order(order_id)
        status = capture.get("status")
        if status != "COMPLETED":
            return render(error_message=f"결제가 완료되지 않았습니다 (status: {status})")

        # custom_id 파싱: "user_id:package_key:credits"
        custom_id = (
            capture.get("purchase_units", [{}])[0]
            .get("payments", {})
            .get("captures", [{}])[0]
            .get("custom_id", "")
        )
        parts = custom_id.split(":")
        if len(parts) >= 3:
            user_id_str, package_key, credits_str = parts[0], parts[1], parts[2]
            user = db.session.get(User, int(user_id_str))
            credits = int(credits_str)
            if user and credits > 0:
                user.credits += credits
                # 결제 기록 저장 (어드민 통계용) — 중복 INSERT 방지
                try:
                    pkg_info = CREDIT_PACKAGES.get(package_key, {})
                    existing = db.session.query(Payment).filter_by(paypal_order_id=order_id).first()
                    if not existing:
                        payment = Payment(
                            user_id=user.id,
                            paypal_order_id=order_id,
                            package_key=package_key,
                            credits=credits,
                            amount_cents=pkg_info.get("price_cents", 0),
                            currency="USD",
                        )
                        db.session.add(payment)
                except Exception as _pe:
                    print(f"[PAYPAL] Payment record insert failed (non-fatal): {_pe}")
                db.session.commit()
                print(f"[PAYPAL] 크레딧 추가: user={user.email} +{credits} → {user.credits}")
                return render(success_message=f"✅ 결제 완료! {credits} 크레딧이 추가되었습니다. (현재 잔여: {user.credits})")
        return render(success_message="✅ 결제가 완료되었습니다! 크레딧이 추가됩니다.")
    except Exception as e:
        traceback.print_exc()
        return render(error_message=f"결제 확인 오류: {str(e)}")

# ─── Email Helper ─────────────────────────────────────────────────────────────
def send_feedback_email(message, rating, sender_email, user_name):
    """피드백을 이메일로 전송 (Gmail SMTP)"""
    smtp_email    = os.getenv("SMTP_EMAIL", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    to_email      = os.getenv("FEEDBACK_TO_EMAIL", "joongix@gmail.com")

    if not smtp_email or not smtp_password:
        app.logger.warning("SMTP 설정 없음 — 이메일 전송 생략")
        return

    try:
        stars = "⭐" * (rating or 0)
        html_body = f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:auto;">
  <h2 style="color:#4F46E5;">📬 CopyPxl 새 피드백</h2>
  <table style="width:100%;border-collapse:collapse;">
    <tr><td style="padding:8px;font-weight:bold;color:#6B7280;">평점</td>
        <td style="padding:8px;">{stars or "없음"} ({rating or "-"}점)</td></tr>
    <tr style="background:#F9FAFB;"><td style="padding:8px;font-weight:bold;color:#6B7280;">보낸 사람</td>
        <td style="padding:8px;">{user_name or "비로그인"} {f"({sender_email})" if sender_email else ""}</td></tr>
    <tr><td style="padding:8px;font-weight:bold;color:#6B7280;">내용</td>
        <td style="padding:8px;white-space:pre-wrap;">{message}</td></tr>
    <tr style="background:#F9FAFB;"><td style="padding:8px;font-weight:bold;color:#6B7280;">시각</td>
        <td style="padding:8px;">{datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}</td></tr>
  </table>
</body></html>
"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[CopyPxl] 새 피드백 {stars}"
        msg["From"]    = smtp_email
        msg["To"]      = to_email
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, to_email, msg.as_string())

        app.logger.info("피드백 이메일 전송 완료 → %s", to_email)
    except Exception:
        app.logger.error("피드백 이메일 전송 실패:\n%s", traceback.format_exc())


# ─── Feedback Route ───────────────────────────────────────────────────────────
@app.route("/feedback", methods=["POST"])
def feedback():
    user = current_user()
    message = request.form.get("message", "").strip()
    rating  = request.form.get("rating", type=int)
    email   = request.form.get("email", "").strip()
    if message:
        fb = Feedback(
            user_id=user.id if user else None,
            message=message,
            rating=rating,
            email=email,
        )
        db.session.add(fb)
        db.session.commit()

        # 이메일 알림 전송
        user_name = user.name if user else None
        sender_email = email or (user.email if user else "")
        send_feedback_email(message, rating, sender_email, user_name)

        return render(success_message="💬 피드백을 보내주셔서 감사합니다!")
    return redirect(url_for("index"))

# ─── SEO Routes ───────────────────────────────────────────────────────────────
@app.route("/robots.txt")
def robots_txt():
    from flask import Response
    content = """User-agent: *
Allow: /
Disallow: /outputs/
Disallow: /api/
Disallow: /auth/
Disallow: /checkout/
Disallow: /payment/
Disallow: /paypal/

Sitemap: https://copypxl.com/sitemap.xml
"""
    return Response(content, mimetype="text/plain")

@app.route("/sitemap.xml")
def sitemap_xml():
    from flask import Response
    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%m-%d")
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://copypxl.com/</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
"""
    return Response(content, mimetype="application/xml")

# ─── Main Routes ──────────────────────────────────────────────────────────────
_UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")

def _capture_attribution_to_session():
    """방문 시 URL의 utm_*과 referrer를 세션에 저장 (가입까진 first-touch로 보존)."""
    for k in _UTM_KEYS:
        v = (request.args.get(k) or "").strip()
        if v and k not in session:
            session[k] = v[:100]
    if "referrer" not in session and request.referrer:
        try:
            session["referrer"] = request.referrer[:500]
        except Exception:
            pass

@app.route("/")
def index():
    _capture_attribution_to_session()
    return render()

@app.route("/convert", methods=["POST"])
def convert():
    user = current_user()

    # 로그인 체크
    if not user:
        return render(error_message="🔒 생성하려면 Google 로그인이 필요합니다. 신규 가입 시 10회 무료!")

    # 크레딧 체크
    if user.credits < CREDITS_PER_GENERATION:
        return render(error_message=f"크레딧이 부족합니다 (현재 {user.credits}개). 충전 후 이용해주세요.")

    job_id   = uuid.uuid4().hex[:12]
    job_path = os.path.join(OUTPUT_FOLDER, job_id)
    os.makedirs(job_path, exist_ok=True)

    try:
        ref_file = request.files.get("image")
        if not ref_file or ref_file.filename == "":
            raise ValueError("레퍼런스 이미지가 없습니다.")

        mode  = request.form.get("mode")
        tol   = int(request.form.get("tolerance", 45))
        f_name = request.form.get("font_name", "BitmapFont")

        ref_p = os.path.join(job_path, "ref.png")
        ref_file.save(ref_p)

        with Image.open(ref_p) as _tmp:
            target_rgb, target_hex, target_name = determine_best_bg_color(_tmp.copy())

        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

        custom_data = None
        if mode == "custom":
            c_sheet = request.files.get("custom_sheet")
            c_p = None
            if c_sheet and c_sheet.filename != "":
                c_p = os.path.join(job_path, "user_sheet.png")
                c_sheet.save(c_p)
            custom_data = {
                "path": c_p,
                "charset": request.form.get("custom_charset", ""),
                "cols": int(request.form.get("custom_cols", 8)),
                "rows": int(request.form.get("custom_rows", 1)),
            }

        sheet, charset, cols, rows = generate_sheet_once(
            client, ref_p, mode, target_name, target_hex, custom_data
        )

        font_p   = os.path.join(job_path, "font.png")
        fnt_p    = os.path.join(job_path, "font.fnt")
        zip_name = f"font_{job_id}.zip"
        zip_p    = os.path.join(job_path, zip_name)
        sheet_p  = os.path.join(job_path, "sheet.png")

        remove_background_smart(sheet, target_rgb, tol).save(font_p)

        fnt_text = generate_fnt_content(f_name, charset, cols, rows, sheet.width, sheet.height)
        with open(fnt_p, "w", encoding="utf-8") as f:
            f.write(fnt_text)

        with zipfile.ZipFile(zip_p, "w") as z:
            z.write(font_p, arcname="font.png")
            z.write(fnt_p, arcname="font.fnt")

        sheet.save(sheet_p)

        # 크레딧 차감
        user.credits -= CREDITS_PER_GENERATION
        db.session.commit()
        print(f"[GENERATE] user={user.email} 크레딧 차감 → 잔여 {user.credits}개")

        return render(
            uploaded_image=f"{job_id}/ref.png",
            generated_sheet=f"{job_id}/sheet.png",
            converted_image=f"{job_id}/font.png",
            download_zip=f"{job_id}/{zip_name}",
            selected_mode=mode,
        )

    except Exception as e:
        traceback.print_exc()
        try:
            safe_rmtree(job_path)
        except Exception:
            pass
        err_str = str(e).lower()
        if "quota" in err_str or "resource_exhausted" in err_str or "429" in err_str or "rate" in err_str or "limit" in err_str:
            return render(error_message="⏳ AI 생성 한도에 도달했습니다. 잠시 후 다시 시도해주세요. (Gemini API 일일 한도 초과)")
        if "invalid_argument" in err_str or "api key" in err_str:
            return render(error_message="🔑 API 키 오류입니다. 서버 설정을 확인해주세요.")
        return render(error_message=f"⚠️ 생성 중 오류가 발생했습니다: {str(e)}")

@app.route("/outputs/<path:filename>")
def output_file(filename):
    return send_from_directory(OUTPUT_FOLDER, filename)


# ─── Sprite Sheet API ─────────────────────────────────────────────────────────
@app.route("/api/sprite/generate", methods=["POST"])
def api_sprite_generate():
    user = current_user()
    if not user:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    cost = TOOL_CREDITS["sprite"]
    if user.credits < cost:
        return jsonify({"error": f"크레딧이 부족합니다 (필요: {cost}, 현재: {user.credits})"}), 402

    job_id   = uuid.uuid4().hex[:12]
    job_path = os.path.join(OUTPUT_FOLDER, job_id)
    os.makedirs(job_path, exist_ok=True)

    try:
        files = request.files.getlist("images")
        if not files or all(f.filename == "" for f in files):
            return jsonify({"error": "이미지를 최소 1개 이상 업로드해주세요."}), 400

        pack_mode   = request.form.get("pack_mode", "tight")
        padding     = int(request.form.get("padding", 2))
        cell_w      = int(request.form.get("cell_w", 0))
        cell_h      = int(request.form.get("cell_h", 0))
        sort_by     = request.form.get("sort_by", "area")
        pivot_x     = float(request.form.get("pivot_x", 0.5))
        pivot_y     = float(request.form.get("pivot_y", 0.5))

        saved_paths = []
        for f in files:
            if f.filename == "":
                continue
            ext = os.path.splitext(f.filename)[1].lower() or ".png"
            p = os.path.join(job_path, f"src_{uuid.uuid4().hex[:6]}{ext}")
            f.save(p)
            saved_paths.append(p)

        sheet, atlas = generate_sprite_sheet(
            saved_paths,
            pack_mode=pack_mode,
            padding=padding,
            cell_w=cell_w,
            cell_h=cell_h,
            sort_by=sort_by,
            pivot_x=pivot_x,
            pivot_y=pivot_y,
        )

        sheet_p   = os.path.join(job_path, "sheet.png")
        atlas_p   = os.path.join(job_path, "atlas.json")
        zip_name  = f"sprite_{job_id}.zip"
        zip_p     = os.path.join(job_path, zip_name)

        sheet.save(sheet_p)
        with open(atlas_p, "w", encoding="utf-8") as f:
            json.dump(atlas, f, ensure_ascii=False, indent=2)

        with zipfile.ZipFile(zip_p, "w") as z:
            z.write(sheet_p, arcname="sheet.png")
            z.write(atlas_p, arcname="atlas.json")

        user.credits -= cost
        db.session.commit()
        print(f"[SPRITE] user={user.email} -{cost} → {user.credits}")

        return jsonify({
            "job_id":       job_id,
            "sheet_url":    f"/outputs/{job_id}/sheet.png",
            "atlas_url":    f"/outputs/{job_id}/atlas.json",
            "download_zip": f"/outputs/{job_id}/{zip_name}",
            "frames":       len(atlas["frames"]),
            "size":         atlas["meta"]["size"],
            "credits_left": user.credits,
        })

    except Exception as e:
        traceback.print_exc()
        try:
            safe_rmtree(job_path)
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500


# ─── Pixel Art API ────────────────────────────────────────────────────────────
@app.route("/api/pixel/convert", methods=["POST"])
def api_pixel_convert():
    user = current_user()
    if not user:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    cost = TOOL_CREDITS["pixel"]
    if user.credits < cost:
        return jsonify({"error": f"크레딧이 부족합니다 (필요: {cost}, 현재: {user.credits})"}), 402

    job_id   = uuid.uuid4().hex[:12]
    job_path = os.path.join(OUTPUT_FOLDER, job_id)
    os.makedirs(job_path, exist_ok=True)

    try:
        img_file = request.files.get("image")
        if not img_file or img_file.filename == "":
            return jsonify({"error": "이미지를 업로드해주세요."}), 400

        pixel_size   = int(request.form.get("pixel_size", 8))
        palette_size = int(request.form.get("palette_size", 16))
        output_scale = int(request.form.get("output_scale", 4))
        dither       = request.form.get("dither", "false").lower() == "true"
        outline      = request.form.get("outline", "false").lower() == "true"

        src_p = os.path.join(job_path, "src.png")
        img_file.save(src_p)

        result = convert_to_pixel_art(
            src_p,
            pixel_size=pixel_size,
            palette_size=palette_size,
            output_scale=output_scale,
            dither=dither,
            outline=outline,
        )

        out_p    = os.path.join(job_path, "pixel.png")
        zip_name = f"pixel_{job_id}.zip"
        zip_p    = os.path.join(job_path, zip_name)

        result.save(out_p)

        palette = get_palette_hex(result, max_colors=palette_size)

        with zipfile.ZipFile(zip_p, "w") as z:
            z.write(out_p, arcname="pixel.png")

        user.credits -= cost
        db.session.commit()
        print(f"[PIXEL] user={user.email} -{cost} → {user.credits}")

        return jsonify({
            "job_id":       job_id,
            "output_url":   f"/outputs/{job_id}/pixel.png",
            "download_zip": f"/outputs/{job_id}/{zip_name}",
            "size":         {"w": result.width, "h": result.height},
            "palette":      palette,
            "credits_left": user.credits,
        })

    except Exception as e:
        traceback.print_exc()
        try:
            safe_rmtree(job_path)
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500


# ─── 9-Slice UI API ───────────────────────────────────────────────────────────
@app.route("/api/ui/9slice", methods=["POST"])
def api_ui_9slice():
    user = current_user()
    if not user:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    cost = TOOL_CREDITS["ui"]
    if user.credits < cost:
        return jsonify({"error": f"크레딧이 부족합니다 (필요: {cost}, 현재: {user.credits})"}), 402

    job_id   = uuid.uuid4().hex[:12]
    job_path = os.path.join(OUTPUT_FOLDER, job_id)
    os.makedirs(job_path, exist_ok=True)

    try:
        img_file = request.files.get("image")
        if not img_file or img_file.filename == "":
            return jsonify({"error": "이미지를 업로드해주세요."}), 400

        auto_detect = request.form.get("auto_detect", "true").lower() == "true"
        left   = request.form.get("left",   type=int)
        right  = request.form.get("right",  type=int)
        top    = request.form.get("top",    type=int)
        bottom = request.form.get("bottom", type=int)

        src_p = os.path.join(job_path, "src.png")
        img_file.save(src_p)

        preview, original, metadata = generate_9slice(
            src_p,
            auto_detect=auto_detect,
            left=left,
            right=right,
            top=top,
            bottom=bottom,
        )

        preview_p  = os.path.join(job_path, "preview.png")
        meta_p     = os.path.join(job_path, "metadata.json")
        zip_name   = f"9slice_{job_id}.zip"
        zip_p      = os.path.join(job_path, zip_name)

        preview.save(preview_p)
        with open(meta_p, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        with zipfile.ZipFile(zip_p, "w") as z:
            z.write(src_p, arcname="source.png")
            z.write(preview_p, arcname="preview.png")
            z.write(meta_p, arcname="metadata.json")

        user.credits -= cost
        db.session.commit()
        print(f"[9SLICE] user={user.email} -{cost} → {user.credits}")

        slices = metadata["slice_lines"]
        return jsonify({
            "job_id":       job_id,
            "preview_url":  f"/outputs/{job_id}/preview.png",
            "source_url":   f"/outputs/{job_id}/src.png",
            "meta_url":     f"/outputs/{job_id}/metadata.json",
            "download_zip": f"/outputs/{job_id}/{zip_name}",
            "slice_lines":  slices,
            "source_size":  metadata["source_size"],
            "credits_left": user.credits,
        })

    except Exception as e:
        traceback.print_exc()
        try:
            safe_rmtree(job_path)
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500


# ─── Admin Dashboard ──────────────────────────────────────────────────────────

# ─── Influencer / Marketing short URLs ─────────────────────────────────────────
GO_LINKS_PATH = os.path.join(BASE_DIR, "go_links.json")

def _load_go_links():
    """go_links.json을 매 요청마다 새로 읽어 변경 즉시 반영. 작은 파일이라 비용 미미."""
    try:
        with open(GO_LINKS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k.lower(): v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

@app.route("/go/<slug>")
def go_redirect(slug):
    """Short URL → UTM이 박힌 풀 URL로 302 리다이렉트.
    인플루언서 협업 시 영상 설명란에 짧은 URL을 두기 위함."""
    from urllib.parse import urlencode, urlparse, urlunparse, parse_qs

    links = _load_go_links()
    entry = links.get(slug.lower())
    if not entry:
        # 알 수 없는 slug — 홈으로
        return redirect(url_for("index"), code=302)

    target = entry.get("url") or "https://copypxl.com/"
    utm_params = {k: v for k, v in entry.items() if k.startswith("utm_") and v}
    if utm_params:
        parts = urlparse(target)
        existing = parse_qs(parts.query)
        for k, v in utm_params.items():
            existing[k] = [v]  # entry의 값으로 덮어쓰기
        new_query = urlencode({k: v[0] for k, v in existing.items()})
        target = urlunparse(parts._replace(query=new_query))

    # GA4가 풀 URL의 utm을 자동 캡처하도록 풀 URL로 리다이렉트
    return redirect(target, code=302)

@app.route("/admin")
def admin_page():
    """어드민 대시보드 페이지 — joongix@gmail.com (또는 ADMIN_EMAILS) 만 접근 가능."""
    user = current_user()
    if not is_admin(user):
        return redirect(url_for("auth_google", next="/admin")) if not user else ("Forbidden", 403)
    return render_template("admin.html", user=user)


@app.route("/admin/stats")
def admin_stats():
    """대시보드용 JSON 통계.

    응답 구조:
      users:   { total, today, week, month }
      payments:{ total_count, today_count, total_revenue_usd, today_revenue_usd, week_revenue_usd, month_revenue_usd }
      conversion: { paid_users, rate_percent }
      daily_signups: [ {date, count}, ... 30 days ]
      daily_revenue: [ {date, amount_usd}, ... 30 days ]
    """
    user = current_user()
    if not is_admin(user):
        return jsonify({"error": "forbidden"}), 403

    if not _db_available:
        return jsonify({"error": "db_unavailable"}), 503

    from datetime import timedelta
    from sqlalchemy import func

    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    week_start  = today_start - timedelta(days=7)
    month_start = today_start - timedelta(days=30)

    # 가입자
    total_users = db.session.query(User).count()
    today_users = db.session.query(User).filter(User.created_at >= today_start).count()
    week_users  = db.session.query(User).filter(User.created_at >= week_start).count()
    month_users = db.session.query(User).filter(User.created_at >= month_start).count()

    # 결제 건수
    total_payments = db.session.query(Payment).count()
    today_payments = db.session.query(Payment).filter(Payment.created_at >= today_start).count()
    week_payments  = db.session.query(Payment).filter(Payment.created_at >= week_start).count()
    month_payments = db.session.query(Payment).filter(Payment.created_at >= month_start).count()

    # 매출 (cents 합)
    def _sum_cents(since=None):
        q = db.session.query(func.coalesce(func.sum(Payment.amount_cents), 0))
        if since is not None:
            q = q.filter(Payment.created_at >= since)
        return int(q.scalar() or 0)

    total_rev_c = _sum_cents()
    today_rev_c = _sum_cents(today_start)
    week_rev_c  = _sum_cents(week_start)
    month_rev_c = _sum_cents(month_start)

    # 결제 전환율
    paid_user_count = db.session.query(Payment.user_id).distinct().count()
    conv_rate = round((paid_user_count / total_users * 100), 2) if total_users > 0 else 0.0

    # 30일 일별 가입자
    daily_signups = []
    daily_revenue = []
    for i in range(30):
        day      = today_start - timedelta(days=29 - i)
        next_day = day + timedelta(days=1)
        sc = db.session.query(User).filter(User.created_at >= day, User.created_at < next_day).count()
        rc = (db.session.query(func.coalesce(func.sum(Payment.amount_cents), 0))
                .filter(Payment.created_at >= day, Payment.created_at < next_day).scalar() or 0)
        date_str = day.strftime("%Y-%m-%d")
        daily_signups.append({"date": date_str, "count": sc})
        daily_revenue.append({"date": date_str, "amount_usd": round(int(rc) / 100, 2)})

    return jsonify({
        "generated_at_utc": now.isoformat() + "Z",
        "users": {
            "total": total_users,
            "today": today_users,
            "week":  week_users,
            "month": month_users,
        },
        "payments": {
            "total_count":       total_payments,
            "today_count":       today_payments,
            "week_count":        week_payments,
            "month_count":       month_payments,
            "total_revenue_usd": round(total_rev_c / 100, 2),
            "today_revenue_usd": round(today_rev_c / 100, 2),
            "week_revenue_usd":  round(week_rev_c  / 100, 2),
            "month_revenue_usd": round(month_rev_c / 100, 2),
        },
        "conversion": {
            "paid_users":   paid_user_count,
            "rate_percent": conv_rate,
        },
        "daily_signups": daily_signups,
        "daily_revenue": daily_revenue,
    })

# === Coupons (influencer/marketing seed credits) ==============================
import re as _re_coupon

_COUPON_CODE_RE = _re_coupon.compile(r"^[A-Z0-9_-]{3,50}$")

@app.route("/api/coupon/redeem", methods=["POST"])
def coupon_redeem():
    """Logged-in user redeems a coupon code for credits."""
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "login_required"}), 401
    payload = request.get_json(silent=True) or request.form or {}
    code = (payload.get("code") or "").strip().upper()
    if not code:
        return jsonify({"ok": False, "error": "code_required"}), 400
    coupon = Coupon.query.filter_by(code=code, is_active=True).first()
    if not coupon:
        return jsonify({"ok": False, "error": "invalid_code"}), 404
    if coupon.expires_at and coupon.expires_at < datetime.utcnow():
        return jsonify({"ok": False, "error": "expired"}), 400
    if coupon.max_uses and (coupon.used_count or 0) >= coupon.max_uses:
        return jsonify({"ok": False, "error": "max_uses_reached"}), 400
    if CouponRedemption.query.filter_by(coupon_id=coupon.id, user_id=user.id).first():
        return jsonify({"ok": False, "error": "already_redeemed"}), 400
    user.credits = (user.credits or 0) + coupon.credits
    coupon.used_count = (coupon.used_count or 0) + 1
    db.session.add(CouponRedemption(coupon_id=coupon.id, user_id=user.id, credits=coupon.credits))
    db.session.commit()
    print(f"[COUPON] {user.email} redeemed {code} (+{coupon.credits} -> {user.credits})")
    return jsonify({"ok": True, "credits_added": coupon.credits, "new_balance": user.credits})

@app.route("/admin/coupons", methods=["GET"])
def admin_coupons_list():
    user = current_user()
    if not is_admin(user):
        return jsonify({"error": "forbidden"}), 403
    coupons = Coupon.query.order_by(Coupon.created_at.desc()).limit(500).all()
    return jsonify([{
        "id": c.id,
        "code": c.code,
        "credits": c.credits,
        "max_uses": c.max_uses,
        "used_count": c.used_count or 0,
        "note": c.note,
        "expires_at": c.expires_at.isoformat() if c.expires_at else None,
        "is_active": bool(c.is_active),
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    } for c in coupons])

@app.route("/admin/coupons/create", methods=["POST"])
def admin_coupons_create():
    user = current_user()
    if not is_admin(user):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or request.form or {}
    code = (data.get("code") or "").strip().upper()
    try:
        credits = int(data.get("credits", 0))
    except (TypeError, ValueError):
        credits = 0
    raw_max = data.get("max_uses")
    try:
        max_uses = int(raw_max) if raw_max not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        max_uses = None
    note = (data.get("note") or "").strip() or None
    expires_iso = data.get("expires_at")
    expires_at = None
    if expires_iso:
        try:
            expires_at = datetime.fromisoformat(str(expires_iso).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            expires_at = None
    if not code or not _COUPON_CODE_RE.match(code):
        return jsonify({"error": "invalid_code", "hint": "A-Z, 0-9, _, - only. 3-50 chars"}), 400
    if credits <= 0 or credits > 10000:
        return jsonify({"error": "invalid_credits", "hint": "1-10000"}), 400
    if Coupon.query.filter_by(code=code).first():
        return jsonify({"error": "code_exists"}), 409
    coupon = Coupon(
        code=code, credits=credits, max_uses=max_uses, note=note,
        expires_at=expires_at, created_by=user.email, is_active=True
    )
    db.session.add(coupon)
    db.session.commit()
    return jsonify({"ok": True, "id": coupon.id, "code": coupon.code})

@app.route("/admin/coupons/<int:cid>/toggle", methods=["POST"])
def admin_coupons_toggle(cid):
    user = current_user()
    if not is_admin(user):
        return jsonify({"error": "forbidden"}), 403
    coupon = Coupon.query.get_or_404(cid)
    coupon.is_active = not bool(coupon.is_active)
    db.session.commit()
    return jsonify({"ok": True, "is_active": coupon.is_active})

@app.route("/admin/coupons/<int:cid>", methods=["DELETE"])
def admin_coupons_delete(cid):
    user = current_user()
    if not is_admin(user):
        return jsonify({"error": "forbidden"}), 403
    coupon = Coupon.query.get_or_404(cid)
    if (coupon.used_count or 0) > 0:
        coupon.is_active = False
        db.session.commit()
        return jsonify({"ok": True, "deactivated": True})
    db.session.delete(coupon)
    db.session.commit()
    return jsonify({"ok": True, "deleted": True})

@app.route("/admin/credits/grant", methods=["POST"])
def admin_credits_grant():
    """Admin manually grants credits to a user by email. For coupon-less seeds."""
    user = current_user()
    if not is_admin(user):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or request.form or {}
    email = (data.get("email") or "").strip().lower()
    try:
        amount = int(data.get("credits", 0))
    except (TypeError, ValueError):
        amount = 0
    note = (data.get("note") or "manual seed").strip()[:200]
    if not email or amount <= 0 or amount > 10000:
        return jsonify({"error": "invalid_input"}), 400
    target = User.query.filter(db.func.lower(User.email) == email).first()
    if not target:
        return jsonify({"error": "user_not_found"}), 404
    target.credits = (target.credits or 0) + amount
    db.session.commit()
    print(f"[ADMIN_GRANT] {user.email} -> {target.email} +{amount} ({note}) -> {target.credits}")
    return jsonify({"ok": True, "email": target.email, "added": amount, "new_balance": target.credits})
