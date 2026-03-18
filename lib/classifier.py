"""Classification des réponses email via Claude Haiku."""

import os
import json
import time
import anthropic
from lib.config import get_prompt


def classify_reply(email_text: str) -> dict:
    """Classifie un email entrant.

    Returns: {"category": "INTERESTED|QUESTION|MEETING_CONFIRMED|OUT_OF_OFFICE|NOT_INTERESTED|UNSUBSCRIBE|HOSTILE",
              "reason": "explication courte"}
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    system_prompt = get_prompt("classify")

    # Retry avec backoff pour gérer les 529 (overloaded)
    for attempt in range(4):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": f"Voici l'email à classifier :\n\n{email_text}"}
                ],
            )
            break
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < 3:
                wait = 2 ** attempt
                print(f"   ⏳ API overloaded, retry dans {wait}s...")
                time.sleep(wait)
                continue
            raise
    else:
        return {"category": "INTERESTED", "reason": "API unavailable after retries, default to INTERESTED"}

    result_text = response.content[0].text.strip()
    # Parser le JSON de la réponse
    if "```" in result_text:
        result_text = result_text.split("```")[1]
        if result_text.startswith("json"):
            result_text = result_text[4:]
        result_text = result_text.strip()

    try:
        return json.loads(result_text)
    except json.JSONDecodeError:
        return {"category": "INTERESTED", "reason": f"Parsing failed, default to INTERESTED: {result_text[:100]}"}
