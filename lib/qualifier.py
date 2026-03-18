"""Qualification des leads via Claude Sonnet + enrichissement Perplexity."""

import os
import json
import anthropic
from lib.config import get_prompt


def qualify_lead(thread_text: str, lead_info: dict,
                 perplexity_data: dict, client_config: dict,
                 plusvibe_label: str = None) -> dict:
    """Qualifie un lead selon les critères du client.

    Args:
        thread_text: conversation complète formatée
        lead_info: infos de base sur le lead (email, nom, etc.)
        perplexity_data: résultat de la recherche Perplexity
        client_config: config du client avec les critères

    Returns: dict avec score, recommendation, suggested_questions, etc.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    system_prompt = get_prompt("qualification")

    # Construire le contexte
    criteria = client_config.get("qualification_criteria", {})
    sectors = criteria.get("sectors", [])
    general = criteria.get("general", "")

    criteria_text = "## Critères de qualification par secteur\n\n"
    for s in sectors:
        criteria_text += f"- **{s['name']}** : {s['criteria']}\n"
    criteria_text += f"\n## Critère général\n{general}\n"

    perplexity_text = "## Infos trouvées sur internet\n\n"
    if perplexity_data and not perplexity_data.get("error"):
        perplexity_text += json.dumps(perplexity_data, indent=2, ensure_ascii=False)
    else:
        perplexity_text += "Aucune information trouvée."

    label_context = ""
    if plusvibe_label:
        label_context = f"\n## Label PlusVibe (signal d'intérêt)\nLe système de prospection a classé ce lead comme : **{plusvibe_label}**\n"

    # Extraire les métriques trouvées par Perplexity
    findings = perplexity_data.get("qualification_findings", {}) if perplexity_data else {}
    findings_text = ""
    if findings:
        found = {k: v for k, v in findings.items() if v is not None and k != "sources"}
        unknown = [k for k, v in findings.items() if v is None and k != "sources"]
        if found:
            findings_text += "\n## Métriques trouvées par Perplexity (NE PAS citer dans l'email)\n"
            for k, v in found.items():
                findings_text += f"- {k}: {v}\n"
        if unknown:
            findings_text += f"\n## Métriques NON trouvées (à demander au prospect si nécessaire)\n"
            findings_text += ", ".join(unknown) + "\n"

    user_message = f"""{criteria_text}

{perplexity_text}
{findings_text}
{label_context}
## Informations sur le lead
- Email: {lead_info.get('email', 'inconnu')}
- Nom: {lead_info.get('name', 'inconnu')}
- Entreprise (depuis email): {lead_info.get('company', 'inconnue')}

## Conversation complète
{thread_text}

Évalue ce lead selon les critères ci-dessus.

RÈGLES pour les suggested_questions :
- Ne pose des questions QUE sur les métriques NON trouvées par Perplexity (listées ci-dessus).
- Si Perplexity a déjà trouvé une métrique (ex: nb collaborateurs = 8), ne la redemande PAS.
- Formule 1-2 questions max, ultra-ciblées sur les critères manquants (ex: encours en M€, nb lits, CA en M€).
- Si toutes les métriques nécessaires sont connues, ne propose aucune question et recommande directement BOOK_MEETING ou DECLINE_POLITELY."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_message}
        ],
    )

    result_text = response.content[0].text.strip()
    if "```" in result_text:
        result_text = result_text.split("```")[1]
        if result_text.startswith("json"):
            result_text = result_text[4:]
        result_text = result_text.strip()

    try:
        return json.loads(result_text)
    except json.JSONDecodeError:
        return {
            "score": 0.5,
            "recommendation": "ASK_QUESTIONS",
            "suggested_questions": ["Pourriez-vous me préciser votre activité ?"],
            "reasoning": f"Parsing failed: {result_text[:200]}",
        }
