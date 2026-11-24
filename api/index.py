import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Optional

import psycopg2
from psycopg2 import errors as pg_errors
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# Load environment variables (Make sure you have a .env file with DATABASE_URL)
load_dotenv()

logger = logging.getLogger("omniverse_api")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Omniverse API", version="1.1.0")

# CORS: auth is a header (x-api-key), never a cookie, so credentials are NOT
# needed. In production the frontend and API share an origin (Vercel), so no
# cross-origin access is required at all. Extra origins can be added with
# ALLOWED_ORIGINS="https://a.example,https://b.example". Localhost on any port
# is allowed so local development keeps working.
_extra_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_extra_origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["x-api-key", "content-type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    return response

# ---------------------------------------------------------
# DATA ACCESS LAYER
# Every raw SQL statement lives here so endpoint handlers stay
# focused on request/response shaping and permission checks.
# ---------------------------------------------------------

# One small connection pool per server process. Opening a fresh connection to
# Neon on every query (the old approach) costs a couple of seconds of network
# round-trips each time, and a single click used to open two of them. A warm
# server now reuses connections instead.
_pool = None
_pool_lock = threading.Lock()


def get_pool():
    global _pool
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is not set")
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = pg_pool.ThreadedConnectionPool(
                    1, 10,
                    os.getenv("DATABASE_URL"),
                    cursor_factory=RealDictCursor,
                    connect_timeout=10,
                )
    return _pool


def run_query(query, params=None, fetch=None, commit=False):
    """
    Runs one SQL statement on a pooled connection.

    fetch: None | "one" | "all"
    commit: whether to commit (needed for INSERT/UPDATE/DELETE)

    Raises the original psycopg2 exception on failure (including
    UniqueViolation) so the calling endpoint can decide the right
    HTTP status code. Never leaks raw DB text to the caller itself.

    Neon closes idle connections, so a pooled connection can be dead by the
    time it is reused. If that happens we discard it and retry once on a
    fresh one.
    """
    for attempt in range(2):
        try:
            conn = get_pool().getconn()
        except Exception:
            logger.exception("Database connection error")
            raise HTTPException(status_code=503, detail="Database connection failed. Please try again later.")

        committing = False
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(query, params or ())
                result = None
                if fetch == "one":
                    result = cursor.fetchone()
                elif fetch == "all":
                    result = cursor.fetchall()
                if commit:
                    committing = True
                    conn.commit()
            finally:
                cursor.close()
            get_pool().putconn(conn)
            return result
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            # Stale/dropped connection: throw it away. Retry once, but NEVER
            # if the failure happened during COMMIT: the write may already be
            # saved, and retrying would duplicate it (double INSERT).
            get_pool().putconn(conn, close=True)
            if attempt == 1 or committing:
                raise
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            get_pool().putconn(conn)
            raise


def db_get_comic_count():
    row = run_query("SELECT COUNT(*) FROM comic_archive;", fetch="one")
    return row["count"]


def db_get_all_heroes():
    return run_query(
        "SELECT id, alias, publisher, alignment, affiliation, origin_era, image, civilian_name "
        "FROM comic_archive ORDER BY id ASC;",
        fetch="all",
    )


def db_get_hero_by_id(hero_id: int):
    return run_query("SELECT * FROM comic_archive WHERE id = %s;", (hero_id,), fetch="one")


def _like(term: str) -> str:
    # Escape LIKE wildcards so a user's % or _ is matched literally.
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def db_search_heroes(q: Optional[str], publisher: Optional[str], alignment: Optional[str]):
    # Same public column list as the catalog: search must never expose the
    # classified dossier fields that /api/heroes/{id} reserves for registered agents.
    query = (
        "SELECT id, alias, publisher, alignment, affiliation, origin_era, image, civilian_name "
        "FROM comic_archive WHERE 1=1"
    )
    params = []
    if q:
        query += " AND (alias ILIKE %s OR civilian_name ILIKE %s)"
        params.extend([_like(q), _like(q)])
    if publisher:
        query += " AND publisher ILIKE %s"
        params.append(_like(publisher))
    if alignment:
        query += " AND alignment ILIKE %s"
        params.append(_like(alignment))
    query += " ORDER BY id ASC LIMIT 200"
    return run_query(query, tuple(params), fetch="all")


def db_insert_hero(fields: dict):
    columns = ', '.join(fields.keys())
    placeholders = ', '.join(['%s'] * len(fields))
    values = tuple(fields.values())
    query = f"INSERT INTO comic_archive ({columns}) VALUES ({placeholders}) RETURNING id;"
    row = run_query(query, values, fetch="one", commit=True)
    return row["id"]


def db_update_hero(hero_id: int, fields: dict):
    set_clause = ', '.join([f"{key} = %s" for key in fields.keys()])
    values = tuple(fields.values()) + (hero_id,)
    query = f"UPDATE comic_archive SET {set_clause} WHERE id = %s RETURNING id;"
    row = run_query(query, values, fetch="one", commit=True)
    return row is not None


def db_delete_hero(hero_id: int):
    row = run_query(
        "DELETE FROM comic_archive WHERE id = %s RETURNING id;", (hero_id,), fetch="one", commit=True
    )
    return row is not None


def db_get_user_by_api_key(api_key: str):
    # `id` is included so routes that write records tied to a specific agent
    # (intel reports) have a real foreign key to use, not just a username.
    return run_query(
        "SELECT id, username, role FROM users WHERE api_key = %s;", (api_key,), fetch="one"
    )


# --- Password hashing (scrypt, Python stdlib: no new dependency) ----------
# Stored format: scrypt$<N>$<r>$<p>$<salt hex>$<hash hex>
# Legacy plaintext rows are still accepted ONCE and are re-hashed on that
# successful login, so existing users migrate transparently.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if stored and stored.startswith("scrypt$"):
        try:
            _, n, r, p, salt_hex, hash_hex = stored.split("$")
            digest = hashlib.scrypt(
                password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p)
            )
            return hmac.compare_digest(digest.hex(), hash_hex)
        except Exception:
            return False
    # Legacy plaintext row (constant-time compare).
    return hmac.compare_digest((stored or "").encode(), password.encode())


