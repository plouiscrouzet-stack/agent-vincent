"""Envoi d'emails HTML de validation via Gmail SMTP."""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _build_html(
    email_id: str,
    lead_email: str,
    subject: str,
    reply_text: str,
    classification: dict,
    qualification: dict,
    perplexity_data: dict,
    label: str,
    approve_url: str,
    reject_url: str,
    warnings: list = None,
) -> str:
    category = classification.get("category", "?")
    reason = classification.get("reason", "")
    score = qualification.get("score", "?")
    recommendation = qualification.get("recommendation", "?")
    reasoning = qualification.get("reasoning", "")

    company = perplexity_data.get("company_name", "?")
    sector = perplexity_data.get("sector", "?")
    location = perplexity_data.get("location", "?")

    findings = perplexity_data.get("qualification_findings", {})
    found_items = {k: v for k, v in findings.items() if v is not None and k != "sources"}
    unknown_items = [k for k, v in findings.items() if v is None and k != "sources"]

    findings_html = ""
    if found_items:
        rows = "".join(f"<tr><td style='padding:4px 8px;color:#6b7280'>{k}</td><td style='padding:4px 8px;font-weight:600'>{v}</td></tr>" for k, v in found_items.items())
        findings_html += f"<p style='margin:8px 0 4px;font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px'>Trouvé par Perplexity</p><table style='width:100%;border-collapse:collapse'>{rows}</table>"
    if unknown_items:
        findings_html += f"<p style='margin:8px 0 4px;font-size:12px;color:#f59e0b;text-transform:uppercase;letter-spacing:.5px'>Non trouvé (métriques manquantes)</p><p style='color:#92400e;font-size:13px'>{', '.join(unknown_items)}</p>"

    label_badge = ""
    if label:
        label_badge = f"<span style='background:#dbeafe;color:#1d4ed8;padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:600;margin-left:8px'>{label}</span>"

    # Bannière d'alertes
    warnings_html = ""
    if warnings:
        warning_items = "".join(
            f"<li style='margin:4px 0;font-size:13px'>{w}</li>" for w in warnings
        )
        warnings_html = f"""
  <div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:12px 16px;margin:12px 24px 0">
    <p style="margin:0 0 6px;font-weight:700;color:#92400e;font-size:13px">⚠️ ALERTES</p>
    <ul style="margin:0;padding-left:20px;color:#92400e">{warning_items}</ul>
  </div>"""

    # Bannière "pas de réponse" si should_respond = false
    no_response = not qualification.get("should_respond", True)
    no_response_html = ""
    if no_response:
        no_response_html = f"""
  <div style="background:#fee2e2;border:2px solid #dc2626;border-radius:8px;padding:16px 20px;margin:12px 24px 0">
    <p style="margin:0;font-weight:700;color:#dc2626;font-size:15px">🚫 RECOMMANDATION : NE PAS RÉPONDRE</p>
    <p style="margin:6px 0 0;color:#991b1b;font-size:13px">{qualification.get('reasoning', 'Prospect hors cible')}</p>
  </div>"""

    reply_html = reply_text.replace("\n", "<br>")

    criteria_rows = ""
    for c in qualification.get("criteria_evaluation", []):
        status = c.get("status", "?")
        color = {"REMPLI": "#16a34a", "INCONNU": "#d97706", "NON_REMPLI": "#dc2626"}.get(status, "#6b7280")
        badge = f"<span style='color:{color};font-weight:700'>{status}</span>"
        criteria_rows += f"""
        <tr style='border-bottom:1px solid #f3f4f6'>
          <td style='padding:6px 8px;font-size:13px'>{c.get('criteria','')}</td>
          <td style='padding:6px 8px;text-align:center'>{badge}</td>
          <td style='padding:6px 8px;font-size:12px;color:#6b7280'>{c.get('evidence','')}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9fafb;margin:0;padding:20px">
<div style="max-width:680px;margin:0 auto">

  <!-- Header -->
  <div style="background:#1e293b;border-radius:12px 12px 0 0;padding:20px 24px">
    <p style="color:#94a3b8;margin:0;font-size:12px;text-transform:uppercase;letter-spacing:1px">Agent Vincent — Validation requise</p>
    <h2 style="color:#f1f5f9;margin:4px 0 0;font-size:18px">{lead_email} {label_badge}</h2>
    <p style="color:#64748b;margin:4px 0 0;font-size:13px">{subject}</p>
  </div>

  {no_response_html}
  {warnings_html}

  <!-- Classification + Qualification -->
  <div style="background:#fff;padding:20px 24px;border-left:4px solid #6366f1">
    <div style="display:flex;gap:16px;flex-wrap:wrap">
      <div>
        <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">Classification</p>
        <p style="margin:4px 0 0;font-weight:700;color:#1e293b">{category}</p>
        <p style="margin:2px 0 0;font-size:12px;color:#6b7280">{reason}</p>
      </div>
      <div style="border-left:1px solid #e5e7eb;padding-left:16px">
        <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">Qualification</p>
        <p style="margin:4px 0 0;font-weight:700;color:#1e293b">Score {score} → {recommendation}</p>
        <p style="margin:2px 0 0;font-size:12px;color:#6b7280">{reasoning[:120]}{"..." if len(reasoning) > 120 else ""}</p>
      </div>
    </div>
  </div>

  <!-- Perplexity -->
  <div style="background:#f8fafc;padding:16px 24px;border-top:1px solid #e2e8f0">
    <p style="margin:0 0 8px;font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">Contexte Perplexity</p>
    <p style="margin:0;font-size:14px;font-weight:600">{company} <span style="font-weight:400;color:#6b7280">— {sector}</span></p>
    <p style="margin:2px 0 8px;font-size:13px;color:#6b7280">{location}</p>
    {findings_html}
  </div>

  <!-- Criteria evaluation -->
  {"<div style='background:#fff;padding:16px 24px;border-top:1px solid #e2e8f0'><p style='margin:0 0 8px;font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px'>Évaluation critères</p><table style='width:100%;border-collapse:collapse'><thead><tr style='background:#f3f4f6'><th style='padding:6px 8px;text-align:left;font-size:12px;color:#6b7280'>Critère</th><th style='padding:6px 8px;font-size:12px;color:#6b7280'>Statut</th><th style='padding:6px 8px;text-align:left;font-size:12px;color:#6b7280'>Preuve</th></tr></thead><tbody>" + criteria_rows + "</tbody></table></div>" if criteria_rows else ""}

  <!-- Reply -->
  <div style="background:#fff;padding:20px 24px;border-top:1px solid #e2e8f0">
    <p style="margin:0 0 12px;font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">Réponse proposée</p>
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px;font-size:14px;line-height:1.6;color:#1e293b">
      {reply_html}
    </div>
  </div>

  <!-- Buttons -->
  <div style="background:#fff;padding:28px 24px 24px;border-top:1px solid #e2e8f0;border-radius:0 0 12px 12px;text-align:center">
    <a href="{approve_url}" style="display:block;background:#16a34a;color:#fff;text-decoration:none;padding:16px 32px;border-radius:8px;font-weight:700;font-size:17px;margin-bottom:16px">
      ✅ Valider &amp; Envoyer
    </a>
    <a href="{reject_url}" style="display:block;background:#fff;color:#dc2626;text-decoration:none;padding:16px 32px;border-radius:8px;font-weight:700;font-size:17px;border:2px solid #dc2626">
      ❌ Refuser
    </a>
    <p style="margin:16px 0 0;font-size:11px;color:#9ca3af">Email ID: {email_id}</p>
  </div>

</div>
</body>
</html>"""


