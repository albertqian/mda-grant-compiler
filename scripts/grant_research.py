"""
grant_research.py
-----------------
Searches for new grant opportunities relevant to Mujeres de Acción, filters out
grants already reported in previous runs, and emails a formatted summary.

State is persisted in data/seen_grants.json, which is committed back to the repo
by the GitHub Actions workflow after each run.
"""

import anthropic
import json
import os
import re
import smtplib
import hashlib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

SEEN_GRANTS_FILE = Path("data/seen_grants.json")

ORG_PROFILE = """
Organization: Mujeres de Acción
Location: San Luis Obispo County, California
Type: 501(c)(3) nonprofit
Mission: Empowering Latina women to take control of their health, become more
civically engaged, share their cultural pride, and lead their communities.

Key program areas:
- Health education and healthcare access for Latina women (including mental health)
- Civic engagement and voter participation
- Cultural empowerment and Hispanic heritage programming
- Leadership development for Latinas
- Support for immigrant and undocumented families (housing aid, navigation services)
- Community events: annual gala, Hispanic Heritage Festival, Children's Day

Past grants received:
- $1,250 (Legacy of Service Grant) for website development
- $12,400 (UndocuSupport Grant) for housing aid to immigrant families

Scale: Grassroots/emerging nonprofit; relevant to California and national funders.
"""

GRANT_SOURCES = (
    # SLO County & Central Coast
    "cfslo.org, French Hospital Medical Center community benefit grants, "
    "Arroyo Grande Regional Hospital community grants, Pacific Premier Bank community giving, "
    # California statewide
    "calwellness.org, blueshieldcafoundation.org, calendow.org, sierrahealth.org, "
    "calfund.org, weingartfnd.org, "
    # One national org — best fit for small grassroots Latina nonprofits
    "hiponline.org"
)

SEARCH_PROMPT = f"""
Find currently open grants for this small grassroots nonprofit:

Mujeres de Acción — San Luis Obispo County, CA 501(c)(3)
Mission: Empowering Latina women through health education, civic engagement, cultural pride, and community leadership.
Programs: Latina health/mental health, immigrant family support (housing, navigation), civic engagement, Hispanic Heritage Festival, women's leadership, Children's Day events.
Scale: Small/emerging nonprofit. Past grants: $1,250 (website), $12,400 (immigrant housing). Not a large or established NGO — prioritize funders that explicitly support grassroots or emerging organizations.

Search ONLY these sources in this order:
1. SLO County & Central Coast: cfslo.org, French Hospital Medical Center community grants, Arroyo Grande Regional Hospital grants, Pacific Premier Bank (Central Coast giving)
2. California statewide: calwellness.org, blueshieldcafoundation.org, calendow.org, sierrahealth.org, calfund.org, weingartfnd.org
3. National (grassroots Latina focus only): hiponline.org

Topic focus: Latina/Hispanic women's health, immigrant support, civic engagement, cultural programs, women's leadership, health equity.

Rules:
- Only grants currently open or opening within 60 days
- Only include grants realistic for a small emerging nonprofit — skip anything requiring multi-year budgets, audited financials, or large organizational infrastructure
- Do not fabricate amounts, deadlines, or URLs — use "Varies" or "Not listed" if unknown

Return ONLY a JSON array, no markdown, no preamble:
[{{"name":"...","funder":"...","amount":"...","deadline":"MM/DD/YYYY or Rolling or Not listed","location_scope":"SLO County|California|National","source":"website found on","url":"...","fit_reason":"2-3 sentences specific to MDA's programs, scale, and demographics"}}]

Return 5-8 grants sorted: SLO County first, then California, then National.
"""

# ── State management ───────────────────────────────────────────────────────────

def load_seen_grants() -> dict:
    if SEEN_GRANTS_FILE.exists():
        with open(SEEN_GRANTS_FILE) as f:
            return json.load(f)
    return {}


def save_seen_grants(seen: dict) -> None:
    SEEN_GRANTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_GRANTS_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def grant_id(grant: dict) -> str:
    """Stable ID derived from grant name + funder. Deadline intentionally excluded
    so that re-extended grants don't appear as duplicates."""
    key = f"{grant.get('name', '').strip().lower()}|{grant.get('funder', '').strip().lower()}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ── Grant search ───────────────────────────────────────────────────────────────

