#!/usr/bin/env python3
"""Platinum AI Visibility Audit.

A small CLI that:
1. asks Claude who the best businesses are for a trade in an area,
2. collects the business's local-search signals,
3. scores AI visibility out of 100, and
4. writes a branded Markdown report and a one-page PDF.

Usage:
    python geo_audit.py --business "Smile Dental" --trade dentist --area "Westlands, Nairobi"

Run with no flags to be prompted for every value.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ---------------------------------------------------------------------------
# Branding and report constants
# ---------------------------------------------------------------------------

BRAND = "Platinum"
REPORT_TITLE = "Platinum AI Visibility Audit"
DEFAULT_MODEL = "gemini-3.8-flash"
CTA_SITE = "platinummarketingagency.co.uk"
# No public phone number yet, so the CTA band points at the site only. Set this
# to the real number (e.g. "+44 7700 900123") and it is appended automatically.
CTA_PHONE = ""

INK = colors.HexColor("#14161A")
GOLD = colors.HexColor("#B8914B")
SILVER = colors.HexColor("#E5E7EB")
GREY = colors.HexColor("#6B7280")
LIGHT = colors.HexColor("#F6F7F9")
GREEN = colors.HexColor("#0A7A45")
AMBER = colors.HexColor("#B45309")
RED = colors.HexColor("#B42318")
WHITE = colors.white

# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class BusinessProfile:
    """The business being audited and the signals supplied by the user."""

    business: str
    trade: str
    area: str
    review_count: int
    last_review: date | None
    has_website: bool
    has_faq: bool
    directory_count: int
    gbp_complete: bool
    has_schema: bool


@dataclass
class AIResult:
    """The parsed result of the live AI search query."""

    question: str
    answer: str
    mentioned: bool
    position: int | None
    competitors: list[str]


@dataclass
class ScoreLine:
    """One scored signal in the audit."""

    label: str
    score: int
    maximum: int
    detail: str
    missing: str
    fix: str


# ---------------------------------------------------------------------------
# Environment and interactive input
# ---------------------------------------------------------------------------


def load_environment() -> None:
    """Load .env next to this script, regardless of the current directory."""
    load_dotenv(dotenv_path=Path(__file__).resolve().with_name(".env"))


def get_api_key() -> str:
    """Return the Gemini key or exit with a helpful setup message."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    placeholder = key.lower() in {"", "your-key-here", "changeme", "your-api-key-here"}
    if placeholder or key.startswith("your_"):
        env_path = Path(__file__).resolve().with_name(".env")
        sys.exit(
            "Missing GEMINI_API_KEY.\n"
            f"Add it to {env_path} and run the audit again."
        )
    return key


def ask(prompt: str) -> str:
    """Prompt until the user supplies a non-empty value."""
    while True:
        try:
            value = input(f"{prompt}: ").strip()
        except EOFError:
            sys.exit("\nInput cancelled.")
        if value:
            return value
        print("  Please enter a value.")


def ask_int(prompt: str, minimum: int = 0) -> int:
    """Prompt for a whole number."""
    while True:
        raw = ask(f"{prompt} (number)").replace(",", "")
        try:
            value = int(raw)
            if value < minimum:
                raise ValueError
            return value
        except ValueError:
            print(f"  Enter a whole number of {minimum} or more.")


def ask_bool(prompt: str) -> bool:
    """Prompt for a yes/no answer."""
    while True:
        raw = input(f"{prompt} (y/n): ").strip().lower()
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("  Please type y or n.")


def parse_bool(value: str) -> bool:
    """Argparse-friendly yes/no parser."""
    normalized = value.strip().lower()
    if normalized in {"y", "yes", "true", "1"}:
        return True
    if normalized in {"n", "no", "false", "0"}:
        return False
    raise argparse.ArgumentTypeError("Use y or n.")


DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y")


def parse_date_value(value: str) -> date | None:
    """Parse a review date, accepting 'never' for no reviews."""
    cleaned = value.strip().lower()
    if cleaned in {"", "never", "none", "no", "n/a", "na"}:
        return None

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue

    raise ValueError("Use YYYY-MM-DD, DD/MM/YYYY, or 'never'.")


def ask_date(prompt: str) -> date | None:
    """Prompt for a date, retrying until it is valid."""
    while True:
        raw = input(f"{prompt}: ").strip()
        try:
            return parse_date_value(raw)
        except ValueError as exc:
            print(f"  {exc}")


# ---------------------------------------------------------------------------
# AI search query and parsing
# ---------------------------------------------------------------------------

LIST_LINE_RE = re.compile(r"^\s*(?:(\d{1,2})[.)\]]|[-*\u2022])\s+(.+?)\s*$")
LEGAL_SUFFIXES = {
    "ltd",
    "limited",
    "llc",
    "inc",
    "incorporated",
    "plc",
    "co",
    "company",
    "corp",
    "corporation",
}