def send_validation_email(
    to_email: str,
    email_id: str,
    lead_email: str,
    subject: str,
    reply_text: str,
    classification: dict,
    qualification: dict,
    perplexity_data: dict,
    label: str = None,
    warnings: list = None,
) -> bool:
    """Envoie un email HTML de validation avec boutons Valider/Refuser.

    Requiert dans .env :
        GMAIL_SENDER_EMAIL  — adresse Gmail qui envoie
        GMAIL_APP_PASSWORD  — mot de passe d'application Gmail (16 caractères)
        VALIDATION_BASE_URL — ex: https://xxxx.ngrok-free.app ou http://localhost:5001
    """
    gmail_email = os.getenv("GMAIL_SENDER_EMAIL")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    # VALIDATION_BASE_URL doit pointer vers l'URL de production stable
    # (ex: https://agent-vincent.vercel.app), PAS vers VERCEL_URL qui change à chaque déploiement
    base_url = os.getenv("VALIDATION_BASE_URL", "http://localhost:5001")
    base_url = base_url.rstrip("/")

    if not gmail_email or not gmail_password:
        raise ValueError(
            "GMAIL_SENDER_EMAIL et GMAIL_APP_PASSWORD manquants dans .env\n"
            "→ Créer un mot de passe d'app : myaccount.google.com/apppasswords"
        )

    approve_url = f"{base_url}/validate/{email_id}/approve"
    reject_url = f"{base_url}/validate/{email_id}/reject"

    html = _build_html(
        email_id=email_id,
        lead_email=lead_email,
        subject=subject,
        reply_text=reply_text,
        classification=classification,
        qualification=qualification,
        perplexity_data=perplexity_data,
        label=label,
        approve_url=approve_url,
        reject_url=reject_url,
        warnings=warnings,
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[AGENT] {lead_email} — {classification.get('category', '?')}"
    msg["From"] = gmail_email
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_email, gmail_password)
        server.sendmail(gmail_email, to_email, msg.as_string())

    print(f"   ✅ Email de validation envoyé à {to_email}")
    return True
