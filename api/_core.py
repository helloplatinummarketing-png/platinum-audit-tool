"""Web-safe adapter around the Platinum audit modules.

The CLI modules in _lib/ prompt with input() and end on sys.exit(). Nothing
here calls those paths: profiles are built straight from the request JSON, and
the one place the CLI still exits (a failed Gemini query) is caught and turned
into an AuditError so the API can answer with JSON instead of a stack trace.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import fields as dataclass_fields
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE / "_lib"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import automation_gap_audit as automation  # noqa: E402
import geo_audit as geo  # noqa: E402
import platinum_audit_pack as pack  # noqa: E402
import website_audit as website  # noqa: E402

AUDIT_TYPES = ("geo", "website", "automation", "full")

# The dropdown sends a sector label; the AI query reads better with the noun a
# customer would actually search for.
TRADE_NOUNS = {
    "roofing": "roofer",
    "plumbing": "plumber",
    "electrical": "electrician",
    "landscaping": "landscaper",
    "cleaning": "cleaning company",
    "real estate": "estate agent",
}


class AuditError(Exception):
    """A problem the caller can fix, reported as a 400 with a plain message."""


# ---------------------------------------------------------------------------
# Input coercion
# ---------------------------------------------------------------------------


def as_bool(value, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"y", "yes", "true", "1"}:
            return True
        if lowered in {"n", "no", "false", "0"}:
            return False
    raise AuditError(f"'{field}' must be yes or no.")


def as_number(value, field: str, minimum: float = 0.0) -> float:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise AuditError(f"'{field}' must be a number.") from None
    if number < minimum:
        raise AuditError(f"'{field}' must be {minimum:g} or more.")
    return number


def as_text(value, field: str, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise AuditError(f"'{field}' is required.")
    return text


def block(payload: dict, name: str) -> dict:
    section = payload.get(name)
    if not isinstance(section, dict):
        raise AuditError(f"Missing the '{name}' answers for this audit type.")
    return section


def trade_noun(industry: str, country: str) -> str:
    noun = TRADE_NOUNS.get(industry.strip().lower(), industry.strip().lower())
    if noun == "estate agent" and country == "Kenya":
        return "real estate agent"
    return noun


def require_api_key() -> None:
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key or key.lower().startswith("your"):
        raise AuditError(
            "GEMINI_API_KEY is not set. Add it under Settings > Environment "
            "Variables in the Vercel project, then redeploy."
        )


def model_name() -> str:
    return os.getenv("GEMINI_MODEL", geo.DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def build_geo(payload: dict, business: str, trade: str, area: str, ai_data=None):
    answers = block(payload, "geo")
    if not area:
        raise AuditError("'area' is required for the GEO audit.")

    try:
        last_review = geo.parse_date_value(str(answers.get("last_review", "")))
    except ValueError as exc:
        raise AuditError(f"Last review date: {exc}") from None

    profile = geo.BusinessProfile(
        business=business,
        trade=trade,
        area=area,
        review_count=int(as_number(answers.get("reviews", 0), "Google review count")),
        last_review=last_review,
        has_website=as_bool(answers.get("has_website"), "Has website"),
        has_faq=as_bool(answers.get("has_faq"), "Has FAQ page"),
        directory_count=int(
            as_number(answers.get("directories", 0), "Directory listings")
        ),
        gbp_complete=as_bool(answers.get("gbp_complete"), "GBP complete"),
        has_schema=as_bool(answers.get("has_schema"), "Schema markup"),
    )

    if ai_data:
        ai = geo.AIResult(
            question=str(ai_data.get("question", "")),
            answer=str(ai_data.get("answer", "")),
            mentioned=bool(ai_data.get("mentioned", False)),
            position=ai_data.get("position") or None,
            competitors=[str(name) for name in ai_data.get("competitors", [])],
        )
    else:
        require_api_key()
        try:
            ai = geo.query_ai(trade, area, model_name(), business)
        except SystemExit as exc:
            raise AuditError(str(exc) or "The AI search query failed.") from None

    lines = geo.score_audit(profile, ai)
    return profile, lines, geo.total_score(lines), ai


def build_website(payload: dict, business: str, url: str, trade: str, country: str):
    answers = block(payload, "website")
    if not url:
        raise AuditError("'url' is required for the website audit.")

    profile = website.WebsiteProfile(
        business=business,
        url=website.normalize_url(url),
        industry=trade,
        country=country,
        mobile_ok=as_bool(answers.get("mobile"), "Mobile responsive"),
        load_seconds=as_number(answers.get("load_time", 0), "Load time"),
        has_ssl=as_bool(answers.get("ssl"), "HTTPS"),
        has_title=as_bool(answers.get("title"), "Title tag"),
        has_meta=as_bool(answers.get("meta"), "Meta description"),
        has_h1=as_bool(answers.get("h1"), "H1 heading"),
        contact_visible=as_bool(answers.get("contact"), "Contact info"),
        has_cta=as_bool(answers.get("cta"), "Call to action"),
        gbp_linked=as_bool(answers.get("gbp_linked"), "GBP linked"),
        has_schema=as_bool(answers.get("schema"), "Schema markup"),
        has_testimonials=as_bool(answers.get("testimonials"), "Testimonials"),
        has_chat_payment=as_bool(answers.get("chat_payment"), "Chat / payment"),
    )
    lines = website.score_website(profile)
    return profile, lines, website.total_score(lines)


def website_note(profile, lines, supplied=None) -> str:
    """The Gemini one-liner. Optional: the report is complete without it."""
    if supplied is not None:
        return str(supplied or "")
    if not (os.getenv("GEMINI_API_KEY") or "").strip():
        return ""
    try:
        return website.query_industry_note(profile, lines, model_name()) or ""
    except SystemExit:
        return ""
    except Exception:
        return ""


def build_automation(payload: dict, business: str, trade: str, country: str):
    answers = block(payload, "automation")
    flags = {
        dest: as_bool(answers.get(dest), question)
        for dest, question, _points, _rate in automation.QUESTIONS
    }
    profile = automation.AutomationProfile(
        business=business,
        industry=trade,
        country=country,
        avg_job_value=as_number(answers.get("avg_job_value", 0), "Average job value"),
        monthly_leads=int(as_number(answers.get("monthly_leads", 0), "Monthly leads")),
        **flags,
    )
    lines = automation.score_automation(profile)
    return profile, lines, automation.total_score(lines)


def revenue_summary(profile, lines) -> dict:
    leaks = automation.revenue_leaks(profile, lines)
    total = sum(amount for _, _, amount in leaks)
    return {
        "total": round(total, 2),
        "formatted": automation.format_money(total, profile.country),
        "basis": automation.leak_basis(profile),
        "leaks": [
            {
                "label": label,
                "rate": rate,
                "rate_label": f"{rate:.0%} of enquiries",
                "amount": round(amount, 2),
                "formatted": automation.format_money(amount, profile.country),
            }
            for label, rate, amount in leaks
        ],
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def compute(payload: dict) -> dict:
    """Validate the request and score every audit it asks for."""
    audit_type = str(payload.get("audit_type", "")).strip().lower()
    if audit_type not in AUDIT_TYPES:
        raise AuditError(f"'audit_type' must be one of: {', '.join(AUDIT_TYPES)}.")

    business = as_text(payload.get("business"), "Business name")
    raw_industry = as_text(payload.get("industry"), "Industry")
    area = as_text(payload.get("area"), "City / area", required=False)
    url = as_text(payload.get("url"), "Website URL", required=False)

    try:
        country = website.parse_country(str(payload.get("country", "")))
    except Exception:
        raise AuditError("'country' must be UK or Kenya.") from None

    trade = trade_noun(raw_industry, country)
    result = {
        "audit_type": audit_type,
        "business": business,
        "industry": raw_industry,
        "trade": trade,
        "area": area,
        "country": country,
        "url": url,
        "generated": date.today(),
        "geo": None,
        "website": None,
        "automation": None,
    }

    if audit_type in ("geo", "full"):
        profile, lines, total, ai = build_geo(
            payload, business, trade, area, payload.get("ai")
        )
        result["geo"] = {"profile": profile, "lines": lines, "total": total, "ai": ai}

    if audit_type in ("website", "full"):
        profile, lines, total = build_website(payload, business, url, trade, country)
        note = website_note(profile, lines, payload.get("website_note"))
        result["website"] = {
            "profile": profile,
            "lines": lines,
            "total": total,
            "note": note,
        }

    if audit_type in ("automation", "full"):
        profile, lines, total = build_automation(payload, business, trade, country)
        result["automation"] = {"profile": profile, "lines": lines, "total": total}

    return result


def line_to_dict(line) -> dict:
    data = {field.name: getattr(line, field.name) for field in dataclass_fields(line)}
    data["percent"] = round(100 * line.score / line.maximum) if line.maximum else 0
    return data


def sections_for(computed: dict) -> list[dict]:
    """Normalise each scored audit into the shape the frontend renders."""
    trade, area, country = computed["trade"], computed["area"], computed["country"]
    sections: list[dict] = []

    if computed["geo"]:
        part = computed["geo"]
        sections.append(
            {
                "key": "geo",
                "title": "GEO / AI Visibility",
                "score": part["total"],
                "grade": geo.grade(part["total"]),
                "weight": pack.GEO_WEIGHT,
                "verdict": geo.verdict(part["total"], trade, area),
                "lines": [line_to_dict(line) for line in part["lines"]],
                "missing": geo.missing_items(part["lines"]),
                "fixes": [
                    {"title": title, "detail": detail}
                    for title, detail in geo.top_fixes(part["lines"])
                ],
                "ai": {
                    "question": part["ai"].question,
                    "answer": part["ai"].answer,
                    "mentioned": part["ai"].mentioned,
                    "position": part["ai"].position,
                    "competitors": part["ai"].competitors,
                    "summary": geo.ai_mention_line(part["ai"]).replace("**", ""),
                },
            }
        )

    if computed["website"]:
        part = computed["website"]
        sections.append(
            {
                "key": "website",
                "title": "Website Health",
                "score": part["total"],
                "grade": geo.grade(part["total"]),
                "weight": pack.WEBSITE_WEIGHT,
                "verdict": website.verdict(part["total"], trade, country),
                "lines": [line_to_dict(line) for line in part["lines"]],
                "missing": website.missing_items(part["lines"]),
                "fixes": [
                    {"title": title, "detail": detail}
                    for title, detail in website.top_fixes(part["lines"])
                ],
                "note": part["note"],
            }
        )

    if computed["automation"]:
        part = computed["automation"]
        sections.append(
            {
                "key": "automation",
                "title": "Automation Gaps",
                "score": part["total"],
                "grade": geo.grade(part["total"]),
                "weight": pack.AUTOMATION_WEIGHT,
                "verdict": automation.verdict(part["total"], trade),
                "lines": [line_to_dict(line) for line in part["lines"]],
                "missing": automation.missing_items(part["lines"]),
                "fixes": [
                    {"title": line.label, "detail": line.fix, "n8n": line.n8n}
                    for line in automation.top_automations(part["lines"])
                ],
                "revenue": revenue_summary(part["profile"], part["lines"]),
            }
        )

    return sections


def pack_sections(computed: dict, sections: list[dict]) -> list:
    """Rebuild the combined-report sections the pack's PDF builder expects."""
    by_key = {part["key"]: part for part in sections}
    built = []
    for number, key in enumerate(("geo", "website", "automation"), start=1):
        part, raw = by_key[key], computed[key]
        note = ""
        if key == "automation" and part["revenue"]["total"]:
            note = f"Estimated monthly revenue at risk: {part['revenue']['formatted']}"
        built.append(
            pack.AuditSection(
                number=number,
                title=part["title"],
                score=part["score"],
                weight=part["weight"],
                verdict=part["verdict"],
                lines=raw["lines"],
                missing=part["missing"],
                fixes=[(fix["title"], fix["detail"]) for fix in part["fixes"]],
                note=note,
            )
        )
    return built


