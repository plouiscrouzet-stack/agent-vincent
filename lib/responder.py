"""Génération de réponses email contextuelles via Claude Sonnet."""

import os
import json
import anthropic
from lib.config import get_prompt


def generate_reply(thread_text: str, latest_message: str,
                   classification: dict, qualification: dict,
                   perplexity_data: dict, client_config: dict) -> str:
    """Génère une réponse email contextuelle.

    Args:
        thread_text: conversation complète formatée
        latest_message: dernier message du prospect
        classification: résultat du classifier
        qualification: résultat du qualifier
        perplexity_data: infos Perplexity sur le lead
        client_config: config du client

    Returns: texte de l'email de réponse (prêt à envoyer)
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    system_template = get_prompt("system")

    # Construire le prompt avec les variables du client
    tone = client_config.get("tone", {})
    faq = client_config.get("faq", {})
    faq_text = "\n".join(f"- **{k}** : {v}" for k, v in faq.items())

    system_prompt = system_template.format(
        client_name=client_config.get("client_name", ""),
        offer_description=client_config.get("offer_description", ""),
        booking_link=client_config.get("booking_link", ""),
        tone_style=tone.get("style", "professionnel"),
        tu_vous=tone.get("tu_vous", "vouvoyer"),
        faq=faq_text,
        custom_instructions=tone.get("custom_instructions", ""),
    )

    # Construire le message utilisateur
    qualification_context = ""
    recommendation = qualification.get("recommendation", "ASK_QUESTIONS")
    if recommendation == "BOOK_MEETING":
        qualification_context = (
            f"Le prospect semble QUALIFIÉ (score: {qualification.get('score', '?')}).\n"
            f"→ Propose naturellement un call via le lien de réservation.\n"
            f"Secteur identifié: {qualification.get('sector_match', 'inconnu')}"
        )
    elif recommendation == "ASK_QUESTIONS":
        questions = qualification.get("suggested_questions", [])
        qualification_context = (
            f"Pas assez d'infos pour qualifier (score: {qualification.get('score', '?')}).\n"
            f"→ Glisse ces questions naturellement dans ta réponse :\n"
            + "\n".join(f"  - {q}" for q in questions[:2])
        )
    elif recommendation == "DECLINE_POLITELY":
        qualification_context = (
            f"Le prospect ne correspond PAS aux critères (score: {qualification.get('score', '?')}).\n"
            f"→ Clôture poliment la conversation.\n"
            f"Raison: {qualification.get('reasoning', '')}"
        )

    perplexity_context = ""
    if perplexity_data and not perplexity_data.get("error"):
        perplexity_context = (
            "## Infos trouvées sur le prospect\n"
            + json.dumps(perplexity_data, indent=2, ensure_ascii=False)
        )

    user_message = f"""## Qualification
{qualification_context}

{perplexity_context}

## Historique de conversation
{thread_text}

## Dernier message du prospect (à répondre)
{latest_message}

## Signature à utiliser
{client_config.get('signature', '')}

Rédige la réponse email."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_message}
        ],
    )

    return response.content[0].text.strip()