def search_grants() -> list[dict]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    print("Searching for grants via Anthropic API + web search...")

    # Retry up to 3 times with exponential backoff on rate limit errors
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
                messages=[{"role": "user", "content": SEARCH_PROMPT}],
            )
            break  # Success — exit retry loop
        except anthropic.RateLimitError as e:
            if attempt == max_attempts:
                raise
            wait = 60 * attempt  # 60s, then 120s
            print(f"Rate limit hit (attempt {attempt}/{max_attempts}). Waiting {wait}s...")
            time.sleep(wait)

    # Debug: log block types and stop reason to help diagnose future issues
    block_types = [getattr(b, "type", "unknown") for b in response.content]
    print(f"Stop reason: {response.stop_reason}")
    print(f"Response block types: {block_types}")

    # Collect all text blocks — web search results arrive as separate block types;
    # the final JSON answer will be in a "text" block at the end.
    raw_text = ""
    for block in response.content:
        if block.type == "text":
            raw_text += block.text

    if not raw_text.strip():
        print("ERROR: No text blocks found in response. Full content dump:")
        for i, block in enumerate(response.content):
            print(f"  Block {i}: type={getattr(block, 'type', '?')} "
                  f"text_preview={str(getattr(block, 'text', ''))[:120]}")
        raise ValueError(
            f"Anthropic API returned no text blocks. "
            f"stop_reason={response.stop_reason}, block_types={block_types}"
        )

    # Robustly extract the JSON array even if the model added preamble/postamble
    clean = raw_text.strip()

    # Strip markdown fences (```json ... ``` or ``` ... ```)
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", clean)
    if fence_match:
        clean = fence_match.group(1).strip()

    # Find the outermost [ ... ] in case there's any remaining prose
    start = clean.find("[")
    end = clean.rfind("]")
    if start != -1 and end != -1 and end > start:
        clean = clean[start : end + 1]

    if not clean:
        raise ValueError(f"Could not locate a JSON array in the response text:\n{raw_text[:500]}")

    grants = json.loads(clean)

    if not isinstance(grants, list):
        raise TypeError(f"Expected a JSON array; got {type(grants).__name__}")

    print(f"API returned {len(grants)} grant(s).")
    return grants


# ── Email ──────────────────────────────────────────────────────────────────────

BRAND_COLOR = "#8B1A4A"  # Deep magenta — close to MDA's palette

