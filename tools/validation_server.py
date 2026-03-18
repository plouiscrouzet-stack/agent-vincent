"""Serveur webhook de validation — reçoit les clics Valider/Refuser des emails de validation.

Usage:
    python3 tools/validation_server.py              # port 5001 par défaut
    python3 tools/validation_server.py --port 5001

Puis exposer publiquement avec ngrok :
    ngrok http 5001
    → copier l'URL https dans VALIDATION_BASE_URL du .env
"""

import sys
import os
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

try:
    from flask import Flask, jsonify, abort
except ImportError:
    print("❌ Flask manquant — installe avec : pip install flask")
    sys.exit(1)

from lib.plusvibe import PlusVibeClient

app = Flask(__name__)
TMP_DIR = Path(__file__).parent.parent / ".tmp"


def load_pending(email_id: str) -> dict:
    path = TMP_DIR / f"pending_{email_id}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def mark_done(email_id: str, action: str):
    path = TMP_DIR / f"pending_{email_id}.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        data["validation_action"] = action
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Pages de résultat ────────────────────────────────────────────────────────

def _page(title: str, color: str, emoji: str, message: str, detail: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{title}</title>
<style>body{{font-family:-apple-system,sans-serif;background:#f9fafb;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{background:#fff;border-radius:16px;padding:48px 40px;text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.08);max-width:440px}}
.icon{{font-size:56px;margin-bottom:16px}}.title{{font-size:24px;font-weight:700;color:{color};margin:0 0 8px}}
.msg{{color:#6b7280;font-size:15px;line-height:1.5;margin:0 0 16px}}.detail{{font-size:12px;color:#9ca3af;font-family:monospace;background:#f3f4f6;padding:8px 12px;border-radius:6px;word-break:break-all}}</style>
</head><body>
<div class="card">
  <div class="icon">{emoji}</div>
  <p class="title">{title}</p>
  <p class="msg">{message}</p>
  {"<p class='detail'>" + detail + "</p>" if detail else ""}
</div>
</body></html>"""


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/validate/<email_id>/approve")
def approve(email_id):
    pending = load_pending(email_id)
    if not pending:
        return _page("Introuvable", "#dc2626", "🔍",
                     "Aucune réponse en attente trouvée pour cet ID.",
                     f"Email ID: {email_id}"), 404

    if pending.get("validation_action"):
        action = pending["validation_action"]
        return _page("Déjà traité", "#6366f1", "ℹ️",
                     f"Cet email a déjà été traité : {action}."), 200

    # Envoyer via PlusVibe
    pv = PlusVibeClient()
    try:
        subject = pending["subject"]
        if not subject.startswith("Re:"):
            subject = f"Re: {subject}"

        send_result = pv.reply_to_email(
            workspace_id=pending["workspace_id"],
            reply_to_id=pending["email_id"],
            subject=subject,
            from_email=pending["eaccount"],
            to_email=pending["from_email"],
            body=pending["reply_text"],
        )
        mark_done(email_id, "APPROVED")
        print(f"✅ Envoyé via PlusVibe — ID: {send_result.get('id', '?')}")
        return _page(
            "Réponse envoyée !",
            "#16a34a", "✅",
            f"La réponse a bien été envoyée à {pending['from_email']} via PlusVibe.",
            f"PlusVibe message ID: {send_result.get('id', '?')}"
        )
    except Exception as e:
        print(f"❌ Erreur PlusVibe: {e}")
        return _page("Erreur d'envoi", "#dc2626", "❌",
                     f"Impossible d'envoyer via PlusVibe.",
                     str(e)), 500
    finally:
        pv.close()


@app.route("/validate/<email_id>/reject")
def reject(email_id):
    pending = load_pending(email_id)
    if not pending:
        return _page("Introuvable", "#dc2626", "🔍",
                     "Aucune réponse en attente trouvée pour cet ID.",
                     f"Email ID: {email_id}"), 404

    mark_done(email_id, "REJECTED")
    print(f"🚫 Refusé — {pending.get('from_email', email_id)}")
    return _page(
        "Réponse refusée",
        "#f59e0b", "🚫",
        f"La réponse à {pending.get('from_email', '?')} a été refusée et ne sera pas envoyée."
    )


@app.route("/status")
def status():
    """Dashboard : liste toutes les validations en attente."""
    pending_files = list(TMP_DIR.glob("pending_*.json"))
    items = []
    for f in sorted(pending_files, key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            items.append({
                "email_id": data.get("email_id"),
                "from": data.get("from_email"),
                "category": data.get("classification", {}).get("category"),
                "action": data.get("validation_action", "PENDING"),
            })
        except Exception:
            pass
    return jsonify({"pending": items})


@app.route("/")
def index():
    base_url = os.getenv("VALIDATION_BASE_URL", "http://localhost:5001")
    return _page(
        "Serveur de validation actif",
        "#6366f1", "🤖",
        f"Agent Vincent — Webhook de validation opérationnel.",
        f"Base URL: {base_url}"
    )


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()

    TMP_DIR.mkdir(exist_ok=True)
    base_url = os.getenv("VALIDATION_BASE_URL", f"http://localhost:{args.port}")
    print(f"🚀 Serveur de validation démarré")
    print(f"   URL locale : http://localhost:{args.port}")
    print(f"   VALIDATION_BASE_URL : {base_url}")
    print(f"   Pour exposer publiquement : ngrok http {args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False)