_schema_ready = False


def ensure_security_schema():
    """Hashes are longer than the old varchar(50) password column. Widen it once
    (idempotent, instant on Postgres) before any hash is written."""
    global _schema_ready
    if _schema_ready:
        return
    run_query("ALTER TABLE users ALTER COLUMN password TYPE text;", commit=True)
    _schema_ready = True


def db_create_user(username: str, password: str):
    # Role is always 'user' here, never taken from the request, so nobody can
    # sign themselves up as an admin. Admins are created directly in the database.
    ensure_security_schema()
    api_key = secrets.token_urlsafe(32)
    return run_query(
        "INSERT INTO users (username, password, api_key, role) "
        "VALUES (%s, %s, %s, 'user') RETURNING username, api_key, role;",
        (username, hash_password(password), api_key),
        fetch="one",
        commit=True,
    )


def db_get_user_by_credentials(username: str, password: str):
    row = run_query(
        "SELECT username, role, api_key, password FROM users WHERE username = %s;",
        (username,),
        fetch="one",
    )
    if not row:
        # Burn comparable time so unknown usernames can't be told apart by speed.
        verify_password(password, "scrypt$16384$8$1$" + "00" * 16 + "$" + "00" * 64)
        return None
    if not verify_password(password, row["password"]):
        return None
    if not row["password"].startswith("scrypt$"):
        try:  # upgrade legacy plaintext -> hash on successful login
            ensure_security_schema()
            run_query("UPDATE users SET password = %s WHERE username = %s;",
                      (hash_password(password), username), commit=True)
        except Exception:
            logger.exception("Could not upgrade legacy password hash for %s", username)
    return {"username": row["username"], "role": row["role"], "api_key": row["api_key"]}


# ---------------------------------------------------------
# GAME DATA — Matchup Simulator & Danger Room
# power_stats and nemesis_links existed in the schema but were never
# queried anywhere; these functions are what actually puts them to use.
# ---------------------------------------------------------

POWER_STAT_FIELDS = [
    "intelligence", "strength", "speed",
    "durability", "energy_projection", "fighting_ability",
]


def db_get_matchup_roster():
    # Only characters that actually have a power_stats row can be put into
    # a fair fight — everyone else is left out rather than guessed at.
    return run_query(
        """
        SELECT c.id, c.alias, c.publisher, c.alignment, c.image,
               p.intelligence, p.strength, p.speed,
               p.durability, p.energy_projection, p.fighting_ability
        FROM comic_archive c
        JOIN power_stats p ON p.hero_id = c.id
        ORDER BY c.alias ASC;
        """,
        fetch="all",
    )


def db_get_fighter(hero_id: int):
    return run_query(
        """
        SELECT c.id, c.alias, c.publisher, c.alignment, c.image,
               p.intelligence, p.strength, p.speed,
               p.durability, p.energy_projection, p.fighting_ability
        FROM comic_archive c
        JOIN power_stats p ON p.hero_id = c.id
        WHERE c.id = %s;
        """,
        (hero_id,),
        fetch="one",
    )


