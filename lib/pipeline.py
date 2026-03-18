"""Pipeline de traitement d'un email entrant.

classify → enrich (Perplexity) → qualify → generate reply → send validation email
"""

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib.plusvibe import Email, PlusVibeClient
    from lib.perplexity import PerplexityClient


def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in [
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&eacute;", "é"), ("&egrave;", "è"), ("&agrave;", "à"),
        ("&ecirc;", "ê"), ("&rsquo;", "'"), ("&Ecirc;", "Ê"), ("&Eacute;", "É"),
    ]:
        text = text.replace(entity, char)
    return text


def extract_quoted_email(body_text: str) -> str:
    quoted_lines = []
    in_quoted = False
    for line in body_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(">"):
            in_quoted = True
            quoted_lines.append(stripped.lstrip("> ").strip())
        elif in_quoted and not stripped:
            quoted_lines.append("")
    return "\n".join(quoted_lines).strip()


def format_thread(emails: list) -> str:
    from lib.plusvibe import Email as PVEmail

    reconstructed = []
    for e in emails:
        reconstructed.append(e)
        if e.direction == "IN":
            full_text = e.body_text or e.content_preview or ""
            quoted = extract_quoted_email(full_text)
            if quoted and len(quoted) > 20:
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

        text = e.body_text or e.content_preview or e.body_html or "(vide)"
        if "<" in text and ">" in text:
            text = clean_html(text)

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


def extract_lead_info(email) -> dict:
    lead_email = email.lead_email or email.from_email or ""
    domain = ""
    name = ""
    if "@" in lead_email:
        domain = lead_email.split("@")[1]
    from_raw = email.raw.get("from_address_json", []) if email.raw else []
    if from_raw and isinstance(from_raw, list) and from_raw[0].get("name"):
        name = from_raw[0]["name"]
    return {"email": lead_email, "name": name, "company": domain}


def process_email(email, client_config: dict, pv, ppx, send: bool = False) -> dict:
    """Traite un email entrant : classify → enrich → qualify → respond → validate.

    Returns: dict avec tous les résultats intermédiaires.
    """
    from lib.classifier import classify_reply
    from lib.qualifier import qualify_lead
    from lib.responder import generate_reply
    from lib.gmail_sender import send_validation_email
    from lib.kv_store import save_pending

    ws_id = client_config["workspace_id"]
    result = {
        "email_id": email.id,
        "from": email.from_email,
        "subject": email.subject,
        "lead_email": email.lead_email,
        "plusvibe_label": email.label,
        "campaign_id": email.campaign_id,
    }

    # 1. Texte du message — préférer snippet/preview, sinon nettoyer le HTML
    message_text = email.content_preview or email.body_text or ""
    if not message_text and email.body_html:
        message_text = clean_html(email.body_html)
    # Si le texte contient encore du HTML/CSS résiduel, le nettoyer
    if message_text and ("<" in message_text or "style=" in message_text.lower()):
        message_text = clean_html(message_text)
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

    skip_map = {
        "OUT_OF_OFFICE": ("⏭️  Out of office — aucune action", "SKIP_OOO"),
        "AUTO_REPLY": ("🤖 Réponse automatique — aucune action", "SKIP_AUTO_REPLY"),
        "NOT_INTERESTED": ("👋 Pas intéressé — aucune réponse", "SKIP_NOT_INTERESTED"),
        "UNSUBSCRIBE": ("🚫 Désinscription — aucune réponse", "SKIP_UNSUBSCRIBE"),
        "HOSTILE": ("⚠️  Message hostile — flag pour review humaine", "FLAG_HOSTILE"),
    }
    if category in skip_map:
        msg, action = skip_map[category]
        print(f"   {msg}")
        result["action"] = action
        return result

    # 3. Enrichissement Perplexity
    lead_info = extract_lead_info(email)
    print(f"\n🔍 Recherche Perplexity sur {lead_info['email']}...")
    perplexity_data = {"error": "not attempted"}
    sectors = client_config.get("qualification_criteria", {}).get("sectors", [])
    for attempt in range(3):
        try:
            perplexity_data = ppx.research_lead(
                lead_name=lead_info["name"],
                lead_email=lead_info["email"],
                company_name=lead_info["company"],
                qualification_criteria=sectors,
            )
            print(f"   → {perplexity_data.get('company_name', '?')} | {perplexity_data.get('sector', '?')}")
            break
        except Exception as e:
            if attempt < 2:
                import time
                wait = 2 ** attempt
                print(f"   ⚠️  Erreur Perplexity (tentative {attempt+1}/3), retry dans {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"   ⚠️  Erreur Perplexity après 3 tentatives: {e}")
                perplexity_data = {"error": str(e)}
    result["perplexity"] = perplexity_data

    # 4. Thread complet
    print(f"\n📋 Récupération du thread {email.thread_id}...")
    thread_emails = pv.get_thread(ws_id, email.thread_id)
    thread_text = format_thread(thread_emails)
    print(f"   → {len(thread_emails)} messages dans le thread")

    # 5. Qualification
    # Si MEETING_CONFIRMED, forcer BOOK_MEETING (pas de questions de qualif)
    if category == "MEETING_CONFIRMED":
        qualification = {
            "score": 0.85,
            "recommendation": "BOOK_MEETING",
            "reasoning": "Le prospect a confirmé ou demandé un meeting — répondre directement avec le lien de réservation.",
            "criteria_evaluation": [],
            "suggested_questions": [],
        }
        print(f"\n⚖️  Qualification: MEETING_CONFIRMED → BOOK_MEETING automatique (score 0.85)")
    else:
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
        qualification=qualification,
        perplexity_data=perplexity_data,
        client_config=client_config,
    )
    result["reply"] = reply_text
    print(f"\n📝 RÉPONSE GÉNÉRÉE:\n{'─'*40}\n{reply_text}\n{'─'*40}")

    # 7. Envoi direct (mode --send)
    if send:
        print(f"\n📤 Envoi de la réponse...")
        try:
            subject = email.subject or ""
            send_result = pv.reply_to_email(
                workspace_id=ws_id,
                reply_to_id=email.id,
                subject=f"Re: {subject}" if not subject.startswith("Re:") else subject,
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

    # 8. Mode validation : sauvegarde KV + email HTML
    pending_data = {
        "email_id": email.id,
        "workspace_id": ws_id,
        "from_email": email.from_email,
        "lead_email": email.lead_email,
        "eaccount": email.eaccount,
        "subject": email.subject,
        "reply_text": reply_text,
        "classification": classification,
        "qualification": qualification,
        "perplexity": perplexity_data,
        "plusvibe_label": email.label,
    }
    save_pending(email.id, pending_data)

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
            qualification=qualification,
            perplexity_data=perplexity_data,
            label=email.label,
        )
        result["action"] = "VALIDATION_SENT"
    except Exception as e:
        print(f"   ⚠️  Erreur envoi validation: {e}")
        result["action"] = "VALIDATION_FAILED"
        result["validation_error"] = str(e)

    return result