def pdf_name(computed: dict) -> str:
    slug = geo.slugify(computed["business"])
    stamp = computed["generated"].strftime("%Y%m%d")
    suffix = {
        "geo": "ai-visibility-audit",
        "website": "website-health-audit",
        "automation": "automation-gap-audit",
        "full": "platinum-business-audit",
    }[computed["audit_type"]]
    return f"{slug}-{suffix}-{stamp}.pdf"


def to_json(computed: dict) -> dict:
    sections = sections_for(computed)
    generated = computed["generated"]

    if computed["audit_type"] == "full":
        built = pack_sections(computed, sections)
        score = pack.combined_score(built)
        headline = pack.combined_verdict(score)
        plan = [
            {"area": area, "title": label, "detail": fix}
            for area, label, fix in pack.priority_plan(built)
        ]
    else:
        score = sections[0]["score"]
        headline = sections[0]["verdict"]
        plan = [dict(fix, area=sections[0]["title"]) for fix in sections[0]["fixes"]]

    return {
        "ok": True,
        "audit_type": computed["audit_type"],
        "business": computed["business"],
        "industry": computed["industry"],
        "trade": computed["trade"],
        "area": computed["area"],
        "country": computed["country"],
        "url": computed["url"],
        "generated": generated.isoformat(),
        "generated_label": generated.strftime("%d %b %Y"),
        "score": score,
        "grade": geo.grade(score),
        "verdict": headline,
        "sections": sections,
        "plan": plan,
        "pdf_name": pdf_name(computed),
        "ai": sections[0]["ai"] if computed["geo"] else None,
        "website_note": computed["website"]["note"] if computed["website"] else None,
    }


def to_pdf(computed: dict) -> tuple[str, bytes]:
    """Render the report to PDF bytes via a temp file.

    Every build_pdf in _lib hands SimpleDocTemplate a path, so the report is
    written to the function's writable /tmp and read back.
    """
    name = pdf_name(computed)
    out = Path(tempfile.gettempdir()) / name
    generated = computed["generated"]
    audit_type = computed["audit_type"]

    try:
        if audit_type == "geo":
            part = computed["geo"]
            geo.build_pdf(
                out, part["profile"], part["ai"], part["lines"], part["total"], generated
            )
        elif audit_type == "website":
            part = computed["website"]
            website.build_pdf(
                out,
                part["profile"],
                part["lines"],
                part["total"],
                part["note"] or None,
                generated,
            )
        elif audit_type == "automation":
            part = computed["automation"]
            automation.build_pdf(
                out, part["profile"], part["lines"], part["total"], generated
            )
        else:
            built = pack_sections(computed, sections_for(computed))
            pack.build_pdf(
                out,
                computed["business"],
                computed["trade"],
                computed["area"],
                computed["country"],
                built,
                pack.combined_score(built),
                generated,
            )
        return name, out.read_bytes()
    finally:
        out.unlink(missing_ok=True)
