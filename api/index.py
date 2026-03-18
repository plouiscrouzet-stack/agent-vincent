"""Serveur Flask pour Vercel — validation + webhook PlusVibe.

Routes :
  GET  /                              → page de statut
  GET  /validate/<email_id>/approve   → valider et envoyer via PlusVibe
  GET  /validate/<email_id>/reject    → refuser
  GET  /status                        → liste JSON des emails en attente
  POST /webhook                       → reçoit les nouveaux emails depuis PlusVibe
"""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from flask import Flask, request, jsonify
from lib.kv_store import load_pending, mark_done, list_pending
from lib.plusvibe import PlusVibeClient

app = Flask(__name__)


# ── Pages HTML ────────────────────────────────────────────────────────────────

def _page(title: str, color: str, emoji: str, message: str, detail: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
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


# ── Routes validation ─────────────────────────────────────────────────────────

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

    # Marquer immédiatement comme en cours pour éviter le double-clic
    mark_done(email_id, "APPROVING")

    pv = PlusVibeClient()
    try:
        subject = pending["subject"] or ""
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
            f"PlusVibe message ID: {send_result.get('id', '?')}",
        )
    except Exception as e:
        print(f"❌ Erreur PlusVibe: {e}")
        # Remettre en attente pour permettre un nouvel essai
        mark_done(email_id, None)
        return _page("Erreur d'envoi", "#dc2626", "❌",
                     "Impossible d'envoyer via PlusVibe. Vous pouvez réessayer.", str(e)), 500
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
        f"La réponse à {pending.get('from_email', '?')} a été refusée et ne sera pas envoyée.",
    )


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/status")
def status():
    items = []
    for data in list_pending():
        items.append({
            "email_id": data.get("email_id"),
            "from": data.get("from_email"),
            "category": data.get("classification", {}).get("category"),
            "action": data.get("validation_action", "PENDING"),
        })
    return jsonify({"pending": items})


# ── Webhook PlusVibe ──────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """Reçoit un nouvel email depuis PlusVibe et déclenche le pipeline."""
    payload = request.get_json(silent=True) or {}
    print(f"📩 Webhook reçu: {payload}")

    try:
        from lib.plusvibe import PlusVibeClient, Email
        from lib.perplexity import PerplexityClient
        from lib.config import get_config
        from lib.pipeline import process_email
        from lib.kv_store import is_processed, mark_processed

        # Extraire les données de l'email depuis le payload PlusVibe
        # Le webhook PlusVibe envoie les champs directement à la racine
        # avec des noms différents de l'API Unibox
        email_raw = payload.get("data", payload)
        workspace_id = (
            email_raw.get("workspace_id")
            or os.getenv("DEFAULT_WORKSPACE_ID", "6999f3922e0a8f7dc9258774")
        )

        # Parser l'email — adapter les champs webhook → format interne
        pv = PlusVibeClient()
        email = pv.parse_webhook_email(email_raw)

        if not email.id:
            return jsonify({"status": "ignored", "reason": "no email id"}), 200

        # Anti-doublon
        if is_processed(email.id):
            return jsonify({"status": "ignored", "reason": "already processed"}), 200

        # Ne traiter que les emails entrants
        if email.direction != "IN":
            return jsonify({"status": "ignored", "reason": "not inbound"}), 200

        # Marquer comme traité AVANT le pipeline (évite les doublons
        # si PlusVibe envoie 2 webhooks rapprochés pendant le traitement)
        mark_processed(email.id)

        config = get_config(workspace_id)
        if not config:
            return jsonify({"status": "error", "reason": f"no config for workspace {workspace_id}"}), 400

        ppx = PerplexityClient()
        try:
            result = process_email(email, config, pv, ppx)
            return jsonify({"status": "ok", "action": result.get("action")}), 200
        finally:
            ppx.close()
            pv.close()

    except Exception as e:
        import traceback
        print(f"❌ Erreur webhook: {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Root ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    base_url = os.getenv("VERCEL_URL", os.getenv("VALIDATION_BASE_URL", "localhost"))
    return _page(
        "Agent Vincent — opérationnel",
        "#6366f1", "🤖",
        "Webhook de validation actif.",
        f"URL: {base_url}",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