def normalize_name(value: str) -> str:
    """Lowercase a business name and remove punctuation/legal suffixes."""
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    tokens = [token for token in value.split() if token not in LEGAL_SUFFIXES]
    return " ".join(tokens)


def names_match(first: str, second: str) -> bool:
    """Match names while tolerating suffixes, extra words, and typos."""
    a = normalize_name(first)
    b = normalize_name(second)
    if not a or not b:
        return False
    if a == b:
        return True
    if re.search(rf"\b{re.escape(a)}\b", b) or re.search(rf"\b{re.escape(b)}\b", a):
        return True

    # Token overlap handles "Smile Dental" vs "Smile Dental Clinic".
    a_tokens, b_tokens = set(a.split()), set(b.split())
    if a_tokens and b_tokens:
        overlap = len(a_tokens & b_tokens) / min(len(a_tokens), len(b_tokens))
        if overlap >= 0.75:
            return True

    return SequenceMatcher(None, a, b).ratio() >= 0.88


def parse_ranked_names(answer: str) -> list[str]:
    """Extract ranked business names from a numbered or bulleted answer."""
    names: list[str] = []

    for line in answer.splitlines():
        match = LIST_LINE_RE.match(line)
        if not match:
            continue

        raw = re.sub(r"[*_`]+", "", match.group(2)).strip()
        # Drop short descriptions that follow the business name.
        name = re.split(r"\s+[\u2013\u2014|:]\s+|\s+-\s+", raw, maxsplit=1)[0]
        name = name.strip(" .;")
        if name:
            names.append(name)

    # Fallback for models that return a plain list without bullets.
    if not names:
        for line in answer.splitlines():
            clean = re.sub(r"[*_`]+", "", line).strip(" .;")
            if clean and len(clean) <= 80:
                names.append(clean)

    return names[:10]


