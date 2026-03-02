"""
JWT authentication and SQLite-backed user/key management.

Supports:
- User registration (email + password hash)
- API key creation and validation
- Usage tracking (per-user score count)
- Tier enforcement (free: 5 total scores, pro: unlimited)
"""

import hashlib
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

import jwt

from cloud.config import settings


# =============================================================================
# DATABASE
# =============================================================================

def _get_db_path() -> str:
    import os
    db_path = settings.DB_PATH
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    return db_path


def init_db():
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                tier TEXT NOT NULL DEFAULT 'free',
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash TEXT UNIQUE NOT NULL,
                key_prefix TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                label TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_used_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL,
                scored_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
            CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log(user_id);
        """)


@contextmanager
def get_db():
    """Context manager for SQLite connections."""
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =============================================================================
# USER MANAGEMENT
# =============================================================================

def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}:{h.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    salt, h = stored_hash.split(":")
    computed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return computed.hex() == h


def _validate_email(email: str) -> str:
    """Validate and normalize email. Raises ValueError if invalid."""
    email = email.lower().strip()
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
        raise ValueError("Invalid email format.")
    return email


def create_user(email: str, password: str) -> Dict[str, Any]:
    """Register a new user. Returns user dict with id."""
    email = _validate_email(email)
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    pw_hash = _hash_password(password)
    with get_db() as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                (email.lower().strip(), pw_hash),
            )
            user_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError(f"Email already registered: {email}")

    return {"id": user_id, "email": email.lower().strip(), "tier": "free"}


def authenticate_user(email: str, password: str) -> Optional[Dict[str, Any]]:
    """Verify credentials. Returns user dict or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, tier FROM users WHERE email = ?",
            (email.lower().strip(),),
        ).fetchone()

    if not row or not _verify_password(password, row["password_hash"]):
        return None

    return {"id": row["id"], "email": row["email"], "tier": row["tier"]}


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, email, tier, stripe_customer_id, stripe_subscription_id FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return dict(row)


def get_user_by_stripe_customer_id(stripe_customer_id: str) -> Optional[Dict[str, Any]]:
    """Look up a user by their Stripe customer ID (for webhook-driven downgrades)."""
    if not stripe_customer_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, email, tier, stripe_customer_id, stripe_subscription_id FROM users WHERE stripe_customer_id = ?",
            (stripe_customer_id,),
        ).fetchone()
    if not row:
        return None
    return dict(row)


def update_user_tier(user_id: int, tier: str, stripe_customer_id: str = None, stripe_subscription_id: str = None):
    """Upgrade or downgrade user tier."""
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET tier = ?, stripe_customer_id = ?, stripe_subscription_id = ?, updated_at = datetime('now') WHERE id = ?",
            (tier, stripe_customer_id, stripe_subscription_id, user_id),
        )


# =============================================================================
# API KEY MANAGEMENT
# =============================================================================

def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_api_key(user_id: int, label: str = "") -> str:
    """Generate a new API key for a user. Returns the raw key (only shown once)."""
    raw_key = f"rb_{secrets.token_hex(24)}"
    key_hash = _hash_api_key(raw_key)
    key_prefix = raw_key[:10]

    with get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, label) VALUES (?, ?, ?, ?)",
            (key_hash, key_prefix, user_id, label),
        )

    return raw_key


def validate_api_key(raw_key: str) -> Optional[Dict[str, Any]]:
    """
    Validate an API key and return user info.
    Returns None if invalid or inactive.
    """
    key_hash = _hash_api_key(raw_key)
    with get_db() as conn:
        row = conn.execute(
            """SELECT ak.user_id, ak.is_active, u.email, u.tier
               FROM api_keys ak JOIN users u ON ak.user_id = u.id
               WHERE ak.key_hash = ?""",
            (key_hash,),
        ).fetchone()

        if not row or not row["is_active"]:
            return None

        # Update last_used_at
        conn.execute(
            "UPDATE api_keys SET last_used_at = datetime('now') WHERE key_hash = ?",
            (key_hash,),
        )

    return {
        "user_id": row["user_id"],
        "email": row["email"],
        "tier": row["tier"],
    }


def revoke_api_key(raw_key: str) -> bool:
    key_hash = _hash_api_key(raw_key)
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_hash = ?", (key_hash,)
        )
    return cursor.rowcount > 0


# =============================================================================
# ANONYMOUS USER MANAGEMENT
# =============================================================================

def get_or_create_anonymous_user(fingerprint: str) -> Dict[str, Any]:
    """
    Get or create an anonymous user by device fingerprint (hashed IP).

    Anonymous users have email = 'anon:<fingerprint>' and a random password.
    They get the same free tier limits as registered users.
    """
    anon_email = f"anon:{fingerprint}"
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, email, tier FROM users WHERE email = ?",
            (anon_email,),
        ).fetchone()
        if row:
            return {"id": row["id"], "email": row["email"], "tier": row["tier"]}

        # Create anonymous user
        pw_hash = _hash_password(secrets.token_hex(16))
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, tier) VALUES (?, ?, 'free')",
            (anon_email, pw_hash),
        )
        return {"id": cursor.lastrowid, "email": anon_email, "tier": "free"}


# =============================================================================
# USAGE TRACKING
# =============================================================================

def log_usage(user_id: int, endpoint: str):
    """Record a scoring request."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO usage_log (user_id, endpoint) VALUES (?, ?)",
            (user_id, endpoint),
        )


def get_usage_count(user_id: int) -> int:
    """Total lifetime scores for a user."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM usage_log WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row["cnt"] if row else 0


def check_usage_allowed(user_id: int, tier: str) -> bool:
    """Check if user is within their tier limits."""
    if tier == "pro":
        return True
    # Free tier: limited total scores
    count = get_usage_count(user_id)
    return count < settings.FREE_TIER_TOTAL_LIMIT


def get_usage_stats(user_id: int) -> Dict[str, Any]:
    """Get detailed usage stats for a user."""
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM usage_log WHERE user_id = ?",
            (user_id,),
        ).fetchone()["cnt"]

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM usage_log WHERE user_id = ? AND scored_at >= ?",
            (user_id, today),
        ).fetchone()["cnt"]

    return {
        "total_scores": total,
        "today_scores": today_count,
        "tier_limit": None if get_user_by_id(user_id).get("tier") == "pro" else settings.FREE_TIER_TOTAL_LIMIT,
        "remaining": None if get_user_by_id(user_id).get("tier") == "pro" else max(0, settings.FREE_TIER_TOTAL_LIMIT - total),
    }


# =============================================================================
# JWT TOKENS
# =============================================================================

def create_jwt_token(user_id: int, email: str, tier: str) -> str:
    """Create a JWT access token."""
    payload = {
        "sub": str(user_id),
        "email": email,
        "tier": tier,
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_jwt_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode and validate a JWT token. Returns payload dict or None.

    Note: 'sub' is stored as string per JWT spec, but returned as int for convenience.
    """
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
        # Convert sub back to int for internal use
        payload["sub"] = int(payload["sub"])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# =============================================================================
# INITIALIZATION
# =============================================================================

# Auto-init DB on import
init_db()