def db_get_danger_room_roster():
    # Only pairs where BOTH sides have power_stats are listed, so every
    # Danger Room session always has real numbers to run the fight on.
    return run_query(
        """
        SELECT
            h.id AS hero_id, h.alias AS hero_alias, h.publisher AS hero_publisher, h.image AS hero_image,
            v.id AS villain_id, v.alias AS villain_alias, v.publisher AS villain_publisher, v.image AS villain_image,
            n.first_encounter_issue
        FROM nemesis_links n
        JOIN comic_archive h ON h.id = n.hero_id
        JOIN comic_archive v ON v.id = n.villain_id
        JOIN power_stats hp ON hp.hero_id = h.id
        JOIN power_stats vp ON vp.hero_id = v.id
        ORDER BY h.alias ASC, v.alias ASC;
        """,
        fetch="all",
    )


def db_get_nemesis_pair(hero_id: int, villain_id: int):
    return run_query(
        "SELECT link_id, first_encounter_issue FROM nemesis_links "
        "WHERE hero_id = %s AND villain_id = %s;",
        (hero_id, villain_id),
        fetch="one",
    )


def db_get_power_stats(hero_id: int):
    return run_query("SELECT * FROM power_stats WHERE hero_id = %s;", (hero_id,), fetch="one")


def db_upsert_power_stats(hero_id: int, stats: dict):
    columns = ["hero_id"] + list(stats.keys())
    values = [hero_id] + list(stats.values())
    placeholders = ", ".join(["%s"] * len(values))
    update_clause = ", ".join(f"{key} = EXCLUDED.{key}" for key in stats.keys())
    query = (
        f"INSERT INTO power_stats ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT (hero_id) DO UPDATE SET {update_clause} "
        f"RETURNING *;"
    )
    return run_query(query, tuple(values), fetch="one", commit=True)


def db_get_nemeses_for_hero(hero_id: int):
    return run_query(
        """
        SELECT n.link_id, n.villain_id, v.alias AS villain_alias, v.image AS villain_image,
               n.first_encounter_issue
        FROM nemesis_links n
        JOIN comic_archive v ON v.id = n.villain_id
        WHERE n.hero_id = %s
        ORDER BY v.alias ASC;
        """,
        (hero_id,),
        fetch="all",
    )


def db_add_nemesis_link(hero_id: int, villain_id: int, first_encounter_issue: Optional[str]):
    return run_query(
        "INSERT INTO nemesis_links (hero_id, villain_id, first_encounter_issue) "
        "VALUES (%s, %s, %s) RETURNING link_id;",
        (hero_id, villain_id, first_encounter_issue),
        fetch="one",
        commit=True,
    )


def db_delete_nemesis_link(hero_id: int, villain_id: int):
    row = run_query(
        "DELETE FROM nemesis_links WHERE hero_id = %s AND villain_id = %s RETURNING link_id;",
        (hero_id, villain_id),
        fetch="one",
        commit=True,
    )
    return row is not None


def resolve_battle(fighter_a: dict, fighter_b: dict):
    # Deterministic, stats-only resolution: same math for every pairing,
    # regardless of publisher, alignment, or alias — nothing is hardcoded
    # to favor one side.
    total_a = sum(fighter_a[s] for s in POWER_STAT_FIELDS)
    total_b = sum(fighter_b[s] for s in POWER_STAT_FIELDS)

    tiebreak_order = ["fighting_ability", "strength", "intelligence", "speed", "durability", "energy_projection"]
    winner = None
    if total_a > total_b:
        winner = "a"
    elif total_b > total_a:
        winner = "b"
    else:
        for stat in tiebreak_order:
            if fighter_a[stat] > fighter_b[stat]:
                winner = "a"
                break
            if fighter_b[stat] > fighter_a[stat]:
                winner = "b"
                break
        # else: every stat tied — a genuine draw.

    def public_view(fighter, total):
        return {
            "id": fighter["id"],
            "alias": fighter["alias"],
            "publisher": fighter["publisher"],
            "alignment": fighter["alignment"],
            "image": fighter["image"],
            "stats": {s: fighter[s] for s in POWER_STAT_FIELDS},
            "total": total,
        }

    return {
        "fighter_a": public_view(fighter_a, total_a),
        "fighter_b": public_view(fighter_b, total_b),
        "winner": winner,  # "a" | "b" | None (draw)
    }


# ---------------------------------------------------------
# INTEL REPORTS — filed by registered Agents, readable by every Agent
# ---------------------------------------------------------

def db_list_intel_reports():
    return run_query(
        """
        SELECT r.report_id, r.hero_id, h.alias AS hero_alias, h.image AS hero_image,
               r.agent_id, u.username AS agent_username,
               r.threat_status, r.report_text, r.filed_at
        FROM intel_reports r
        JOIN comic_archive h ON h.id = r.hero_id
        JOIN users u ON u.id = r.agent_id
        ORDER BY r.filed_at DESC;
        """,
        fetch="all",
    )


