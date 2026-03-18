"""Client API Perplexity — enrichissement d'infos sur les leads via recherche web."""

import os
import json
import httpx
from typing import Optional


class PerplexityClient:
    """Recherche d'informations sur un lead via l'API Perplexity."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("PERPLEXITY_API_KEY")
        if not self.api_key:
            raise ValueError("PERPLEXITY_API_KEY manquant")
        self.client = httpx.Client(
            base_url="https://api.perplexity.ai",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def search(self, query: str, system_prompt: Optional[str] = None) -> str:
        """Recherche Perplexity et retourne la réponse textuelle."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": query})

        response = self.client.post(
            "/chat/completions",
            json={
                "model": "sonar",
                "messages": messages,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def research_lead(self, lead_name: str, lead_email: str,
                      company_name: Optional[str] = None,
                      qualification_criteria: Optional[list] = None) -> dict:
        """Recherche des infos business sur un lead, ciblée sur les critères de qualification.

        Args:
            qualification_criteria: liste de dicts {name, criteria} des secteurs cibles
                                    → guide Perplexity sur les métriques à chercher

        Returns: dict avec infos générales + qualification_findings (métriques trouvées)
        """
        parts = []
        if company_name:
            parts.append(company_name)
        elif lead_email and "@" in lead_email:
            domain = lead_email.split("@")[1]
            if domain not in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com"):
                parts.append(domain)
        if lead_name:
            parts.append(lead_name)

        if not parts:
            return {"error": "Pas assez d'infos pour rechercher le lead"}

        search_query = " ".join(parts)

        # Construire la liste des métriques à chercher selon les critères
        metrics_instructions = ""
        if qualification_criteria:
            metrics_lines = []
            for s in qualification_criteria:
                metrics_lines.append(f"  - {s['name']} : {s['criteria']}")
            metrics_instructions = (
                "\n\nCritères de qualification à vérifier (cherche ces métriques en priorité) :\n"
                + "\n".join(metrics_lines)
                + "\n\nPour chaque métrique trouvée, indique la valeur exacte et la source."
            )

        system = (
            "Tu es un analyste business spécialisé en M&A. Recherche des informations précises "
            "sur cette entreprise/personne pour évaluer si elle correspond à des critères d'acquisition.\n\n"
            "Retourne un JSON avec :\n"
            "- company_name: nom exact de l'entreprise\n"
            "- company_description: activité en 1-2 phrases\n"
            "- sector: secteur d'activité précis\n"
            "- location: ville, département, pays\n"
            "- lead_role: poste/rôle de la personne contactée\n"
            "- website: site web\n"
            "- recent_news: actualités M&A/cessions/acquisitions récentes (null si rien)\n"
            "- qualification_findings: objet avec les métriques de qualification trouvées :\n"
            "    - employees: nombre d'employés/collaborateurs (null si inconnu)\n"
            "    - revenue: CA annuel en euros (null si inconnu)\n"
            "    - ebit: EBIT annuel en euros (null si inconnu)\n"
            "    - ebitda: EBITDA annuel en euros (null si inconnu)\n"
            "    - aum: encours sous gestion en euros pour CGP (null si inconnu)\n"
            "    - beds: nombre de lits pour EHPAD (null si inconnu)\n"
            "    - departments: départements/zones géographiques d'activité (null si inconnu)\n"
            "    - sources: liste des sources pour les chiffres trouvés"
            + metrics_instructions
            + "\n\nRéponds UNIQUEMENT avec le JSON, sans markdown."
        )

        try:
            result = self.search(search_query, system_prompt=system)
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(result)
        except (json.JSONDecodeError, Exception) as e:
            return {"raw_response": result if 'result' in locals() else str(e)}

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
