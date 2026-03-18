"""Pipeline principal : récupère les emails non traités et génère des réponses.

Usage:
    python3 tools/process_replies.py                    # Mode dry-run (pas d'envoi)
    python3 tools/process_replies.py --send              # Envoie les réponses
    python3 tools/process_replies.py --email-id ID       # Traite un email spécifique
"""

import sys
import os
import json
import argparse
from pathlib import Path

# Ajouter le répertoire racine au path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from lib.plusvibe import PlusVibeClient, Email
from lib.perplexity import PerplexityClient
from lib.classifier import classify_reply
from lib.qualifier import qualify_lead
from lib.responder import generate_reply
from lib.config import load_all_configs, get_config
from lib.gmail_sender import send_validation_email


def clean_html(text: str) -> str:
    """Nettoie le HTML basique d'un texte."""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                          ("&gt;", ">"), ("&eacute;", "é"), ("&egrave;", "è"),
                          ("&agrave;", "à"), ("&ecirc;", "ê"), ("&rsquo;", "'"),
                          ("&Ecirc;", "Ê"), ("&Eacute;", "É")]:
        text = text.replace(entity, char)
    return text


def extract_quoted_email(body_text: str) -> str:
    """Extrait l'email original cité (lignes commençant par '>') d'une réponse."""
    quoted_lines = []
    in_quoted = False
    for line in body_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(">"):
            in_quoted = True
            quoted_lines.append(stripped.lstrip("> ").strip())
        elif in_quoted and not stripped:
            quoted_lines.append("")  # conserver les sauts de ligne dans le cité
    return "\n".join(quoted_lines).strip()


def format_thread(emails: list[Email]) -> str:
    """Formate un thread d'emails pour le prompt.

    Utilise le body_text complet (pas content_preview tronqué).
    Si un email entrant contient un email cité ('>'), le reconstruit
    comme email sortant pour que Claude ait tout le contexte.
    """
    # Reconstruire les emails envoyés depuis le texte cité dans les réponses reçues
    reconstructed = []
    for e in emails:
        reconstructed.append(e)
        if e.direction == "IN":
            full_text = e.body_text or e.content_preview or ""
            quoted = extract_quoted_email(full_text)
            if quoted and len(quoted) > 20:
                # Créer un email "virtuel" OUT pour représenter l'email original
                from lib.plusvibe import Email as PVEmail
                fake_out = PVEmail(
                    id=f"quoted_{e.id}",
                    thread_id=e.thread_id,
                    subject=e.subject,
                    body_text=quoted,
                    direction="OUT",
                    from_email=e.eaccount or "eric (RightLiens)",
                    to_email=e.lead_email or e.from_email,
                    created_at=None,
                )
                reconstructed.insert(len(reconstructed) - 1, fake_out)

    lines = []
    for e in reconstructed:
        direction = "→ ENVOYÉ" if e.direction == "OUT" else "← REÇU"
        is_reconstructed = e.id and e.id.startswith("quoted_")
        label = "[RECONSTITUÉ DEPUIS LE CITÉ]" if is_reconstructed else ""

        # Préférer body_text (complet) à content_preview (tronqué)
        text = e.body_text or e.content_preview or e.body_html or "(vide)"
        if "<" in text and ">" in text:
            text = clean_html(text)

        # Pour les IN, ne garder que la partie avant les lignes citées
        if e.direction == "IN" and e.body_text:
            non_quoted = []
            for line in e.body_text.splitlines():
                if line.lstrip().startswith(">"):
                    break
                non_quoted.append(line)
            text = "\n".join(non_quoted).strip() or text

        lines.append(f"[{direction}] {label} {e.from_email} ({e.created_at or 'date inconnue'})")
        lines.append(f"Sujet: {e.subject or '(sans sujet)'}")
        lines.append(text.strip()[:3000])
        lines.append("---")
    return "\n".join(lines)


def extract_lead_info(email: Email) -> dict:
    """Extrait les infos de base du lead depuis l'email."""
    lead_email = email.lead_email or email.from_email or ""
    domain = ""
    name = ""
    if "@" in lead_email:
        domain = lead_email.split("@")[1]

    # Tenter d'extraire le nom depuis le content_preview ou from
    from_raw = email.raw.get("from_address_json", []) if email.raw else []
    if from_raw and isinstance(from_raw, list) and from_raw[0].get("name"):
        name = from_raw[0]["name"]

    return {
        "email": lead_email,
        "name": name,
        "company": domain,
    }