def query_ai(trade: str, area: str, model: str, business: str) -> AIResult:
    """Ask the live AI question and record whether the business appears."""
    client = genai.Client(api_key=get_api_key())
    question = f"Who are the best {trade} in {area}? List names."
    system_prompt = (
        "You are an AI search assistant. Return a numbered list of the top 10 "
        f"{trade} in {area}. Use business names only, one per line. "
        "Do not include commentary, links, or explanations."
    )

    try:
        response = client.models.generate_content(
            model=model,
            contents=question,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=700,
                temperature=0,
                # Listing names needs no reasoning, and thinking tokens are
                # billed against max_output_tokens - left on, they can eat the
                # budget and truncate the list the whole audit depends on.
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
    except genai_errors.APIError as exc:
        if exc.code in (401, 403):
            sys.exit("Gemini rejected the API key. Check GEMINI_API_KEY in .env.")
        sys.exit(f"Gemini API error: {exc}")

    answer = (response.text or "").strip()
    if not answer:
        sys.exit("Gemini returned an empty answer.")

    return parse_ai_result(question, answer, business)


def parse_ai_result(question: str, answer: str, business: str = "") -> AIResult:
    """Build an AIResult from the raw answer. Separated for easy testing."""
    names = parse_ranked_names(answer)

    matched_index: int | None = None
    if business:
        for index, name in enumerate(names):
            if names_match(business, name):
                matched_index = index
                break

    position = matched_index + 1 if matched_index is not None else None
    mentioned = position is not None
    if not mentioned and business:
        normalized_answer = normalize_name(answer)
        normalized_business = normalize_name(business)
        mentioned = bool(
            normalized_business
            and re.search(
                rf"\b{re.escape(normalized_business)}\b",
                normalized_answer,
            )
        )

    competitors = [
        name for index, name in enumerate(names) if index != matched_index
    ]

    return AIResult(
        question=question,
        answer=answer,
        mentioned=mentioned,
        position=position,
        competitors=competitors,
    )


# ---------------------------------------------------------------------------
# Scoring (weights total 100)
# ---------------------------------------------------------------------------


def days_ago(review_date: date, today: date) -> int:
    """Return non-negative days since a review date."""
    return max(0, (today - review_date).days)


def score_audit(
    profile: BusinessProfile,
    ai: AIResult,
    today: date | None = None,
) -> list[ScoreLine]:
    """Score every signal and return the breakdown for the report."""
    today = today or date.today()
    lines: list[ScoreLine] = []

    # 1. Google Business Profile completeness (15)
    lines.append(
        ScoreLine(
            label="Google Business Profile",
            score=15 if profile.gbp_complete else 0,
            maximum=15,
            detail="Complete" if profile.gbp_complete else "Incomplete",
            missing=(
                ""
                if profile.gbp_complete
                else "Google Business Profile is incomplete \u2014 customers see a thin "
                "listing and trust a competitor's more complete profile instead."
            ),
            fix=(
                "Complete every field on your Google Business Profile: categories, "
                "services, hours, photos, Q&A, and booking link."
            ),
        )
    )

    # 2. Review volume (15), with 50+ treated as the strong-visibility benchmark.
    review_count = max(0, profile.review_count)
    if review_count >= 50:
        review_score = 15
    elif review_count >= 25:
        review_score = 11
    elif review_count >= 10:
        review_score = 8
    elif review_count >= 1:
        review_score = 4
    else:
        review_score = 0

    lines.append(
        ScoreLine(
            label="Review volume",
            score=review_score,
            maximum=15,
            detail=f"{review_count} Google reviews",
            missing=(
                ""
                if review_score == 15
                else (
                    "No Google reviews \u2014 buyers see zero social proof and choose a competitor."
                    if review_count == 0
                    else f"Only {review_count} Google reviews \u2014 competitors with stronger "
                    "social proof are winning the click."
                )
            ),
            fix="Set up a post-visit review request flow and aim for 50+ Google reviews.",
        )
    )

    # 3. Review recency (10)
    if profile.last_review is None:
        recency_score = 0
        recency_detail = "No recent review date"
        recency_missing = (
            "No recent reviews \u2014 the profile looks dormant, so buyers assume the "
            "business may no longer be active."
        )
    else:
        days = days_ago(profile.last_review, today)
        if days <= 30:
            recency_score = 10
        elif days <= 90:
            recency_score = 8
        elif days <= 180:
            recency_score = 5
        elif days <= 365:
            recency_score = 2
        else:
            recency_score = 0

        recency_detail = "Last review: today" if days == 0 else f"Last review: {days} days ago"
        recency_missing = (
            ""
            if recency_score == 10
            else f"Last review was {days} days ago \u2014 a stale profile signals low demand "
            "and sends buyers to more active competitors."
        )

    lines.append(
        ScoreLine(
            label="Review recency",
            score=recency_score,
            maximum=10,
            detail=recency_detail,
            missing=recency_missing,
            fix="Ask recent customers for reviews this month and keep a monthly review cadence.",
        )
    )

    # 4. Directory listings (15)
    directory_count = max(0, profile.directory_count)
    if directory_count >= 10:
        directory_score = 15
    elif directory_count >= 6:
        directory_score = 12
    elif directory_count >= 3:
        directory_score = 8
    elif directory_count >= 1:
        directory_score = 4
    else:
        directory_score = 0

    lines.append(
        ScoreLine(
            label="Directory listings",
            score=directory_score,
            maximum=15,
            detail=f"{directory_count} directory listings",
            missing=(
                ""
                if directory_score == 15
                else (
                    "No directory listings \u2014 the business is missing from the places "
                    "customers and AI tools use to compare options."
                    if directory_count == 0
                    else f"Only {directory_count} directory listings \u2014 competitors appear "
                    "in more places customers search, so you're losing discovery and trust."
                )
            ),
            fix=(
                "Claim and complete 10+ relevant local and industry directories, keeping "
                "name, address, and phone details consistent."
            ),
        )
    )

    # 5. FAQ page (10)
    lines.append(
        ScoreLine(
            label="FAQ page",
            score=10 if profile.has_faq else 0,
            maximum=10,
            detail="Present" if profile.has_faq else "Not found",
            missing=(
                ""
                if profile.has_faq
                else "No FAQ page \u2014 customers with buying questions are finding "
                "answers on competitor sites instead."
            ),
            fix=(
                "Publish an FAQ page covering pricing, location, hours, services, and "
                "common customer objections."
            ),
        )
    )

    # 6. Schema markup (10)
    lines.append(
        ScoreLine(
            label="Schema markup",
            score=10 if profile.has_schema else 0,
            maximum=10,
            detail="Present" if profile.has_schema else "Not found",
            missing=""
            if profile.has_schema
            else "No schema markup \u2014 AI tools can't easily confirm your services, "
            "location, and hours, so competitors get recommended first.",
            fix=(
                "Add LocalBusiness/ProfessionalService JSON-LD schema with name, address, "
                "phone, hours, and services."
            ),
        )
    )

    # 7. Appears in AI answers (15)
    ranked_total = len(ai.competitors) + (1 if ai.position else 0)
    if not ai.mentioned:
        ai_score = 0
        ai_detail = "Not mentioned"
        ai_missing = (
            f'Not showing up when AI answers "{ai.question}" \u2014 buyers are being '
            "recommended competitors before they ever see you."
        )
        ai_fix = (
            "Publish locally specific answer content and earn citations in the sources "
            "AI assistants trust."
        )
    elif ai.position is None:
        ai_score = 7
        ai_detail = "Mentioned outside ranked list"
        ai_missing = (
            "Mentioned outside the AI ranked list \u2014 buyers may never see the recommendation."
        )
        ai_fix = "Strengthen local content and citations so AI names the business in its top 3."
    else:
        if ai.position <= 3:
            ai_score = 15
        elif ai.position <= 5:
            ai_score = 11
        else:
            ai_score = 7
        ai_detail = f"Mentioned at #{ai.position} of {ranked_total}"
        ai_missing = (
            ""
            if ai_score == 15
            else f"Ranked #{ai.position} in AI answers \u2014 buyers see competitors first, "
            "and most never scroll past the top recommendations."
        )
        ai_fix = (
            "Strengthen local content and citations so AI moves the business into its top 3."
        )

    lines.append(
        ScoreLine(
            label="Appears in AI answers",
            score=ai_score,
            maximum=15,
            detail=ai_detail,
            missing=ai_missing,
            fix=ai_fix,
        )
    )

    # 8. Competitive position in the AI answer (10)
    if ai.position is None:
        competitor_score = 3 if ai.mentioned else 0
        competitor_detail = (
            "Outside ranked list" if ai.mentioned else "Absent from ranked answer"
        )
        competitor_missing = (
            "Not ranked against AI-listed competitors \u2014 buyers are being guided to "
            "named alternatives."
            if ai.mentioned
            else "Absent from the AI competitor set \u2014 every recommendation in this "
            "search sends a lead to someone else."
        )
    else:
        if ai.position <= 3:
            competitor_score = 10
        elif ai.position <= 6:
            competitor_score = 7
        else:
            competitor_score = 4
        ahead = max(0, ranked_total - ai.position)
        suffix = "" if ahead == 1 else "s"
        competitor_detail = f"Ahead of {ahead} competitor{suffix}"
        competitor_missing = (
            ""
            if competitor_score == 10
            else "Outside the AI top 3 \u2014 competitors are framed as the best choice "
            "while you're an afterthought."
        )

    lines.append(
        ScoreLine(
            label="Competitive position",
            score=competitor_score,
            maximum=10,
            detail=competitor_detail,
            missing=competitor_missing,
            fix=(
                "Benchmark the top 3 AI-listed competitors and close gaps in reviews, "
                "listings, and answer content."
            ),
        )
    )

    return lines


def total_score(lines: list[ScoreLine]) -> int:
    """Sum the signal scores. The weights are designed to total 100."""
    return sum(line.score for line in lines)


def grade(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def verdict(score: int, trade: str, area: str) -> str:
    """A direct, client-friendly interpretation of the score."""
    if score >= 85:
        return "Dominant. You're showing up first when customers ask AI for help."
    if score >= 70:
        return "Solid foundation, but competitors are closing the gap."
    if score >= 55:
        return (
            "Your competitors are showing up before you. Customers asking AI for the "
            f"best {trade} in {area} are being sent elsewhere first."
        )
    if score >= 40:
        return (
            "Almost invisible. AI doesn't know you exist \u2014 your competitors are "
            "taking your leads."
        )
    return "Invisible to AI. Every lead is going to your competitors right now."


def missing_items(lines: list[ScoreLine]) -> list[str]:
    """Return missing-signal messages, biggest point loss first."""
    # The AI mention and competitor rank overlap; show the AI message only once.
    ai_line = next((line for line in lines if line.label == "Appears in AI answers"), None)
    visible = [
        line
        for line in lines
        if not (
            line.label == "Competitive position"
            and ai_line is not None
            and ai_line.missing
        )
    ]
    ordered = sorted(visible, key=lambda line: line.maximum - line.score, reverse=True)
    return [line.missing for line in ordered if line.missing]


def top_fixes(lines: list[ScoreLine]) -> list[tuple[str, str]]:
    """Choose the three highest-value fixes, merging the two AI signals."""
    gaps = [line for line in lines if line.maximum > line.score]
    if not gaps:
        return [
            ("Protect review velocity", "Keep asking every customer for a review so the profile stays active."),
            ("Refresh local content", "Update the FAQ and service pages quarterly with local proof points."),
            ("Re-run the audit", "Track AI answers monthly and respond quickly to competitor movement."),
        ]

    ai_labels = {"Appears in AI answers", "Competitive position"}
    ai_gaps = [line for line in gaps if line.label in ai_labels]
    other_gaps = [line for line in gaps if line.label not in ai_labels]

    ranked: list[tuple[int, str, str]] = []
    if ai_gaps:
        lost = sum(line.maximum - line.score for line in ai_gaps)
        ranked.append(
            (
                lost,
                "Win the AI answer",
                (
                    "Publish locally specific proof, FAQs, and comparison content, and earn "
                    "citations in the sources AI assistants trust. Benchmark the top 3 listed competitors."
                ),
            )
        )

    ranked.extend(
        (line.maximum - line.score, line.label, line.fix) for line in other_gaps
    )
    ranked.sort(key=lambda item: item[0], reverse=True)

    maintenance = [
        ("Keep review velocity", "Maintain a monthly review-request habit."),
        ("Refresh local content", "Update the FAQ and service pages with fresh local proof."),
        ("Re-run the audit", "Track AI answers monthly and watch competitor movement."),
    ]
    while len(ranked) < 3:
        ranked.append((0, *maintenance[len(ranked)]))

    return [(title, fix) for _, title, fix in ranked[:3]]


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "business"


def ai_mention_line(ai: AIResult) -> str:
    ranked_total = len(ai.competitors) + (1 if ai.position else 0)
    if ai.position:
        return f"Yes - position **{ai.position} of {ranked_total}**"
    if ai.mentioned:
        return "Yes - mentioned outside the ranked list"
    return "No"


def render_markdown(
    profile: BusinessProfile,
    ai: AIResult,
    lines: list[ScoreLine],
    total: int,
    generated: date,
) -> str:
    """Build a clean one-page Markdown report."""
    competitors = ", ".join(ai.competitors[:5])
    if len(ai.competitors) > 5:
        competitors += ", \u2026"
    if not competitors:
        competitors = "None parsed"

    missing = missing_items(lines)
    fixes = top_fixes(lines)

    md: list[str] = [
        "# Platinum AI Visibility Audit",
        "",
        f"**Business:** {profile.business}  ",
        f"**Trade:** {profile.trade}  ",
        f"**Area:** {profile.area}  ",
        f"**Audit date:** {generated.strftime('%d %b %Y')}",
        "",
        f"## AI Visibility Score: {total}/100 \u2014 Grade {grade(total)}",
        "",
        f"> {verdict(total, profile.trade, profile.area)}",
        "",
        "### AI search snapshot",
        "",
        f'**Question asked:** "{ai.question}"',
        "",
        f"- **Mentioned:** {ai_mention_line(ai)}",
        f"- **Website:** {'Yes' if profile.has_website else 'No'}",
        f"- **Competitors listed:** {competitors}",
        "",
        "### Score breakdown",
        "",
        "| Signal | Score | Max | Detail |",
        "| --- | ---: | ---: | --- |",
    ]

    for line in lines:
        md.append(
            f"| {line.label} | {line.score} | {line.maximum} | {line.detail} |"
        )

    md.extend(["", "### What's missing", ""])
    if missing:
        md.extend(f"- {item}" for item in missing)
    else:
        md.append("- No critical gaps detected. Protect the position with fresh reviews and content.")

    md.extend(["", "### Top 3 fixes", ""])
    for index, (title, fix) in enumerate(fixes, start=1):
        md.append(f"{index}. **{title}** \u2014 {fix}")

    md.extend(
        [
            "",
            "---",
            f"*Prepared by {BRAND} \u00b7 AI answers vary between runs; this audit reflects one live query.*",
            "",
        ]
    )
    return "\n".join(md)


# ---------------------------------------------------------------------------
# PDF report
# ---------------------------------------------------------------------------


def pdf_styles() -> dict[str, ParagraphStyle]:
    """Compact styles sized to keep the report on one A4 page."""
    return {
        "brand": ParagraphStyle(
            "brand", fontName="Helvetica-Bold", fontSize=18, leading=20, textColor=INK
        ),
        "brand_right": ParagraphStyle(
            "brand_right",
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=GOLD,
            alignment=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", fontName="Helvetica", fontSize=7.5, leading=9, textColor=GREY
        ),
        "label": ParagraphStyle(
            "label", fontName="Helvetica-Bold", fontSize=6.5, leading=8, textColor=GREY
        ),
        "meta": ParagraphStyle(
            "meta", fontName="Helvetica", fontSize=8.5, leading=11, textColor=INK
        ),
        "section": ParagraphStyle(
            "section",
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=11,
            textColor=INK,
            spaceBefore=7,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "body", fontName="Helvetica", fontSize=8.4, leading=11, textColor=INK
        ),
        "body_small": ParagraphStyle(
            "body_small", fontName="Helvetica", fontSize=8, leading=10, textColor=INK
        ),
        "tiny": ParagraphStyle(
            "tiny", fontName="Helvetica-Oblique", fontSize=6.8, leading=8.5, textColor=GREY
        ),
        "banner_text": ParagraphStyle(
            "banner_text", fontName="Helvetica", fontSize=9, leading=12.5, textColor=INK
        ),
        "banner_small": ParagraphStyle(
            "banner_small", fontName="Helvetica", fontSize=7.2, leading=9, textColor=GREY
        ),
        "score_label": ParagraphStyle(
            "score_label",
            fontName="Helvetica-Bold",
            fontSize=6.3,
            leading=8,
            textColor=SILVER,
        ),
        "score_big": ParagraphStyle(
            "score_big",
            fontName="Helvetica-Bold",
            fontSize=29,
            leading=31,
            textColor=WHITE,
        ),
        "grade_light": ParagraphStyle(
            "grade_light",
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=GOLD,
        ),
        "table_head": ParagraphStyle(
            "table_head",
            fontName="Helvetica-Bold",
            fontSize=6.8,
            leading=8,
            textColor=WHITE,
        ),
        "table_head_right": ParagraphStyle(
            "table_head_right",
            fontName="Helvetica-Bold",
            fontSize=6.8,
            leading=8,
            textColor=WHITE,
            alignment=2,
        ),
        "table_cell": ParagraphStyle(
            "table_cell", fontName="Helvetica", fontSize=7.8, leading=9.5, textColor=INK
        ),
        "table_cell_right": ParagraphStyle(
            "table_cell_right",
            fontName="Helvetica-Bold",
            fontSize=7.8,
            leading=9.5,
            textColor=INK,
            alignment=2,
        ),
        "table_cell_small": ParagraphStyle(
            "table_cell_small",
            fontName="Helvetica",
            fontSize=7.2,
            leading=9,
            textColor=INK,
        ),
        "bullet": ParagraphStyle(
            "bullet",
            fontName="Helvetica",
            fontSize=8,
            leading=10.5,
            textColor=INK,
            leftIndent=9,
            firstLineIndent=-9,
            spaceAfter=2.5,
        ),
        "fix": ParagraphStyle(
            "fix",
            fontName="Helvetica",
            fontSize=8,
            leading=10.5,
            textColor=INK,
            leftIndent=10,
            firstLineIndent=-10,
            spaceAfter=3,
        ),
        "cta": ParagraphStyle(
            "cta", fontName="Helvetica", fontSize=8.8, leading=12, textColor=INK
        ),
    }


def p(text: str, style: ParagraphStyle) -> Paragraph:
    """Shorthand for an escaped Paragraph."""
    return Paragraph(html.escape(text), style)


def cta_block(headline: str, styles: dict[str, ParagraphStyle], width: float) -> Table:
    """Return the gold call-to-action band used at the foot of every report."""
    cta_text = (
        f"<b>{html.escape(headline)}</b><br/>"
        "Contact us at "
        f"<link href='https://{CTA_SITE}' color='#14161A'>{CTA_SITE}</link>"
    )
    if CTA_PHONE.strip():
        cta_text += f" · {html.escape(CTA_PHONE)}"
    cta = Table([[Paragraph(cta_text, styles["cta"])]], colWidths=[width])
    cta.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), GOLD),
                ("LINEBEFORE", (0, 0), (0, -1), 3, INK),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return cta


