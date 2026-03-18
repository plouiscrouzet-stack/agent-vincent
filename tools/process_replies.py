"""Pipeline principal : récupère les emails non traités et génère des réponses.

Usage:
    python3 tools/process_replies.py                    # Mode validation (email HTML)
    python3 tools/process_replies.py --send              # Envoie les réponses directement
    python3 tools/process_replies.py --email-id ID       # Traite un email spécifique
"""

import sys
import os
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from lib.plusvibe import PlusVibeClient
from lib.perplexity import PerplexityClient
from lib.config import load_all_configs, get_config
from lib.pipeline import process_email
from lib.kv_store import is_processed, mark_processed


def main():
    parser = argparse.ArgumentParser(description="Traitement des réponses email")
    parser.add_argument("--send", action="store_true", help="Envoyer les réponses (sinon mode validation)")
    parser.add_argument("--email-id", help="Traiter un email spécifique par ID")
    parser.add_argument("--workspace", default="6999f3922e0a8f7dc9258774",
                        help="Workspace ID (défaut: RightLiens)")
    args = parser.parse_args()

    load_all_configs()
    config = get_config(args.workspace)
    if not config:
        print(f"❌ Aucune config trouvée pour le workspace {args.workspace}")
        return

    print(f"🏢 Client: {config['client_name']}")
    print(f"{'='*60}")

    pv = PlusVibeClient()
    ppx = PerplexityClient()

    try:
        if args.email_id:
            emails, _ = pv.get_emails(args.workspace)
            email = next((e for e in emails if e.id == args.email_id), None)
            if not email:
                all_emails = pv.get_all_emails(args.workspace, max_pages=5)
                email = next((e for e in all_emails if e.id == args.email_id), None)
            if not email:
                print(f"❌ Email {args.email_id} introuvable")
                return
            process_email(email, config, pv, ppx, send=args.send)
            return

        # Récupérer tous les emails
        all_emails = pv.get_all_emails(args.workspace, max_pages=10)
        inbound = [e for e in all_emails if e.direction == "IN"]

        if not inbound:
            print("📭 Aucun email entrant à traiter")
            return

        already_processed = sum(1 for e in inbound if is_processed(e.id))
        print(f"📬 {len(inbound)} email(s) entrant(s) trouvé(s) ({already_processed} déjà traités)")

        results = []
        for email in inbound:
            if is_processed(email.id):
                continue

            # Skip si déjà répondu dans le thread
            thread_emails = sorted(
                [e for e in all_emails if e.thread_id == email.thread_id],
                key=lambda e: e.created_at or ""
            )
            idx = next((i for i, e in enumerate(thread_emails) if e.id == email.id), -1)
            already_replied = (idx >= 0 and idx < len(thread_emails) - 1
                               and thread_emails[idx + 1].direction == "OUT")
            if already_replied:
                print(f"\n⏭️  Déjà répondu à {email.from_email} (thread {email.thread_id})")
                mark_processed(email.id)
                continue

            result = process_email(email, config, pv, ppx, send=args.send)
            results.append(result)
            mark_processed(email.id)

        print(f"\n{'='*60}")
        print(f"📊 RÉSUMÉ: {len(results)} email(s) traité(s)")
        for r in results:
            print(f"  - {r['from']}: {r['classification'].get('category', '?')} → {r['action']}")

        # Sauvegarder les résultats localement
        tmp_dir = Path(__file__).parent.parent / ".tmp"
        tmp_dir.mkdir(exist_ok=True)
        output_path = tmp_dir / "results.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n💾 Résultats sauvegardés → {output_path}")

    finally:
        pv.close()
        ppx.close()


if __name__ == "__main__":
    main()