def db_get_intel_report(report_id: int):
    return run_query("SELECT * FROM intel_reports WHERE report_id = %s;", (report_id,), fetch="one")


def db_create_intel_report(hero_id: int, agent_id: int, threat_status: str, report_text: str):
    return run_query(
        "INSERT INTO intel_reports (hero_id, agent_id, threat_status, report_text) "
        "VALUES (%s, %s, %s, %s) RETURNING report_id;",
        (hero_id, agent_id, threat_status, report_text),
        fetch="one",
        commit=True,
    )


def db_update_intel_report(report_id: int, fields: dict):
    set_clause = ", ".join(f"{key} = %s" for key in fields.keys())
    values = tuple(fields.values()) + (report_id,)
    query = f"UPDATE intel_reports SET {set_clause} WHERE report_id = %s RETURNING report_id;"
    row = run_query(query, values, fetch="one", commit=True)
    return row is not None


def db_delete_intel_report(report_id: int):
    row = run_query(
        "DELETE FROM intel_reports WHERE report_id = %s RETURNING report_id;",
        (report_id,),
        fetch="one",
        commit=True,
    )
    return row is not None


# ---------------------------------------------------------
# AUTHENTICATION & SECURITY (3-TIER SYSTEM)
# ---------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class SignupRequest(BaseModel):
    # Limits match the users table (username/password are varchar(50)).
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(..., min_length=6, max_length=50)


# Short-lived cache of API-key lookups so every click doesn't pay for an extra
# database round-trip just to re-check who you are. A role change or removed
# account can take up to AUTH_CACHE_TTL seconds (per server instance) to show up.
_AUTH_CACHE = {}
AUTH_CACHE_TTL = 15


# 1. Base Security (Allows Admins, Users, and Guests)
def get_current_user(x_api_key: str = Header(None)):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Unauthorized: Missing Clearance Key")

    # Hardcoded Guest bypass — intentional: this is the public, read-only tier
    # and is not a real account, so it is not stored in `users`.
    if x_api_key == "public_guest_clearance":
        return {"username": "Civilian_Guest", "role": "guest"}

    cached = _AUTH_CACHE.get(x_api_key)
    if cached and time.time() - cached[0] < AUTH_CACHE_TTL:
        return cached[1]

    try:
        user = db_get_user_by_api_key(x_api_key)
    except Exception:
        logger.exception("Database error while validating API key")
        raise HTTPException(status_code=503, detail="Could not verify credentials right now. Please try again.")

    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid Clearance Key")

    if len(_AUTH_CACHE) > 500:
        _AUTH_CACHE.clear()
    user = dict(user)
    _AUTH_CACHE[x_api_key] = (time.time(), user)
    return user


# 2. Middle-Tier Security (Blocks Guests, Allows Users & Admins)
def require_registered(user: dict = Depends(get_current_user)):
    if user['role'] == 'guest':
        raise HTTPException(status_code=403, detail="Forbidden: Registered Agent Clearance Required")
    return user


# 3. Top-Tier Security (Admins Only)
def require_admin(user: dict = Depends(get_current_user)):
    if user['role'] != 'admin':
        raise HTTPException(status_code=403, detail="Forbidden: Omega Level Admin Clearance Required")
    return user


# In-memory sliding-window limiter. Per server instance (serverless instances
# don't share memory), so it blunts casual brute force but is not a substitute
# for an edge/WAF limit on a large deployment.
_RATE = defaultdict(deque)
_RATE_LOCK = threading.Lock()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def rate_limit(bucket: str, limit: int, window_seconds: int):
    now = time.time()
    with _RATE_LOCK:
        hits = _RATE[bucket]
        while hits and now - hits[0] > window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            retry = int(window_seconds - (now - hits[0])) + 1
            raise HTTPException(status_code=429, detail=f"Too many attempts. Try again in {retry}s.",
                                headers={"Retry-After": str(retry)})
        hits.append(now)
        if len(_RATE) > 5000:  # bound memory
            for k in [k for k, v in _RATE.items() if not v][:1000]:
                _RATE.pop(k, None)


@app.post("/api/login")
def login(req: LoginRequest, request: Request):
    ip = client_ip(request)
    rate_limit(f"login-ip:{ip}", 30, 900)
    rate_limit(f"login-user:{ip}:{req.username.lower()}", 8, 900)
    try:
        user = db_get_user_by_credentials(req.username, req.password)
    except Exception:
        logger.exception("Database error during login")
        raise HTTPException(status_code=503, detail="Login is temporarily unavailable. Please try again.")

    if not user:
        raise HTTPException(status_code=401, detail="Invalid Credentials")

    return {"api_key": user["api_key"], "role": user["role"], "username": user["username"]}