def process_email(email: Email, client_config: dict, pv: PlusVibeClient,
                  ppx: PerplexityClient, send: bool = False) -> dict:
    """Traite un email entrant : classify → enrich → qualify → respond.

    Returns: dict avec tous les résultats intermédiaires
    """
    ws_id = client_config["workspace_id"]
    result = {
        "email_id": email.id,
        "from": email.from_email,
        "subject": email.subject,
        "lead_email": email.lead_email,
        "plusvibe_label": email.label,
        "campaign_id": email.campaign_id,
    }

    # 1. Texte du message
    message_text = email.content_preview or email.body_text or ""
    if not message_text and email.body_html:
        import re
        message_text = re.sub(r"<[^>]+>", "", email.body_html)
    message_text = message_text.strip()[:2000]

    print(f"\n{'='*60}")
    print(f"📧 Email de: {email.from_email}")
    print(f"   Sujet: {email.subject}")
    print(f"   Preview: {message_text[:150]}...")

    # 2. Classification
    print(f"\n🏷️  Classification en cours...")
    classification = classify_reply(message_text)
    category = classification.get("category", "UNKNOWN")
    result["classification"] = classification
    print(f"   → {category}: {classification.get('reason', '')}")

    # Actions selon la catégorie — on ne répond QUE s'il y a une opportunité réelle
    if category == "OUT_OF_OFFICE":
        print("   ⏭️  Out of office — aucune action")
        result["action"] = "SKIP_OOO"
        return result

    if category == "AUTO_REPLY":
        print("   🤖 Réponse automatique — aucune action")
        result["action"] = "SKIP_AUTO_REPLY"
        return result

    if category == "NOT_INTERESTED":
        print("   👋 Pas intéressé — on s'arrête, pas de réponse")
        result["action"] = "SKIP_NOT_INTERESTED"
        return result

    if category == "UNSUBSCRIBE":
        print("   🚫 Désinscription — aucune réponse")
        result["action"] = "SKIP_UNSUBSCRIBE"
        return result

    if category == "HOSTILE":
        print("   ⚠️  Message hostile — flag pour review humaine")
        result["action"] = "FLAG_HOSTILE"
        return result

    # Seuls INTERESTED, QUESTION, MEETING_CONFIRMED arrivent ici

    # 3. Enrichissement Perplexity (ciblé sur les critères de qualification)
    lead_info = extract_lead_info(email)
    print(f"\n🔍 Recherche Perplexity sur {lead_info['email']}...")
    try:
        sectors = client_config.get("qualification_criteria", {}).get("sectors", [])
        perplexity_data = ppx.research_lead(
            lead_name=lead_info["name"],
            lead_email=lead_info["email"],
            company_name=lead_info["company"],
            qualification_criteria=sectors,
        )
        result["perplexity"] = perplexity_data
        company = perplexity_data.get("company_name", "?")
        sector = perplexity_data.get("sector", "?")
        print(f"   → Entreprise: {company} | Secteur: {sector}")
    except Exception as e:
        print(f"   ⚠️  Erreur Perplexity: {e}")
        perplexity_data = {"error": str(e)}
        result["perplexity"] = perplexity_data

    # 4. Récupération du thread complet
    print(f"\n📋 Récupération du thread {email.thread_id}...")
    thread_emails = pv.get_thread(ws_id, email.thread_id)
    thread_text = format_thread(thread_emails)
    print(f"   → {len(thread_emails)} messages dans le thread")

    # 5. Qualification
    print(f"\n⚖️  Qualification en cours...")
    qualification = qualify_lead(
        thread_text=thread_text,
        lead_info=lead_info,
        perplexity_data=perplexity_data,
        client_config=client_config,
        plusvibe_label=email.label,
    )
    result["qualification"] = qualification
    print(f"   → Score: {qualification.get('score', '?')} | "
          f"Recommandation: {qualification.get('recommendation', '?')}")

    # 6. Génération de réponse
    print(f"\n✍️  Génération de la réponse...")

    reply_text = generate_reply(
        thread_text=thread_text,
        latest_message=message_text,
        classification=classification,
        qualification=result.get("qualification", {}),
        perplexity_data=result.get("perplexity", {}),
        client_config=client_config,
    )
    result["reply"] = reply_text

    print(f"\n📝 RÉPONSE GÉNÉRÉE:")
    print(f"{'─'*40}")
    print(reply_text)
    print(f"{'─'*40}")

    # 7. Envoi direct PlusVibe (si --send activé)
    if send:
        print(f"\n📤 Envoi de la réponse...")
        try:
            send_result = pv.reply_to_email(
                workspace_id=ws_id,
                reply_to_id=email.id,
                subject=f"Re: {email.subject}" if not email.subject.startswith("Re:") else email.subject,
                from_email=email.eaccount,
                to_email=email.from_email,
                body=reply_text,
            )
            result["send_result"] = send_result
            print(f"   → ✅ Envoyé! ID: {send_result.get('id', '?')}")
        except Exception as e:
            result["send_error"] = str(e)
            print(f"   → ❌ Erreur d'envoi: {e}")
        result["action"] = "REPLIED"
        return result

    # 8. Mode validation : sauvegarde contexte + email HTML avec boutons
    import pathlib
    tmp_dir = pathlib.Path(__file__).parent.parent / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    pending_path = tmp_dir / f"pending_{email.id}.json"
    pending_data = {
        "email_id": email.id,
        "workspace_id": ws_id,
        "from_email": email.from_email,
        "lead_email": email.lead_email,
        "eaccount": email.eaccount,
        "subject": email.subject,
        "reply_text": reply_text,
        "classification": classification,
        "qualification": result.get("qualification", {}),
        "perplexity": result.get("perplexity", {}),
        "plusvibe_label": email.label,
    }
    with open(pending_path, "w", encoding="utf-8") as f:
        json.dump(pending_data, f, indent=2, ensure_ascii=False, default=str)

    validation_to = os.getenv("VALIDATION_EMAIL", "pierre-louis@iautomation.fr")
    print(f"\n📧 Envoi email de validation → {validation_to}...")
    try:
        send_validation_email(
            to_email=validation_to,
            email_id=email.id,
            lead_email=email.lead_email or email.from_email,
            subject=email.subject or "(sans sujet)",
            reply_text=reply_text,
            classification=classification,
            qualification=result.get("qualification", {}),
            perplexity_data=result.get("perplexity", {}),
            label=email.label,
        )
        result["action"] = "VALIDATION_SENT"
    except Exception as e:
        print(f"   ⚠️  Erreur envoi validation: {e}")
        print(f"   → Contexte sauvegardé dans {pending_path}")
        result["action"] = "VALIDATION_FAILED"
        result["validation_error"] = str(e)

    return result