def build_email_html(new_grants: list[dict], today_str: str) -> str:
    if not new_grants:
        return f"""
        <div style="font-family:Georgia,serif;max-width:680px;margin:0 auto;padding:24px;">
          <h2 style="color:{BRAND_COLOR};border-bottom:2px solid {BRAND_COLOR};padding-bottom:8px;">
            Mujeres de Acción — Weekly Grant Report
          </h2>
          <p style="color:#555;">{today_str}</p>
          <p style="font-size:15px;color:#333;">
            No <em>new</em> grant opportunities were identified this week. All currently
            known open grants have already been reported in a prior email.
          </p>
          <p style="font-size:15px;color:#333;">Check back next Monday for updated results.</p>
          <hr style="border:none;border-top:1px solid #ddd;margin-top:32px;">
          <p style="font-size:11px;color:#aaa;">
            This report is generated automatically every Monday morning by the
            Mujeres de Acción grant research workflow. Always verify grant details
            directly with the funder before applying.
          </p>
        </div>
        """

    # Badge colors by location scope
    scope_styles = {
        "SLO County":   ("background:#1a6b3c;color:white;",  "📍 SLO County"),
        "California":   ("background:#1a4a8b;color:white;",  "🌉 California"),
        "National":     ("background:#555;color:white;",      "🇺🇸 National"),
        "International":("background:#7a4a00;color:white;",  "🌎 International"),
    }

    rows = ""
    for i, g in enumerate(new_grants):
        bg = "#fff" if i % 2 == 0 else "#fdf6fa"
        url = g.get("url", "").strip()
        link_cell = f'<a href="{url}" style="color:{BRAND_COLOR};">View ↗</a>' if url else "—"

        scope = g.get("location_scope", "National")
        scope_style, scope_label = scope_styles.get(scope, scope_styles["National"])
        badge = (
            f'<span style="display:inline-block;padding:2px 7px;border-radius:10px;'
            f'font-size:10px;font-weight:bold;{scope_style}">{scope_label}</span>'
        )

        source = g.get("source", "")
        source_note = f'<br><span style="font-size:10px;color:#aaa;">Found on: {source}</span>' if source else ""

        rows += f"""
        <tr style="background:{bg};vertical-align:top;">
          <td style="padding:14px 12px;border-bottom:1px solid #f0e6ec;">
            <strong style="font-size:14px;">{g['name']}</strong><br>
            <span style="font-size:12px;color:#777;">{g['funder']}</span><br>
            <span style="margin-top:4px;display:inline-block;">{badge}</span>
            {source_note}
          </td>
          <td style="padding:14px 12px;border-bottom:1px solid #f0e6ec;font-size:14px;white-space:nowrap;">
            {g['amount']}
          </td>
          <td style="padding:14px 12px;border-bottom:1px solid #f0e6ec;font-size:14px;white-space:nowrap;">
            {g['deadline']}
          </td>
          <td style="padding:14px 12px;border-bottom:1px solid #f0e6ec;font-size:13px;color:#444;line-height:1.5;">
            {g['fit_reason']}
          </td>
          <td style="padding:14px 12px;border-bottom:1px solid #f0e6ec;font-size:13px;">
            {link_cell}
          </td>
        </tr>"""

    # Build scope summary counts for the header
    scope_counts = {}
    for g in new_grants:
        s = g.get("location_scope", "National")
        scope_counts[s] = scope_counts.get(s, 0) + 1
    scope_summary = " &nbsp;·&nbsp; ".join(
        f"{v} {k}" for k, v in scope_counts.items()
    )

    return f"""
    <div style="font-family:Georgia,serif;max-width:820px;margin:0 auto;padding:24px;">
      <h2 style="color:{BRAND_COLOR};border-bottom:2px solid {BRAND_COLOR};padding-bottom:8px;margin-bottom:4px;">
        Mujeres de Acción — Weekly Grant Report
      </h2>
      <p style="color:#777;font-size:13px;margin-top:4px;">
        {today_str} &nbsp;·&nbsp; {len(new_grants)} new grant(s) &nbsp;·&nbsp; {scope_summary}
      </p>

      <table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;margin-top:16px;">
        <thead>
          <tr style="background:{BRAND_COLOR};color:white;">
            <th style="padding:12px;text-align:left;font-size:13px;min-width:180px;">Grant / Funder</th>
            <th style="padding:12px;text-align:left;font-size:13px;min-width:100px;">Amount</th>
            <th style="padding:12px;text-align:left;font-size:13px;min-width:90px;">Deadline</th>
            <th style="padding:12px;text-align:left;font-size:13px;">Why We're a Fit</th>
            <th style="padding:12px;text-align:left;font-size:13px;">Link</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>

      <hr style="border:none;border-top:1px solid #ddd;margin-top:32px;">
      <p style="font-size:11px;color:#aaa;">
        This report is generated automatically every Monday morning. Always verify
        grant details directly with the funder before applying. Grant amounts and
        deadlines may have changed since this report was generated.
      </p>
    </div>
    """


def send_email(new_grants: list[dict]) -> None:
    from_addr = os.environ["EMAIL_FROM"]
    to_addr = os.environ["EMAIL_TO"]
    password = os.environ["EMAIL_PASSWORD"]
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    today_str = datetime.now().strftime("%B %d, %Y")
    count_label = f"{len(new_grants)} new grant(s)" if new_grants else "No new grants"
    subject = f"Mujeres de Acción Grant Report — {count_label} ({today_str})"

    html_body = build_email_html(new_grants, today_str)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html"))

    print(f"Sending email to {to_addr} via {smtp_host}:{smtp_port}...")
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(from_addr, password)
        server.sendmail(from_addr, to_addr, msg.as_string())

    print("Email sent successfully.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    seen = load_seen_grants()
    print(f"Loaded {len(seen)} previously seen grant(s).")

    try:
        grants = search_grants()
    except Exception as exc:
        print(f"ERROR during grant search: {exc}", file=sys.stderr)
        sys.exit(1)

    new_grants = []
    for grant in grants:
        gid = grant_id(grant)
        if gid not in seen:
            new_grants.append(grant)
            seen[gid] = {
                "name": grant.get("name"),
                "funder": grant.get("funder"),
                "first_seen": datetime.utcnow().isoformat() + "Z",
            }
            print(f"  NEW: {grant.get('name')} — {grant.get('funder')}")
        else:
            print(f"  SKIP (seen): {grant.get('name')}")

    save_seen_grants(seen)
    print(f"Saved updated seen_grants.json ({len(seen)} total entries).")

    try:
        send_email(new_grants)
    except Exception as exc:
        print(f"ERROR sending email: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