@app.post("/api/signup", status_code=201)
def signup(req: SignupRequest, request: Request):
    rate_limit(f"signup-ip:{client_ip(request)}", 5, 3600)
    try:
        user = db_create_user(req.username, req.password)
    except pg_errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="That username is already taken.")
    except Exception:
        logger.exception("Database error during signup")
        raise HTTPException(status_code=503, detail="Sign up is temporarily unavailable. Please try again.")

    # Same response shape as /api/login so the frontend can log the new user straight in.
    return {"api_key": user["api_key"], "role": user["role"], "username": user["username"]}


# ---------------------------------------------------------
# SECURE PYDANTIC VALIDATION MODELS
# ---------------------------------------------------------

_MARKUP_RE = re.compile(r'<\s*/?\s*[a-zA-Z!?]|javascript\s*:|on\w+\s*=', re.IGNORECASE)


class HeroCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=100, description="Hero/Villain alias")
    civilian_name: Optional[str] = Field(None, max_length=100)
    origin_era: Optional[str] = Field(None, max_length=50)
    publisher: Optional[str] = Field(None, max_length=50)
    alignment: Optional[str] = Field(None, max_length=30)
    power_tier: Optional[str] = Field(None, max_length=50)
    affiliation: Optional[str] = Field(None, max_length=100)
    threat_level: Optional[str] = Field(None, max_length=50)
    base_of_operations: Optional[str] = Field(None, max_length=100)
    primary_powers: Optional[str] = Field(None, max_length=500)
    tactical_vulnerability: Optional[str] = Field(None, max_length=500)
    signature_gear: Optional[str] = Field(None, max_length=255)
    combat_doctrine: Optional[str] = Field(None, max_length=500)
    psychological_dossier: Optional[str] = Field(None, max_length=500)
    arch_nemesis: Optional[str] = Field(None, max_length=100)
    image: Optional[str] = Field(None, max_length=255)
    # Added so every column in comic_archive is actually reachable through the API.
    species_or_origin: Optional[str] = Field(None, max_length=255)
    classification: Optional[str] = Field(None, max_length=100)
    tactical_profile: Optional[str] = Field(None, max_length=500)
    primary_adversary: Optional[str] = Field(None, max_length=255)

    # Defence in depth only: the frontend escapes all output (esc() in shared.js),
    # which is the real XSS protection. Here we trim and refuse anything that
    # looks like markup, so hostile HTML never gets stored in the first place.
    @field_validator('*', mode='before')
    def sanitize_strings(cls, value):
        if isinstance(value, str):
            cleaned = value.strip()
            if _MARKUP_RE.search(cleaned):
                raise ValueError("HTML/markup is not allowed in dossier fields.")
            return cleaned
        return value

    @field_validator('image')
    def validate_image(cls, value):
        # Only http(s) URLs or plain relative paths (e.g. comics/x.jpg). Blocks javascript:/data: URIs.
        if value and not re.match(r'^(https?://[^\s"\'<>]+|[A-Za-z0-9_./-]+)$', value):
            raise ValueError("Image must be an http(s) URL or a relative file path.")
        return value

    # Publisher whitelist enforcement
    @field_validator('publisher')
    def validate_publisher(cls, value):
        if value:
            allowed_publishers = ['Marvel', 'DC', 'Dynamite', 'Image', 'Valiant', 'Independent']
            matched = next((p for p in allowed_publishers if p.lower() == value.lower()), None)
            if not matched:
                raise ValueError(f"Publisher must be one of: {', '.join(allowed_publishers)}")
            return matched
        return value


class HeroUpdate(HeroCreate):
    # For PUT requests, we make alias optional so partial updates don't fail
    alias: Optional[str] = Field(None, min_length=1, max_length=100)


# ---------------------------------------------------------
# GAME & COMMUNITY MODELS
# ---------------------------------------------------------

class PowerStatsIn(BaseModel):
    intelligence: int = Field(..., ge=1, le=7)
    strength: int = Field(..., ge=1, le=7)
    speed: int = Field(..., ge=1, le=7)
    durability: int = Field(..., ge=1, le=7)
    energy_projection: int = Field(..., ge=1, le=7)
    fighting_ability: int = Field(..., ge=1, le=7)


class NemesisLinkIn(BaseModel):
    villain_id: int
    first_encounter_issue: Optional[str] = Field(None, max_length=255)


class MatchupRequest(BaseModel):
    hero_a_id: int
    hero_b_id: int


class DangerRoomRequest(BaseModel):
    hero_id: int
    villain_id: int