def main():
    parser = argparse.ArgumentParser(description="Traitement des réponses email")
    parser.add_argument("--send", action="store_true", help="Envoyer les réponses (sinon dry-run)")
    parser.add_argument("--email-id", help="Traiter un email spécifique par ID")
    parser.add_argument("--workspace", default="6999f3922e0a8f7dc9258774",
                        help="Workspace ID (défaut: RightLiens)")
    args = parser.parse_args()

    # Charger les configs
    configs = load_all_configs()
    config = get_config(args.workspace)
    if not config:
        print(f"❌ Aucune config trouvée pour le workspace {args.workspace}")
        return

    print(f"🏢 Client: {config['client_name']}")
    print(f"{'='*60}")

    # Initialiser les clients
    pv = PlusVibeClient()
    ppx = PerplexityClient()

    try:
        if args.email_id:
            # Traiter un email spécifique
            emails, _ = pv.get_emails(args.workspace)
            email = next((e for e in emails if e.id == args.email_id), None)
            if not email:
                # Chercher dans les pages suivantes
                all_emails = pv.get_all_emails(args.workspace, max_pages=5)
                email = next((e for e in all_emails if e.id == args.email_id), None)
            if not email:
                print(f"❌ Email {args.email_id} introuvable")
                return
            process_email(email, config, pv, ppx, send=args.send)
        else:
            # Récupérer tous les emails (toutes pages)
            import pathlib as _pathlib
            tmp_dir = _pathlib.Path(__file__).parent.parent / ".tmp"
            tmp_dir.mkdir(exist_ok=True)

            # Charger les IDs déjà traités
            processed_path = tmp_dir / "processed_ids.json"
            processed_ids = set()
            if processed_path.exists():
                processed_ids = set(json.loads(processed_path.read_text(encoding="utf-8")))

            all_emails = pv.get_all_emails(args.workspace, max_pages=10)
            inbound = [e for e in all_emails if e.direction == "IN"]

            if not inbound:
                print("📭 Aucun email entrant à traiter")
                return

            print(f"📬 {len(inbound)} email(s) entrant(s) trouvé(s) ({len(processed_ids)} déjà traités)")

            results = []
            for email in inbound:
                # Skip si déjà traité
                if email.id in processed_ids:
                    continue

                # Skip si on a déjà répondu dans le thread (email OUT après cet email)
                thread_emails = sorted(
                    [e for e in all_emails if e.thread_id == email.thread_id],
                    key=lambda e: e.created_at or ""
                )
                idx = next((i for i, e in enumerate(thread_emails) if e.id == email.id), -1)
                already_replied = (idx >= 0 and idx < len(thread_emails) - 1
                                   and thread_emails[idx + 1].direction == "OUT")
                if already_replied:
                    print(f"\n⏭️  Déjà répondu à {email.from_email} (thread {email.thread_id})")
                    processed_ids.add(email.id)
                    continue

                result = process_email(email, config, pv, ppx, send=args.send)
                results.append(result)

                # Marquer comme traité
                processed_ids.add(email.id)
                processed_path.write_text(json.dumps(list(processed_ids), indent=2), encoding="utf-8")

            # Résumé
            print(f"\n{'='*60}")
            print(f"📊 RÉSUMÉ: {len(results)} email(s) traité(s)")
            for r in results:
                print(f"  - {r['from']}: {r['classification'].get('category', '?')} → {r['action']}")

            # Sauvegarder les résultats en JSON pour validation Gmail
            import pathlib
            tmp_dir = pathlib.Path(__file__).parent.parent / ".tmp"
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
