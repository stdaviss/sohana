import uuid, hashlib, hmac, secrets
from functools import wraps
from flask import session, jsonify, redirect, url_for, request
from database import get_db, fetchone, post_transaction

def hash_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000)
    return f"{salt}${h.hex()}"

def verify_password(password, stored):
    try:
        salt, hx = stored.split("$", 1)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000)
        return hmac.compare_digest(h.hex(), hx)
    except Exception:
        return False

def register_user(phone, full_name, password, email=None, country="RW",
                  first_name=None, last_name=None, gender=None,
                  date_of_birth=None, nationality=None, occupation=None,
                  source_of_funds=None):
    existing = fetchone("SELECT id FROM users WHERE phone=?", (phone,))
    if existing:
        raise ValueError("Phone number already registered")
    uid = str(uuid.uuid4())
    wid = str(uuid.uuid4())
    pw  = hash_password(password)
    with get_db() as db:
        db.execute(
            """INSERT INTO users(id,phone,email,full_name,password_hash,country,
               first_name,last_name,gender,date_of_birth,nationality,occupation,source_of_funds)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (uid, phone, email, full_name, pw, country,
             first_name, last_name, gender, date_of_birth,
             nationality, occupation, source_of_funds)
        )
        db.execute("INSERT INTO wallets(id,user_id,currency,is_default) VALUES(?,?,?,1)", (wid, uid, "EUR"))
    return uid

def login_user(identifier, password):
    """Login by phone OR email."""
    row = fetchone("SELECT id, password_hash, full_name FROM users WHERE phone=?", (identifier,))
    if not row and "@" in identifier:
        row = fetchone("SELECT id, password_hash, full_name FROM users WHERE email=?", (identifier,))
    if not row:
        raise ValueError("Invalid credentials")
    if not verify_password(password, row["password_hash"]):
        raise ValueError("Invalid credentials")
    return dict(row)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("auth_page"))
        check = fetchone("SELECT id FROM users WHERE id=?", (session["user_id"],))
        if check is None:
            session.clear()
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Session expired"}), 401
            return redirect(url_for("auth_page"))
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if "user_id" not in session:
        return None
    row = fetchone(
        """SELECT id,phone,full_name,email,country,hanatag,bio,language,base_currency,
                  ncs_score,ncs_tier,kyc_level,kyc_status,
                  first_name,last_name,gender,date_of_birth,nationality,occupation,source_of_funds,
                  is_admin,admin_role,notif_email,notif_push,notif_sms,
                  freeze_deposits,freeze_withdrawals,freeze_reason,created_at
           FROM users WHERE id=?""",
        (session["user_id"],)
    )
    return dict(row) if row else None