class IntelReportCreate(BaseModel):
    hero_id: int
    threat_status: str = Field("Monitoring", min_length=1, max_length=50)
    report_text: str = Field(..., min_length=1, max_length=2000)

    @field_validator('report_text', 'threat_status', mode='before')
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('report_text', 'threat_status')
    def no_markup(cls, value):
        if _MARKUP_RE.search(value):
            raise ValueError("HTML/markup is not allowed in intel reports.")
        return value


class IntelReportUpdate(BaseModel):
    threat_status: Optional[str] = Field(None, min_length=1, max_length=50)
    report_text: Optional[str] = Field(None, min_length=1, max_length=2000)

    @field_validator('report_text', 'threat_status', mode='before')
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('report_text', 'threat_status')
    def no_markup(cls, value):
        if value is not None and _MARKUP_RE.search(value):
            raise ValueError("HTML/markup is not allowed in intel reports.")
        return value


# ---------------------------------------------------------
# NEON DATABASE CRUD ENDPOINTS
# ---------------------------------------------------------

@app.get("/api/health")
def api_health():
    return {"status": "online"}


@app.get("/")
def root():
    try:
        count = db_get_comic_count()
    except HTTPException:
        raise
    except Exception:
        logger.exception("Database error on root status check")
        raise HTTPException(status_code=503, detail="Database is currently unreachable.")
    return {"registry_status": "ONLINE", "dossiers_cataloged": count}


@app.get("/health")
def health_check():
    return {"status": "online"}


@app.get("/api/heroes/search", dependencies=[Depends(get_current_user)])
def search_heroes(q: Optional[str] = None, publisher: Optional[str] = None, alignment: Optional[str] = None):
    try:
        return db_search_heroes(q, publisher, alignment)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Database error during hero search")
        raise HTTPException(status_code=503, detail="Search is temporarily unavailable. Please try again.")


@app.get("/api/heroes", dependencies=[Depends(get_current_user)])
def get_all_heroes():
    # Guests, Users, and Admins can all view the global catalog
    try:
        return db_get_all_heroes()
    except HTTPException:
        raise
    except Exception:
        logger.exception("Database error while listing heroes")
        raise HTTPException(status_code=503, detail="Could not load the catalog right now. Please try again.")


@app.get("/api/heroes/{hero_id}", dependencies=[Depends(require_registered)])
def get_hero(hero_id: int):
    # Middle-Tier: Only Users and Admins can view highly classified full dossier details
    try:
        hero = db_get_hero_by_id(hero_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Database error while fetching hero %s", hero_id)
        raise HTTPException(status_code=503, detail="Could not load this dossier right now. Please try again.")

    if not hero:
        raise HTTPException(status_code=404, detail="Dossier not found")
    return hero


@app.post("/api/heroes", status_code=201, dependencies=[Depends(require_admin)])
def create_hero(hero: HeroCreate):
    fields = hero.model_dump(exclude_unset=True)
    try:
        new_id = db_insert_hero(fields)
    except pg_errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="A dossier with that alias already exists.")
    except Exception:
        logger.exception("Database error while creating a hero")
        raise HTTPException(status_code=500, detail="Could not create the dossier due to a server error.")

    return {"message": "Dossier created successfully", "id": new_id, "data": fields}


@app.put("/api/heroes/{hero_id}", dependencies=[Depends(require_admin)])
def update_hero(hero_id: int, hero: HeroUpdate):
    fields = hero.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided for update")
    if "alias" in fields and fields["alias"] is None:
        raise HTTPException(status_code=422, detail="Alias cannot be null.")

    try:
        found = db_update_hero(hero_id, fields)
    except pg_errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="A dossier with that alias already exists.")
    except Exception:
        logger.exception("Database error while updating hero %s", hero_id)
        raise HTTPException(status_code=500, detail="Could not update the dossier due to a server error.")

    if not found:
        raise HTTPException(status_code=404, detail="Dossier not found")

    return {"message": "Dossier updated successfully", "updated_fields": fields}