def section(title: str, styles: dict[str, ParagraphStyle]) -> list:
    """Return a small section heading with a gold rule."""
    return [
        Spacer(1, 1),
        p(title.upper(), styles["section"]),
        HRFlowable(width="100%", thickness=0.6, color=GOLD, spaceBefore=0, spaceAfter=4),
    ]


def breakdown_table(
    lines: list[ScoreLine], styles: dict[str, ParagraphStyle], width: float
) -> Table:
    """Build the compact score breakdown table."""
    rows = [
        [
            p("SIGNAL", styles["table_head"]),
            p("SCORE", styles["table_head_right"]),
            p("MAX", styles["table_head_right"]),
            p("DETAIL", styles["table_head"]),
        ]
    ]
    for line in lines:
        rows.append(
            [
                p(line.label, styles["table_cell"]),
                p(str(line.score), styles["table_cell_right"]),
                p(str(line.maximum), styles["table_cell_right"]),
                p(line.detail, styles["table_cell_small"]),
            ]
        )

    table = Table(
        rows,
        colWidths=[55 * mm, 14 * mm, 12 * mm, width - 81 * mm],
        repeatRows=1,
    )
    style = TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), INK),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("LINEBELOW", (0, 1), (-1, -1), 0.3, SILVER),
        ]
    )

    for index, line in enumerate(lines, start=1):
        if index % 2 == 0:
            style.add("BACKGROUND", (0, index), (-1, index), LIGHT)
        ratio = line.score / line.maximum if line.maximum else 0
        score_color = GREEN if ratio >= 0.999 else AMBER if ratio >= 0.5 else RED
        style.add("TEXTCOLOR", (1, index), (1, index), score_color)

    table.setStyle(style)
    return table


