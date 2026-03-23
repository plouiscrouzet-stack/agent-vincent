"""Qualification des leads via Claude Sonnet + enrichissement Perplexity."""

import os
import json
import time
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

    for attempt in range(4):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": user_message}
                ],
            )
            break
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < 3:
                wait = 2 ** attempt
                print(f"   ⏳ API overloaded (qualifier), retry dans {wait}s...")
                time.sleep(wait)
                continue
            raise
    else:
        return {
            "score": 0.5,
            "recommendation": "ASK_QUESTIONS",
            "suggested_questions": ["Pourriez-vous me préciser votre activité ?"],
            "reasoning": "API unavailable after retries",
        }

    result_text = response.content[0].text.strip()

    # Extraction JSON robuste — gère code blocks, texte avant/après, JSON tronqué
    parsed = _extract_json(result_text)
    if parsed:
        # S'assurer que les champs obligatoires existent
        parsed.setdefault("score", 0.5)
        parsed.setdefault("recommendation", "ASK_QUESTIONS")
        parsed.setdefault("suggested_questions", [])
        parsed.setdefault("reasoning", "")
        parsed.setdefault("should_respond", True)
        parsed.setdefault("criteria_evaluation", [])
        return parsed

    return {
        "score": 0.5,
        "recommendation": "ASK_QUESTIONS",
        "suggested_questions": ["Pourriez-vous me préciser votre activité ?"],
        "reasoning": f"Parsing failed: {result_text[:200]}",
        "should_respond": True,
        "criteria_evaluation": [],
        "_parsing_error": True,
    }


def _extract_json(text: str) -> dict | None:
    """Extraction JSON robuste depuis la réponse Claude.

    Gère : code blocks markdown, texte avant/après le JSON,
    JSON tronqué (accolades non fermées).
    """
    import re

    # 1. Essayer le texte brut d'abord
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Extraire depuis un code block markdown
    code_block = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if code_block:
        try:
            return json.loads(code_block.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. Trouver le premier { et le dernier } — extraire le JSON
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace:last_brace + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # 4. JSON tronqué — essayer de fermer les accolades/crochets manquants
    if first_brace != -1:
        candidate = text[first_brace:]
        # Compter les accolades/crochets ouverts
        opens = candidate.count("{") - candidate.count("}")
        open_brackets = candidate.count("[") - candidate.count("]")
        candidate += "]" * max(0, open_brackets)
        candidate += "}" * max(0, opens)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    return None