@app.delete("/api/heroes/{hero_id}", dependencies=[Depends(require_admin)])
def delete_hero(hero_id: int):
    try:
        found = db_delete_hero(hero_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Database error while deleting hero %s", hero_id)
        raise HTTPException(status_code=500, detail="Could not delete the dossier due to a server error.")

    if not found:
        raise HTTPException(status_code=404, detail="Dossier not found")

    return {"message": f"Dossier {hero_id} purged from archive"}


# ---------------------------------------------------------
# GAMES — Matchup Simulator & Danger Room
# Registered Agents and Admins only: these run on classified combat
# stats Guests never see anywhere else in the app either.
# ---------------------------------------------------------

@app.get("/api/game/matchup-roster", dependencies=[Depends(require_registered)])
def matchup_roster():
    try:
        return db_get_matchup_roster()
    except Exception:
        logger.exception("Database error while loading the matchup roster")
        raise HTTPException(status_code=503, detail="Could not load the roster right now. Please try again.")


@app.post("/api/game/matchup", dependencies=[Depends(require_registered)])
def run_matchup(req: MatchupRequest):
    if req.hero_a_id == req.hero_b_id:
        raise HTTPException(status_code=400, detail="Pick two different combatants.")

    try:
        fighter_a = db_get_fighter(req.hero_a_id)
        fighter_b = db_get_fighter(req.hero_b_id)
    except Exception:
        logger.exception("Database error while running a matchup")
        raise HTTPException(status_code=503, detail="The simulator is temporarily unavailable. Please try again.")

    if not fighter_a or not fighter_b:
        raise HTTPException(status_code=404, detail="One or both combatants have no power stats on file.")

    return resolve_battle(fighter_a, fighter_b)


@app.get("/api/game/danger-room-roster", dependencies=[Depends(require_registered)])
def danger_room_roster():
    try:
        rows = db_get_danger_room_roster()
    except Exception:
        logger.exception("Database error while loading the Danger Room roster")
        raise HTTPException(status_code=503, detail="Could not load the roster right now. Please try again.")

    # Group villains under their hero so the frontend gets a hero picker,
    # then a nemesis sub-picker, with no extra requests.
    grouped = {}
    for row in rows:
        hero_id = row["hero_id"]
        if hero_id not in grouped:
            grouped[hero_id] = {
                "hero": {
                    "id": hero_id,
                    "alias": row["hero_alias"],
                    "publisher": row["hero_publisher"],
                    "image": row["hero_image"],
                },
                "nemeses": [],
            }
        grouped[hero_id]["nemeses"].append({
            "villain": {
                "id": row["villain_id"],
                "alias": row["villain_alias"],
                "publisher": row["villain_publisher"],
                "image": row["villain_image"],
            },
            "first_encounter_issue": row["first_encounter_issue"],
        })
    return list(grouped.values())


@app.post("/api/game/danger-room/simulate", dependencies=[Depends(require_registered)])
def run_danger_room(req: DangerRoomRequest):
    try:
        pair = db_get_nemesis_pair(req.hero_id, req.villain_id)
    except Exception:
        logger.exception("Database error while validating a Danger Room pairing")
        raise HTTPException(status_code=503, detail="The Danger Room is temporarily unavailable. Please try again.")

    if not pair:
        raise HTTPException(status_code=404, detail="That is not a recorded nemesis pairing.")

    try:
        fighter_a = db_get_fighter(req.hero_id)
        fighter_b = db_get_fighter(req.villain_id)
    except Exception:
        logger.exception("Database error while running a Danger Room simulation")
        raise HTTPException(status_code=503, detail="The Danger Room is temporarily unavailable. Please try again.")

    if not fighter_a or not fighter_b:
        raise HTTPException(status_code=404, detail="One or both combatants have no power stats on file.")

    result = resolve_battle(fighter_a, fighter_b)
    result["first_encounter_issue"] = pair["first_encounter_issue"]
    return result


# ---------------------------------------------------------
# ADMIN — power stats & nemesis management for a hero
# (hero CRUD itself lives above; this is what admins use to fuel
# the two games with data for every dossier they create)
# ---------------------------------------------------------

@app.get("/api/heroes/{hero_id}/stats", dependencies=[Depends(require_registered)])
def get_hero_stats(hero_id: int):
    try:
        stats = db_get_power_stats(hero_id)
    except Exception:
        logger.exception("Database error while fetching stats for hero %s", hero_id)
        raise HTTPException(status_code=503, detail="Could not load stats right now. Please try again.")
    return stats or {}


@app.put("/api/heroes/{hero_id}/stats", dependencies=[Depends(require_admin)])
def set_hero_stats(hero_id: int, stats: PowerStatsIn):
    try:
        hero = db_get_hero_by_id(hero_id)
    except Exception:
        logger.exception("Database error while checking hero %s before setting stats", hero_id)
        raise HTTPException(status_code=503, detail="Could not update stats right now. Please try again.")
    if not hero:
        raise HTTPException(status_code=404, detail="Dossier not found")

    try:
        result = db_upsert_power_stats(hero_id, stats.model_dump())
    except Exception:
        logger.exception("Database error while setting stats for hero %s", hero_id)
        raise HTTPException(status_code=500, detail="Could not save stats due to a server error.")
    return result


@app.get("/api/heroes/{hero_id}/nemeses", dependencies=[Depends(require_registered)])
def get_hero_nemeses(hero_id: int):
    try:
        return db_get_nemeses_for_hero(hero_id)
    except Exception:
        logger.exception("Database error while fetching nemeses for hero %s", hero_id)
        raise HTTPException(status_code=503, detail="Could not load nemeses right now. Please try again.")


@app.post("/api/heroes/{hero_id}/nemeses", status_code=201, dependencies=[Depends(require_admin)])
def add_hero_nemesis(hero_id: int, link: NemesisLinkIn):
    if hero_id == link.villain_id:
        raise HTTPException(status_code=400, detail="A hero cannot be their own nemesis.")

    try:
        new_link = db_add_nemesis_link(hero_id, link.villain_id, link.first_encounter_issue)
    except pg_errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="That nemesis pairing already exists.")
    except pg_errors.ForeignKeyViolation:
        raise HTTPException(status_code=404, detail="Hero or villain ID does not exist.")
    except Exception:
        logger.exception("Database error while adding a nemesis link")
        raise HTTPException(status_code=500, detail="Could not add the nemesis due to a server error.")

    return {"message": "Nemesis recorded", "link_id": new_link["link_id"]}