def build_pdf(
    path: Path,
    profile: BusinessProfile,
    ai: AIResult,
    lines: list[ScoreLine],
    total: int,
    generated: date,
) -> None:
    """Render the branded one-page PDF."""
    styles = pdf_styles()
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=12 * mm,
        bottomMargin=16 * mm,
        title=REPORT_TITLE,
        author=BRAND,
        subject=f"AI visibility audit for {profile.business}",
    )
    width = A4[0] - 28 * mm
    story: list = []

    # Header
    story.append(
        Table(
            [
                [
                    p(BRAND.upper(), styles["brand"]),
                    p("AI VISIBILITY AUDIT", styles["brand_right"]),
                ]
            ],
            colWidths=[width * 0.58, width * 0.42],
            style=TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                ]
            ),
        )
    )
    story.append(Spacer(1, 2))
    story.append(p("Local search intelligence for the AI era", styles["subtitle"]))
    story.append(Spacer(1, 5))
    story.append(HRFlowable(width="100%", thickness=1.2, color=GOLD, spaceBefore=0, spaceAfter=7))

    # Business metadata
    meta = Table(
        [
            [
                p("BUSINESS", styles["label"]),
                p(profile.business, styles["meta"]),
                p("AUDIT DATE", styles["label"]),
                p(generated.strftime("%d %b %Y"), styles["meta"]),
            ],
            [
                p("TRADE", styles["label"]),
                p(profile.trade, styles["meta"]),
                p("AREA", styles["label"]),
                p(profile.area, styles["meta"]),
            ],
        ],
        colWidths=[17 * mm, 67 * mm, 20 * mm, width - 104 * mm],
    )
    meta.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(meta)
    story.append(Spacer(1, 8))

    # Score banner
    score_stack = Table(
        [
            [p("AI VISIBILITY SCORE", styles["score_label"])],
            [p(f"{total}/100", styles["score_big"])],
            [p(f"GRADE {grade(total)}", styles["grade_light"])],
        ],
        colWidths=[44 * mm],
    )
    score_stack.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    full_marks = sum(1 for line in lines if line.score == line.maximum)
    summary = f"{len(lines)} signals scored \u00b7 {full_marks} at full marks"
    right_text = (
        f"{html.escape(verdict(total, profile.trade, profile.area))}<br/><br/>"
        f"<font size='7.2' color='#6B7280'>{html.escape(summary)}</font>"
    )

    banner = Table(
        [[score_stack, Paragraph(right_text, styles["banner_text"])]],
        colWidths=[52 * mm, width - 52 * mm],
    )
    banner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), INK),
                ("BACKGROUND", (1, 0), (1, 0), LIGHT),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (0, 0), 8),
                ("RIGHTPADDING", (0, 0), (0, 0), 8),
                ("LEFTPADDING", (1, 0), (1, 0), 10),
                ("RIGHTPADDING", (1, 0), (1, 0), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ]
        )
    )
    story.append(banner)

    # AI search snapshot
    story.extend(section("AI search snapshot", styles))
    story.append(p(f'Question asked: "{ai.question}"', styles["body"]))
    story.append(
        Paragraph(
            f"<b>Mentioned:</b> {html.escape(ai_mention_line(ai).replace('**', ''))}",
            styles["body"],
        )
    )
    competitors = ", ".join(ai.competitors[:5])
    if len(ai.competitors) > 5:
        competitors += ", \u2026"
    story.append(
        Paragraph(
            f"<b>Competitors listed:</b> {html.escape(competitors or 'None parsed')}",
            styles["body"],
        )
    )

    # Score breakdown
    story.extend(section("Score breakdown", styles))
    story.append(breakdown_table(lines, styles, width))

    # What's missing
    story.extend(section("What's missing", styles))
    missing = missing_items(lines)
    if missing:
        for item in missing:
            story.append(p(f"- {item}", styles["bullet"]))
    else:
        story.append(
            p(
                "No critical gaps detected. Protect the position with fresh reviews and content.",
                styles["body"],
            )
        )

    # Top 3 fixes
    story.extend(section("Top 3 fixes", styles))
    for index, (title, fix) in enumerate(top_fixes(lines), start=1):
        story.append(
            Paragraph(
                f"<b>{index}. {html.escape(title)}</b> \u2014 {html.escape(fix)}",
                styles["fix"],
            )
        )

    # Call to action
    story.append(Spacer(1, 9))
    story.append(cta_block("Ready to fix your AI visibility?", styles, width))

    def footer(canvas, _doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica-Oblique", 6.2)
        canvas.setFillColor(GREY)
        canvas.drawCentredString(
            A4[0] / 2,
            13.2 * mm,
            "AI answers vary between runs; this reflects one live query at audit time.",
        )
        canvas.setStrokeColor(SILVER)
        canvas.setLineWidth(0.5)
        canvas.line(14 * mm, 11 * mm, A4[0] - 14 * mm, 11 * mm)
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(GREY)
        canvas.drawString(14 * mm, 7 * mm, f"{REPORT_TITLE} \u00b7 Private & confidential")
        canvas.drawRightString(
            A4[0] - 14 * mm,
            7 * mm,
            f"{profile.business} \u00b7 {generated.strftime('%d %b %Y')}",
        )
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)

    if doc.page > 1:
        print(
            "Warning: the PDF used more than one page. Shorten long answers or reduce "
            "the report content to restore the one-page layout.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# CLI flow
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Platinum AI Visibility Audit - score a local business's AI search visibility.",
        epilog=(
            "Run without input flags to be prompted for every value. GBP completeness "
            "and schema markup are extra prompts because they carry score weight but "
            "cannot be inferred from the other inputs."
        ),
    )
    parser.add_argument("--business", help="Business name, e.g. 'Smile Dental'")
    parser.add_argument("--trade", help="Trade/category, e.g. 'dentist'")
    parser.add_argument("--area", help="Area, e.g. 'Westlands, Nairobi'")
    parser.add_argument("--reviews", type=int, help="Google review count")
    parser.add_argument(
        "--last-review",
        dest="last_review",
        help="Date of last review (YYYY-MM-DD) or 'never'",
    )
    parser.add_argument("--website", type=parse_bool, help="Has website? y/n")
    parser.add_argument("--faq", type=parse_bool, help="Has FAQ page? y/n")
    parser.add_argument(
        "--directories", type=int, help="Number of directory listings"
    )
    parser.add_argument(
        "--gbp-complete",
        dest="gbp_complete",
        type=parse_bool,
        help="Is the Google Business Profile complete? y/n",
    )
    parser.add_argument(
        "--schema",
        type=parse_bool,
        help="Has LocalBusiness schema markup? y/n",
    )
    parser.add_argument(
        "--outdir", default="reports", help="Report output directory (default: reports)"
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Gemini model (default: GEMINI_MODEL or {DEFAULT_MODEL})",
    )
    return parser.parse_args(argv)


