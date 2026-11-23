import logging
import os
import re
from typing import Optional

import psycopg2
from psycopg2 import errors as pg_errors
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# Load environment variables (Make sure you have a .env file with DATABASE_URL)
load_dotenv()

logger = logging.getLogger("omniverse_api")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Omniverse API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows your frontend to connect locally
    allow_credentials=True,
    allow_methods=["*"],  # Allows GET, POST, PUT, DELETE, and OPTIONS
    allow_headers=["*"],
)

# ---------------------------------------------------------
# DATA ACCESS LAYER
# Every raw SQL statement lives here so endpoint handlers stay
# focused on request/response shaping and permission checks.
# ---------------------------------------------------------

def get_db_connection():
    try:
        conn = psycopg2.connect(
            os.getenv("DATABASE_URL"),
            cursor_factory=RealDictCursor
        )
        return conn
    except Exception:
        logger.exception("Database connection error")
        raise HTTPException(status_code=503, detail="Database connection failed. Please try again later.")


def run_query(query, params=None, fetch=None, commit=False):
    """
    Runs one SQL statement against a fresh connection.

    fetch: None | "one" | "all"
    commit: whether to commit (needed for INSERT/UPDATE/DELETE)

    Raises the original psycopg2 exception on failure (including
    UniqueViolation) so the calling endpoint can decide the right
    HTTP status code. Never leaks raw DB text to the caller itself.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(query, params or ())
        result = None
        if fetch == "one":
            result = cursor.fetchone()
        elif fetch == "all":
            result = cursor.fetchall()
        if commit:
            conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def db_get_comic_count():
    row = run_query("SELECT COUNT(*) FROM comic_archive;", fetch="one")
    return row["count"]


def db_get_all_heroes():
    return run_query(
        "SELECT id, alias, publisher, affiliation, origin_era, image, civilian_name "
        "FROM comic_archive ORDER BY id ASC;",
        fetch="all",
    )


def db_get_hero_by_id(hero_id: int):
    return run_query("SELECT * FROM comic_archive WHERE id = %s;", (hero_id,), fetch="one")


def db_search_heroes(q: Optional[str], publisher: Optional[str], alignment: Optional[str]):
    query = "SELECT * FROM comic_archive WHERE 1=1"
    params = []
    if q:
        query += " AND (alias ILIKE %s OR civilian_name ILIKE %s)"
        params.extend([f"%{q}%", f"%{q}%"])
    if publisher:
        query += " AND publisher ILIKE %s"
        params.append(f"%{publisher}%")
    if alignment:
        query += " AND alignment ILIKE %s"
        params.append(f"%{alignment}%")
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
    return run_query(
        "SELECT username, role FROM users WHERE api_key = %s;", (api_key,), fetch="one"
    )


def db_get_user_by_credentials(username: str, password: str):
    # NOTE: passwords are compared in plaintext here, by project decision.
    # If you revisit this later, the standard fix is to store a bcrypt hash
    # in `password` and compare with bcrypt.checkpw() instead of SQL equality.
    return run_query(
        "SELECT username, role, api_key FROM users WHERE username = %s AND password = %s;",
        (username, password),
        fetch="one",
    )


# ---------------------------------------------------------
# AUTHENTICATION & SECURITY (3-TIER SYSTEM)
# ---------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


# 1. Base Security (Allows Admins, Users, and Guests)
def get_current_user(x_api_key: str = Header(None)):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Unauthorized: Missing Clearance Key")

    # Hardcoded Guest bypass — intentional: this is the public, read-only tier
    # and is not a real account, so it is not stored in `users`.
    if x_api_key == "public_guest_clearance":
        return {"username": "Civilian_Guest", "role": "guest"}

    try:
        user = db_get_user_by_api_key(x_api_key)
    except Exception:
        logger.exception("Database error while validating API key")
        raise HTTPException(status_code=503, detail="Could not verify credentials right now. Please try again.")

    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid Clearance Key")
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


@app.post("/api/login")
def login(req: LoginRequest):
    try:
        user = db_get_user_by_credentials(req.username, req.password)
    except Exception:
        logger.exception("Database error during login")
        raise HTTPException(status_code=503, detail="Login is temporarily unavailable. Please try again.")

    if not user:
        raise HTTPException(status_code=401, detail="Invalid Credentials")

    return {"api_key": user["api_key"], "role": user["role"]}


# ---------------------------------------------------------
# SECURE PYDANTIC VALIDATION MODELS
# ---------------------------------------------------------

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

    # Security Sanitizer: Strips trailing spaces and prevents basic XSS injection
    @field_validator('*', mode='before')
    def sanitize_strings(cls, value):
        if isinstance(value, str):
            cleaned = value.strip()
            if re.search(r'<script.*?>.*?</script>', cleaned, re.IGNORECASE):
                raise ValueError("Invalid characters or script tags detected.")
            return cleaned
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
# NEON DATABASE CRUD ENDPOINTS
# ---------------------------------------------------------

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