@app.delete("/api/heroes/{hero_id}/nemeses/{villain_id}", dependencies=[Depends(require_admin)])
def remove_hero_nemesis(hero_id: int, villain_id: int):
    try:
        found = db_delete_nemesis_link(hero_id, villain_id)
    except Exception:
        logger.exception("Database error while removing a nemesis link")
        raise HTTPException(status_code=500, detail="Could not remove the nemesis due to a server error.")

    if not found:
        raise HTTPException(status_code=404, detail="That nemesis pairing was not found.")
    return {"message": "Nemesis link removed"}


# ---------------------------------------------------------
# INTEL REPORTS — a logged channel for Agents (registered users) and Admins.
# Guests have none of this; it is the registered tier's actual perk
# beyond just viewing full dossiers.
# ---------------------------------------------------------

@app.get("/api/intel-reports", dependencies=[Depends(require_registered)])
def list_intel_reports():
    try:
        return db_list_intel_reports()
    except Exception:
        logger.exception("Database error while listing intel reports")
        raise HTTPException(status_code=503, detail="Could not load intel reports right now. Please try again.")


@app.post("/api/intel-reports", status_code=201)
def file_intel_report(report: IntelReportCreate, user: dict = Depends(require_registered)):
    try:
        hero = db_get_hero_by_id(report.hero_id)
    except Exception:
        logger.exception("Database error while checking hero %s before filing intel", report.hero_id)
        raise HTTPException(status_code=503, detail="Could not file this report right now. Please try again.")
    if not hero:
        raise HTTPException(status_code=404, detail="That hero does not exist.")

    try:
        new_report = db_create_intel_report(report.hero_id, user["id"], report.threat_status, report.report_text)
    except Exception:
        logger.exception("Database error while filing an intel report")
        raise HTTPException(status_code=500, detail="Could not file the report due to a server error.")

    return {"message": "Intel report filed", "report_id": new_report["report_id"]}


@app.put("/api/intel-reports/{report_id}")
def edit_intel_report(report_id: int, update: IntelReportUpdate, user: dict = Depends(require_registered)):
    fields = update.model_dump(exclude_unset=True, exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided for update")

    try:
        existing = db_get_intel_report(report_id)
    except Exception:
        logger.exception("Database error while fetching intel report %s", report_id)
        raise HTTPException(status_code=503, detail="Could not update this report right now. Please try again.")

    if not existing:
        raise HTTPException(status_code=404, detail="Intel report not found")
    if user["role"] != "admin" and existing["agent_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="You can only edit intel you filed yourself.")

    try:
        db_update_intel_report(report_id, fields)
    except Exception:
        logger.exception("Database error while updating intel report %s", report_id)
        raise HTTPException(status_code=500, detail="Could not update the report due to a server error.")

    return {"message": "Intel report updated"}


@app.delete("/api/intel-reports/{report_id}")
def remove_intel_report(report_id: int, user: dict = Depends(require_registered)):
    try:
        existing = db_get_intel_report(report_id)
    except Exception:
        logger.exception("Database error while fetching intel report %s", report_id)
        raise HTTPException(status_code=503, detail="Could not delete this report right now. Please try again.")

    if not existing:
        raise HTTPException(status_code=404, detail="Intel report not found")
    if user["role"] != "admin" and existing["agent_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="You can only delete intel you filed yourself.")

    try:
        db_delete_intel_report(report_id)
    except Exception:
        logger.exception("Database error while deleting intel report %s", report_id)
        raise HTTPException(status_code=500, detail="Could not delete the report due to a server error.")

    return {"message": "Intel report deleted"}