def collect_metrics(args: argparse.Namespace) -> dict:
    """Prompt for, or read from flags, the scoring signals."""
    review_count = (
        args.reviews if args.reviews is not None else ask_int("Google review count")
    )

    if args.last_review is not None:
        try:
            last_review = parse_date_value(args.last_review)
        except ValueError as exc:
            sys.exit(f"--last-review: {exc}")
    else:
        last_review = ask_date("Last review date (YYYY-MM-DD or 'never')")

    has_website = (
        args.website
        if args.website is not None
        else ask_bool("Has website")
    )
    has_faq = args.faq if args.faq is not None else ask_bool("Has FAQ page")
    directory_count = (
        args.directories
        if args.directories is not None
        else ask_int("Number of directory listings")
    )
    gbp_complete = (
        args.gbp_complete
        if args.gbp_complete is not None
        else ask_bool("Is the Google Business Profile complete")
    )
    has_schema = (
        args.schema
        if args.schema is not None
        else ask_bool("Does the website have LocalBusiness schema markup")
    )

    return {
        "review_count": review_count,
        "last_review": last_review,
        "has_website": has_website,
        "has_faq": has_faq,
        "directory_count": directory_count,
        "gbp_complete": gbp_complete,
        "has_schema": has_schema,
    }


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    load_environment()

    # Step 1: collect the three core inputs, then run the live AI query.
    business = args.business or ask("Business name")
    trade = args.trade or ask("Trade / category")
    area = args.area or ask("Area (e.g. Westlands, Nairobi)")
    model = args.model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL)

    print(f"\nAsking AI: Who are the best {trade} in {area}?")
    ai = query_ai(trade, area, model, business)

    mention = ai_mention_line(ai).replace("**", "")
    print(f"AI result: mentioned = {mention}")

    # Step 2: collect the local-search signals.
    print("\nNow enter the business's visibility signals.\n")
    metrics = collect_metrics(args)
    profile = BusinessProfile(business=business, trade=trade, area=area, **metrics)

    # Step 3: score.
    lines = score_audit(profile, ai)
    total = total_score(lines)
    generated = date.today()

    # Step 4: write both report formats.
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = f"{slugify(profile.business)}-ai-visibility-audit-{generated:%Y%m%d}"
    md_path = outdir / f"{base}.md"
    pdf_path = outdir / f"{base}.pdf"

    md_path.write_text(
        render_markdown(profile, ai, lines, total, generated), encoding="utf-8"
    )
    build_pdf(pdf_path, profile, ai, lines, total, generated)

    print(f"\nScore: {total}/100 (Grade {grade(total)})")
    print(f"Markdown: {md_path.resolve()}")
    print(f"PDF:      {pdf_path.resolve()}")
    if total < 55:
        print("Priority: this account needs visible fixes before pitching AI visibility.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
