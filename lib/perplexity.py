"""Client API Perplexity — enrichissement d'infos sur les leads via recherche web.

Recherche en 2 étapes :
  1. Sonar Pro — infos générales sur l'entreprise/personne
  2. Sonar Pro — recherche ciblée registres financiers (societe.com, pappers.fr, etc.)
"""

import os
import json
import re
import httpx
from typing import Optional


# Domaines email personnels (pas de domaine entreprise exploitable)
_PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.fr", "hotmail.com", "hotmail.fr",
    "outlook.com", "outlook.fr", "live.com", "live.fr", "orange.fr",
    "wanadoo.fr", "free.fr", "sfr.fr", "laposte.net", "icloud.com",
    "aol.com", "protonmail.com", "gmx.com", "gmx.fr",
}

# Registres financiers par pays
_REGISTRIES_FR = "societe.com, pappers.fr, verif.com, infogreffe.fr"
_REGISTRIES_CH = "moneyhouse.ch, zefix.ch"


def _extract_company_from_subject(subject: str) -> Optional[str]:
    """Extrait le nom d'entreprise depuis le sujet de l'email.

    Exemples : "Re: Capital 2AP" → "2AP", "Re: Capital Groupe BBM" → "Groupe BBM"
    """
    if not subject:
        return None
    # Retirer les préfixes Re:/Fwd:/TR:
    clean = re.sub(r"^(Re|Fwd|TR|Fw)\s*:\s*", "", subject, flags=re.IGNORECASE).strip()
    # Pattern "Capital XXX" (format RightLiens)
    m = re.match(r"Capital\s+(.+)", clean, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return clean


def _detect_country(email: str, company_name: str = "") -> str:
    """Détecte le pays probable du lead pour cibler les bons registres."""
    domain = ""
    if email and "@" in email:
        domain = email.split("@")[1].lower()
    combined = f"{domain} {company_name}".lower()
    if domain.endswith(".ch") or "suisse" in combined or "swiss" in combined:
        return "CH"
    return "FR"


def _parse_json_response(text: str) -> dict:
    """Parse une réponse JSON, même si elle contient du markdown."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    if text.startswith("json"):
        text = text[4:].strip()
    return json.loads(text)


class PerplexityClient:
    """Recherche d'informations sur un lead via l'API Perplexity (Sonar Pro)."""

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
        """Recherche Perplexity (Sonar Pro) et retourne la réponse textuelle."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": query})

        response = self.client.post(
            "/chat/completions",
            json={
                "model": "sonar-pro",
                "messages": messages,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def research_lead(self, lead_name: str, lead_email: str,
                      company_name: Optional[str] = None,
                      subject: Optional[str] = None,
                      qualification_criteria: Optional[list] = None) -> dict:
        """Recherche des infos business sur un lead en 2 étapes.

        Étape 1 : infos générales (nom, secteur, activité, localisation)
        Étape 2 : données financières via registres légaux (societe.com, pappers.fr, etc.)

        Args:
            lead_name: nom du contact
            lead_email: email du contact
            company_name: domaine email ou nom d'entreprise
            subject: sujet de l'email (pour extraire le nom d'entreprise si Gmail)
            qualification_criteria: liste de dicts {name, criteria} des secteurs cibles

        Returns: dict avec infos générales + qualification_findings (métriques trouvées)
        """
        # ── Construire la query de recherche ──────────────────────────
        parts = []
        domain = ""
        if lead_email and "@" in lead_email:
            domain = lead_email.split("@")[1].lower()

        # Utiliser le domaine s'il est professionnel
        if company_name and company_name.lower() not in _PERSONAL_DOMAINS:
            parts.append(company_name)
        elif domain and domain not in _PERSONAL_DOMAINS:
            parts.append(domain)

        # Pour les Gmail/adresses perso, extraire le nom depuis le sujet
        if not parts and subject:
            extracted = _extract_company_from_subject(subject)
            if extracted:
                parts.append(extracted)

        if lead_name:
            parts.append(lead_name)

        if not parts:
            return {"error": "Pas assez d'infos pour rechercher le lead"}

        search_query = " ".join(parts)
        country = _detect_country(lead_email, " ".join(parts))

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

        # ── Étape 1 : recherche générale ──────────────────────────────
        system_general = (
            "Tu es un analyste business spécialisé en M&A. Recherche des informations précises "
            "sur cette entreprise/personne pour évaluer si elle correspond à des critères d'acquisition.\n\n"
            "IMPORTANT : si le domaine email est .ch ou l'entreprise est en Suisse, cherche l'entreprise "
            "dans les registres suisses (zefix.ch, moneyhouse.ch), PAS une organisation internationale homonyme.\n\n"
            "Retourne un JSON avec :\n"
            "- company_name: nom exact de l'entreprise\n"
            "- company_description: activité en 1-2 phrases\n"
            "- sector: secteur d'activité précis\n"
            "- location: ville, département/canton, pays\n"
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
            print(f"   [Étape 1] Recherche générale : '{search_query}'")
            result_text = self.search(search_query, system_prompt=system_general)
            general_data = _parse_json_response(result_text)
        except (json.JSONDecodeError, Exception) as e:
            general_data = {"raw_response": result_text if 'result_text' in locals() else str(e)}

        # ── Étape 2 : recherche registres financiers ──────────────────
        # Construire le nom d'entreprise pour la recherche financière
        company_for_search = general_data.get("company_name") or " ".join(parts)

        if country == "CH":
            registries = _REGISTRIES_CH
            financial_query = f"{company_for_search} chiffre d'affaires effectif site:moneyhouse.ch OR site:zefix.ch"
        else:
            registries = _REGISTRIES_FR
            financial_query = (
                f"{company_for_search} chiffre d'affaires effectif EBITDA "
                f"site:societe.com OR site:pappers.fr OR site:verif.com OR site:infogreffe.fr"
            )

        system_financial = (
            f"Tu es un analyste financier. Recherche les données financières et légales de l'entreprise "
            f"'{company_for_search}' dans les registres publics ({registries}).\n\n"
            "Cherche spécifiquement :\n"
            "- Le chiffre d'affaires (CA) le plus récent disponible\n"
            "- Le résultat net, EBIT ou EBITDA si disponible\n"
            "- Le nombre de salariés / collaborateurs\n"
            "- Le numéro SIREN/SIRET ou IDE (Suisse)\n"
            "- La forme juridique (SARL, SAS, SA, etc.)\n"
            "- La date de création\n"
            "- Le capital social\n"
            "- Les encours sous gestion (si société de gestion / CGP)\n"
            "- Le nombre de lits (si EHPAD / résidence seniors)\n\n"
            "Retourne un JSON avec :\n"
            "- revenue: CA annuel (montant + année, null si inconnu)\n"
            "- net_income: résultat net (montant + année, null si inconnu)\n"
            "- ebitda: EBITDA (montant + année, null si inconnu)\n"
            "- employees: nombre de salariés (null si inconnu)\n"
            "- legal_form: forme juridique (null si inconnu)\n"
            "- registration_number: SIREN/SIRET ou IDE (null si inconnu)\n"
            "- creation_date: date de création (null si inconnu)\n"
            "- share_capital: capital social (null si inconnu)\n"
            "- aum: encours sous gestion (null si non applicable)\n"
            "- beds: nombre de lits (null si non applicable)\n"
            "- data_year: année des données financières\n"
            "- sources: liste des URLs sources\n\n"
            "Réponds UNIQUEMENT avec le JSON, sans markdown."
        )

        financial_data = {}
        try:
            print(f"   [Étape 2] Recherche registres ({registries}) : '{company_for_search}'")
            result_text = self.search(financial_query, system_prompt=system_financial)
            financial_data = _parse_json_response(result_text)
            print(f"   [Étape 2] CA: {financial_data.get('revenue', '?')} | "
                  f"Effectif: {financial_data.get('employees', '?')} | "
                  f"EBITDA: {financial_data.get('ebitda', '?')}")
        except (json.JSONDecodeError, Exception) as e:
            print(f"   [Étape 2] ⚠️ Erreur parsing registres: {e}")
            financial_data = {}

        # ── Fusionner les résultats ───────────────────────────────────
        return self._merge_results(general_data, financial_data)

    def _merge_results(self, general: dict, financial: dict) -> dict:
        """Fusionne les résultats des 2 étapes de recherche.

        Les données financières des registres (étape 2) ont priorité sur
        les données générales (étape 1) car elles sont plus fiables.
        """
        if not financial:
            return general

        # Mettre à jour qualification_findings avec les données des registres
        findings = general.get("qualification_findings", {})

        # Mapper les champs financiers → qualification_findings
        field_map = {
            "revenue": "revenue",
            "employees": "employees",
            "ebitda": "ebitda",
            "net_income": "ebit",  # approximation
            "aum": "aum",
            "beds": "beds",
        }

        for fin_key, find_key in field_map.items():
            fin_value = financial.get(fin_key)
            if fin_value is not None:
                findings[find_key] = fin_value

        # Ajouter les métadonnées légales
        for key in ("legal_form", "registration_number", "creation_date",
                     "share_capital", "data_year"):
            if financial.get(key) is not None:
                findings[key] = financial[key]

        # Sources : fusionner
        fin_sources = financial.get("sources", [])
        gen_sources = findings.get("sources", [])
        if isinstance(fin_sources, list) and isinstance(gen_sources, list):
            findings["sources"] = list(set(gen_sources + fin_sources))
        elif isinstance(fin_sources, list):
            findings["sources"] = fin_sources

        general["qualification_findings"] = findings
        return general

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
