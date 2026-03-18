"""Abstraction pour le stockage d'état.

En production (Vercel) : utilise Vercel KV (Upstash Redis).
En local : fallback sur des fichiers JSON dans .tmp/.

Vercel KV injecte automatiquement KV_REST_API_URL et KV_REST_API_TOKEN
quand une base KV est liée au projet dans le dashboard Vercel.
"""

import os
import json
from pathlib import Path

TTL_7_DAYS = 86400 * 7

# Sur Vercel, /var/task/ est en lecture seule — seul /tmp/ est inscriptible.
# En local, on utilise .tmp/ dans le projet.
_LOCAL_TMP = Path(__file__).parent.parent / ".tmp"
_VERCEL_TMP = Path("/tmp/agent-vincent")
TMP_DIR = _VERCEL_TMP if os.getenv("VERCEL") else _LOCAL_TMP


def _get_redis():
    """Retourne un client Redis Upstash si les variables Vercel KV sont présentes."""
    url = os.getenv("KV_REST_API_URL")
    token = os.getenv("KV_REST_API_TOKEN")
    if url and token:
        try:
            from upstash_redis import Redis
            return Redis(url=url, token=token)
        except ImportError:
            pass
    return None


# ── Pending emails (en attente de validation) ─────────────────────────────────

def save_pending(email_id: str, data: dict):
    """Sauvegarde le contexte d'un email en attente de validation."""
    redis = _get_redis()
    if redis:
        redis.set(
            f"pending:{email_id}",
            json.dumps(data, ensure_ascii=False, default=str),
            ex=TTL_7_DAYS,
        )
    else:
        TMP_DIR.mkdir(exist_ok=True)
        (TMP_DIR / f"pending_{email_id}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )


def load_pending(email_id: str) -> dict | None:
    """Charge le contexte d'un email en attente."""
    redis = _get_redis()
    if redis:
        val = redis.get(f"pending:{email_id}")
        return json.loads(val) if val else None
    else:
        path = TMP_DIR / f"pending_{email_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def mark_done(email_id: str, action: str):
    """Marque un email comme traité (APPROVED ou REJECTED)."""
    data = load_pending(email_id)
    if not data:
        return
    data["validation_action"] = action
    redis = _get_redis()
    if redis:
        redis.set(
            f"pending:{email_id}",
            json.dumps(data, ensure_ascii=False, default=str),
            ex=TTL_7_DAYS,
        )
    else:
        path = TMP_DIR / f"pending_{email_id}.json"
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )


def list_pending() -> list[dict]:
    """Liste tous les emails en attente (pour le dashboard /status)."""
    redis = _get_redis()
    if redis:
        keys = redis.keys("pending:*")
        items = []
        for key in (keys or []):
            val = redis.get(key)
            if val:
                try:
                    items.append(json.loads(val))
                except Exception:
                    pass
        return sorted(items, key=lambda x: x.get("email_id", ""), reverse=True)
    else:
        items = []
        for f in sorted(TMP_DIR.glob("pending_*.json"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                items.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
        return items


# ── IDs traités (anti-doublon) ─────────────────────────────────────────────────

def is_processed(email_id: str) -> bool:
    """Vérifie si un email a déjà été traité."""
    redis = _get_redis()
    if redis:
        return bool(redis.sismember("processed_ids", email_id))
    else:
        path = TMP_DIR / "processed_ids.json"
        if not path.exists():
            return False
        return email_id in json.loads(path.read_text(encoding="utf-8"))


def mark_processed(email_id: str):
    """Marque un email comme traité."""
    redis = _get_redis()
    if redis:
        redis.sadd("processed_ids", email_id)
    else:
        TMP_DIR.mkdir(exist_ok=True)
        path = TMP_DIR / "processed_ids.json"
        ids = set()
        if path.exists():
            ids = set(json.loads(path.read_text(encoding="utf-8")))
        ids.add(email_id)
        path.write_text(json.dumps(list(ids), indent=2), encoding="utf-8")
