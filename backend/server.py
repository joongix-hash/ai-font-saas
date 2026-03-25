from flask import Flask, request, render_template, send_from_directory, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from authlib.integrations.flask_client import OAuth
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv
from datetime import datetime
import os, uuid, io, math, traceback, zipfile, shutil, time, stripe, json

# ─── Module engines ───────────────────────────────────────────────────────────
from modules.sprite_engine import generate_sprite_sheet
from modules.pixel_engine  import convert_to_pixel_art, get_palette_hex
from modules.ui_engine     import generate_9slice

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-please-change-in-production")

# OAuth 콜백 시 세션 쿠키가 정상 전달되도록 설정
_IS_PRODUCTION = bool(os.getenv("RENDER", ""))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = _IS_PRODUCTION
app.config["SESSION_COOKIE_HTTPONLY"] = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Render.com 등 클라우드 환경에서는 /tmp 를 사용 (ephemeral), 로컬은 outputs/ 사용
_RENDER = os.getenv("RENDER", "")
OUTPUT_FOLDER = "/tmp/copyfont_outputs" if _RENDER else os.path.join(BASE_DIR, "outputs")
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ─── Database ────────────────────────────────────────────────────────────────
_db_url = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'copyfont.db')}")
# SQLAlchemy requires postgresql:// not postgres://
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ─── Stripe ──────────────────────────────────────────────────────────────────
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")

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

class Feedback(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    message    = db.Column(db.Text, nullable=False)
    rating     = db.Column(db.Integer)
    email      = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

# ─── Helpers ──────────────────────────────────────────────────────────────────
def current_user():
    if "user_id" not in session:
        return None
    return db.session.get(User, session["user_id"])

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
    kwargs.setdefault("stripe_pk", STRIPE_PUBLISHABLE_KEY)
    return render_template(template, **kwargs)

# ─── Auth Routes ──────────────────────────────────────────────────────────────
@app.route("/auth/google")
def auth_google():
    if not os.getenv("GOOGLE_CLIENT_ID"):
        return render(error_message="Google OAuth가 아직 설정되지 않았습니다. .env에 GOOGLE_CLIENT_ID를 추가해주세요.")
    redirect_uri = url_for("auth_callback", _external=True)
    return google_oauth.authorize_redirect(redirect_uri)

@app.route("/auth/callback")
def auth_callback():
    try:
        token = google_oauth.authorize_access_token()
        info = token.get("userinfo")
        if not info:
            return redirect(url_for("index"))
        user = User.query.filter_by(google_id=info["sub"]).first()
        if not user:
            user = User(
                google_id=info["sub"],
                email=info.get("email"),
                name=info.get("name"),
                picture=info.get("picture"),
                credits=FREE_CREDITS
            )
            db.session.add(user)
            db.session.commit()
        session["user_id"] = user.id
    except Exception:
        traceback.print_exc()
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ─── Payment Routes ───────────────────────────────────────────────────────────
@app.route("/checkout/<package_key>", methods=["POST"])
def checkout(package_key):
    user = current_user()
    if not user:
        return render(error_message="결제를 위해 먼저 로그인해주세요.")
    if not stripe.api_key:
        return render(error_message="결제 시스템이 아직 준비 중입니다.")
    pkg = CREDIT_PACKAGES.get(package_key)
    if not pkg:
        return "Invalid package", 400
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"PixelForge {pkg['name']} Pack — {pkg['desc']}"},
                    "unit_amount": pkg["price_cents"],
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=url_for("payment_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("index", _external=True),
            metadata={
                "user_id": str(user.id),
                "package_key": package_key,
                "credits": str(pkg["credits"]),
            },
        )
        return redirect(checkout_session.url)
    except Exception as e:
        return render(error_message=f"결제 오류: {str(e)}")

@app.route("/payment/success")
def payment_success():
    return render(success_message=f"✅ 결제가 완료되었습니다! 크레딧이 곧 추가됩니다.")

@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return "", 400
    if event["type"] == "checkout.session.completed":
        meta = event["data"]["object"].get("metadata", {})
        try:
            user = db.session.get(User, int(meta.get("user_id", 0)))
            credits = int(meta.get("credits", 0))
            if user and credits > 0:
                user.credits += credits
                db.session.commit()
                print(f"[STRIPE] 크레딧 추가: user={user.email} +{credits} → {user.credits}")
        except Exception:
            traceback.print_exc()
    return "", 200

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
Disallow: /stripe/

Sitemap: https://pixelforge.onrender.com/sitemap.xml
"""
    return Response(content, mimetype="text/plain")

@app.route("/sitemap.xml")
def sitemap_xml():
    from flask import Response
    content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://pixelforge.onrender.com/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
"""
    return Response(content, mimetype="application/xml")

# ─── Main Routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
