#!/usr/bin/env python3
"""Platinum Website Health Audit.

A small CLI that:
1. collects the website's health signals,
2. asks Gemini for a short industry note,
3. scores website health out of 100, and
4. writes a branded Markdown report and a one-page PDF.

Usage:
    python website_audit.py --business "Aqua Plumbing" --url aquaplumbing.co.uk --industry plumber --country UK

Run with no flags to be prompted for every value.
"""

from __future__ import annotations

import argparse
import html
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from geo_audit import (
    BRAND,
    DEFAULT_MODEL,
    GOLD,
    GREY,
    INK,
    LIGHT,
    SILVER,
    ScoreLine,
    ask,
    ask_bool,
    breakdown_table,
    cta_block,
    get_api_key,
    grade,
    load_environment,
    p,
    parse_bool,
    pdf_styles,
    section,
    slugify,
)

# ---------------------------------------------------------------------------
# Branding and report constants
# ---------------------------------------------------------------------------

REPORT_TITLE = "Platinum Website Health Audit"
COUNTRIES = ("UK", "Kenya")

# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class WebsiteProfile:
    """The website being audited and the signals supplied by the user."""

    business: str
    url: str
    industry: str
    country: str
    mobile_ok: bool
    load_seconds: float
    has_ssl: bool
    has_title: bool
    has_meta: bool
    has_h1: bool
    contact_visible: bool
    has_cta: bool
    gbp_linked: bool
    has_schema: bool
    has_testimonials: bool
    has_chat_payment: bool


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------


def parse_country(value: str) -> str:
    """Argparse-friendly country parser limited to the two served markets."""
    normalized = value.strip().lower()
    if normalized in {"uk", "gb", "gbr", "united kingdom", "britain", "england"}:
        return "UK"
    if normalized in {"kenya", "ke", "ken", "kenyan"}:
        return "Kenya"
    raise argparse.ArgumentTypeError("Use UK or Kenya.")


def ask_country(prompt: str) -> str:
    """Prompt for a country, retrying until it is one we serve."""
    while True:
        try:
            return parse_country(ask(f"{prompt} (UK or Kenya)"))
        except argparse.ArgumentTypeError as exc:
            print(f"  {exc}")


def ask_float(prompt: str, minimum: float = 0.0) -> float:
    """Prompt for a decimal number."""
    while True:
        raw = ask(f"{prompt} (seconds)").replace(",", "")
        try:
            value = float(raw)
            if value < minimum:
                raise ValueError
            return value
        except ValueError:
            print(f"  Enter a number of {minimum:g} or more.")


def normalize_url(value: str) -> str:
    """Add a scheme so the address is clickable in the PDF."""
    url = value.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


def display_url(url: str) -> str:
    """Strip the scheme for a cleaner on-page address."""
    return url.split("://", 1)[-1].rstrip("/")


def join_list(items: list[str]) -> str:
    """Join names the way a sentence would: 'a, b and c'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def chat_payment_label(country: str) -> str:
    """The messaging/payment signal differs between the two markets."""
    if country == "Kenya":
        return "WhatsApp / M-Pesa"
    return "WhatsApp click-to-chat"


# ---------------------------------------------------------------------------
# Scoring (weights total 100)
# ---------------------------------------------------------------------------


def score_website(profile: WebsiteProfile) -> list[ScoreLine]:
    """Score every signal and return the breakdown for the report."""
    lines: list[ScoreLine] = []

    # 1. Mobile responsiveness (15)
    lines.append(
        ScoreLine(
            label="Mobile responsiveness",
            score=15 if profile.mobile_ok else 0,
            maximum=15,
            detail="Responsive" if profile.mobile_ok else "Not responsive",
            missing=(
                ""
                if profile.mobile_ok
                else "The site doesn't adapt to phones — most local searches happen on "
                "mobile, so visitors leave before they read anything."
            ),
            fix=(
                "Rebuild the layout mobile-first: readable text without zooming, tappable "
                "buttons, and no sideways scrolling."
            ),
        )
    )

    # 2. Page speed (15), with 2 seconds treated as the strong benchmark.
    seconds = max(0.0, profile.load_seconds)
    if seconds <= 2.0:
        speed_score = 15
    elif seconds <= 3.0:
        speed_score = 12
    elif seconds <= 4.0:
        speed_score = 8
    elif seconds <= 6.0:
        speed_score = 4
    else:
        speed_score = 0

    lines.append(
        ScoreLine(
            label="Page speed",
            score=speed_score,
            maximum=15,
            detail=f"{seconds:.1f}s load time",
            missing=(
                ""
                if speed_score == 15
                else f"Loads in {seconds:.1f}s — visitors abandon slow pages, and every "
                "extra second hands an enquiry to a faster competitor."
            ),
            fix=(
                "Compress images, enable caching, and cut unused scripts to bring first "
                "load under 2 seconds on mobile data."
            ),
        )
    )

    # 3. SSL / HTTPS (10)
    lines.append(
        ScoreLine(
            label="SSL / HTTPS",
            score=10 if profile.has_ssl else 0,
            maximum=10,
            detail="Secure" if profile.has_ssl else "Not secure",
            missing=(
                ""
                if profile.has_ssl
                else "No HTTPS — browsers show a 'Not secure' warning, which kills trust "
                "before a customer reads a word."
            ),
            fix=(
                "Install an SSL certificate and force every page to redirect to HTTPS."
            ),
        )
    )

    # 4. SEO basics (10): title 4, meta description 3, H1 3.
    seo_parts = [
        ("Title tag", profile.has_title, 4),
        ("Meta description", profile.has_meta, 3),
        ("H1 heading", profile.has_h1, 3),
    ]
    seo_score = sum(points for _, present, points in seo_parts if present)
    present_labels = [label for label, present, _ in seo_parts if present]
    absent_labels = [label for label, present, _ in seo_parts if not present]

    lines.append(
        ScoreLine(
            label="SEO basics",
            score=seo_score,
            maximum=10,
            detail=join_list(present_labels) if present_labels else "None found",
            missing=(
                ""
                if not absent_labels
                else f"Missing {join_list(absent_labels).lower()} — search engines and AI "
                "tools can't tell what the page is for, so they rank it below rivals."
            ),
            fix=(
                "Give every page a unique title tag, a meta description that sells the "
                "click, and a single H1 naming the service and the town."
            ),
        )
    )

    # 5. Contact info visible on the homepage (10)
    lines.append(
        ScoreLine(
            label="Contact info visible",
            score=10 if profile.contact_visible else 0,
            maximum=10,
            detail="Visible" if profile.contact_visible else "Hidden",
            missing=(
                ""
                if profile.contact_visible
                else "Phone and location aren't visible on the homepage — ready-to-buy "
                "visitors give up rather than hunt for a way to reach you."
            ),
            fix=(
                "Put a tappable phone number, service area, and hours in the header of "
                "every page, repeated in the footer."
            ),
        )
    )

    # 6. Clear call-to-action on every page (10)
    lines.append(
        ScoreLine(
            label="Clear call-to-action",
            score=10 if profile.has_cta else 0,
            maximum=10,
            detail="Present" if profile.has_cta else "Not found",
            missing=(
                ""
                if profile.has_cta
                else "No clear next step on every page — interested visitors read, then "
                "leave without enquiring."
            ),
            fix=(
                "Add one obvious action per page — call, book, or request a quote — "
                "repeated above and below the fold."
            ),
        )
    )

    # 7. Google Business Profile linked (10)
    lines.append(
        ScoreLine(
            label="Google Business Profile linked",
            score=10 if profile.gbp_linked else 0,
            maximum=10,
            detail="Linked" if profile.gbp_linked else "Not linked",
            missing=(
                ""
                if profile.gbp_linked
                else "The site and Google Business Profile aren't linked — Google treats "
                "them as separate, weaker signals instead of one strong local presence."
            ),
            fix=(
                "Link the website and Google Business Profile both ways, with matching "
                "name, address, and phone details."
            ),
        )
    )

    # 8. Schema markup (10)
    lines.append(
        ScoreLine(
            label="Schema markup",
            score=10 if profile.has_schema else 0,
            maximum=10,
            detail="Present" if profile.has_schema else "Not found",
            missing=(
                ""
                if profile.has_schema
                else "No schema markup — AI tools can't confirm your services, location, "
                "and hours, so they recommend competitors they can read."
            ),
            fix=(
                "Add LocalBusiness/ProfessionalService JSON-LD schema with name, address, "
                "phone, hours, and services."
            ),
        )
    )

    # 9. Social proof / testimonials (5)
    lines.append(
        ScoreLine(
            label="Social proof",
            score=5 if profile.has_testimonials else 0,
            maximum=5,
            detail="Testimonials shown" if profile.has_testimonials else "None shown",
            missing=(
                ""
                if profile.has_testimonials
                else "No testimonials or reviews on the site — first-time visitors have "
                "nothing to reassure them before they call."
            ),
            fix=(
                "Feature real named reviews and recent job photos on the homepage and "
                "every service page."
            ),
        )
    )

    # 10. Messaging / payment integration (5), weighted to the local market.
    channel = chat_payment_label(profile.country)
    lines.append(
        ScoreLine(
            label=channel,
            score=5 if profile.has_chat_payment else 0,
            maximum=5,
            detail="Integrated" if profile.has_chat_payment else "Not found",
            missing=(
                ""
                if profile.has_chat_payment
                else f"No {channel} on the site — customers who won't fill in a form have "
                "no quick way to start a conversation."
            ),
            fix=(
                "Add a one-tap WhatsApp chat button with a pre-filled message, and show "
                "M-Pesa clearly at the point of payment."
                if profile.country == "Kenya"
                else "Add a one-tap WhatsApp chat button with a pre-filled message so "
                "mobile visitors can enquire without a form."
            ),
        )
    )

    return lines


def total_score(lines: list[ScoreLine]) -> int:
    """Sum the signal scores. The weights are designed to total 100."""
    return sum(line.score for line in lines)


def verdict(score: int, industry: str, country: str) -> str:
    """A direct, client-friendly interpretation of the score."""
    if score >= 85:
        return "Strong. The site converts visitors instead of leaking them."
    if score >= 70:
        return "Solid foundation, but fixable gaps are costing enquiries every week."
    if score >= 55:
        return (
            f"Underperforming. Visitors searching for a {industry} in {country} are "
            "landing here and leaving without making contact."
        )
    if score >= 40:
        return (
            "Weak. The site is actively losing enquiries that your marketing already "
            "paid to win."
        )
    return "Critical. The website is costing you customers rather than winning them."


def missing_items(lines: list[ScoreLine]) -> list[str]:
    """Return missing-signal messages, biggest point loss first."""
    ordered = sorted(lines, key=lambda line: line.maximum - line.score, reverse=True)
    return [line.missing for line in ordered if line.missing]


def top_fixes(lines: list[ScoreLine], limit: int = 3) -> list[tuple[str, str]]:
    """Choose the highest-value fixes, biggest point loss first."""
    gaps = [line for line in lines if line.maximum > line.score]
    if not gaps:
        return [
            ("Keep the site fast", "Re-test load speed quarterly as content is added."),
            ("Refresh proof", "Add new reviews and job photos every month."),
            ("Re-run the audit", "Re-check the site after any redesign or theme update."),
        ][:limit]

    gaps.sort(key=lambda line: line.maximum - line.score, reverse=True)
    return [(line.label, line.fix) for line in gaps[:limit]]


# ---------------------------------------------------------------------------
# Gemini industry note
# ---------------------------------------------------------------------------


def query_industry_note(
    profile: WebsiteProfile, lines: list[ScoreLine], model: str
) -> str | None:
    """Ask Gemini for one sentence on what these gaps cost this kind of business.

    Returns None when there is nothing to say or the API is unavailable; the
    report is complete without it.
    """
    gaps = [line.label for line in lines if line.maximum > line.score]
    if not gaps:
        return None

    client = genai.Client(api_key=get_api_key())
    system_prompt = (
        "You write one plain sentence for a small-business website audit. "
        "No statistics, no invented figures, no marketing cliches. "
        "Maximum 30 words."
    )
    question = (
        f"A {profile.industry} business in {profile.country} has these website "
        f"weaknesses: {join_list(gaps)}. In one sentence, say what this costs them "
        "in customer enquiries."
    )

    try:
        response = client.models.generate_content(
            model=model,
            contents=question,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=200,
                temperature=0,
            ),
        )
    except genai_errors.APIError as exc:
        if exc.code in (401, 403):
            sys.exit("Gemini rejected the API key. Check GEMINI_API_KEY in .env.")
        print(f"Industry note skipped ({exc.code}).", file=sys.stderr)
        return None

    note = (response.text or "").strip()
    return note or None


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def render_markdown(
    profile: WebsiteProfile,
    lines: list[ScoreLine],
    total: int,
    note: str | None,
    generated: date,
) -> str:
    """Build a clean one-page Markdown report."""
    missing = missing_items(lines)
    fixes = top_fixes(lines)

    md: list[str] = [
        "# Platinum Website Health Audit",
        "",
        f"**Business:** {profile.business}  ",
        f"**Website:** {display_url(profile.url)}  ",
        f"**Industry:** {profile.industry}  ",
        f"**Country:** {profile.country}  ",
        f"**Audit date:** {generated.strftime('%d %b %Y')}",
        "",
        f"## Website Health Score: {total}/100 — Grade {grade(total)}",
        "",
        f"> {verdict(total, profile.industry, profile.country)}",
        "",
        "### Site snapshot",
        "",
        f"- **Load time:** {max(0.0, profile.load_seconds):.1f}s",
        f"- **Mobile:** {'Responsive' if profile.mobile_ok else 'Not responsive'}",
        f"- **Secure (HTTPS):** {'Yes' if profile.has_ssl else 'No'}",
    ]

    if note:
        md.extend(["", f"*{note}*"])

    md.extend(
        [
            "",
            "### Score breakdown",
            "",
            "| Signal | Score | Max | Detail |",
            "| --- | ---: | ---: | --- |",
        ]
    )

    for line in lines:
        md.append(f"| {line.label} | {line.score} | {line.maximum} | {line.detail} |")

    md.extend(["", "### What's missing", ""])
    if missing:
        md.extend(f"- {item}" for item in missing)
    else:
        md.append("- No critical gaps detected. Keep the site fast and the proof fresh.")

    md.extend(["", "### Top 3 fixes", ""])
    for index, (title, fix) in enumerate(fixes, start=1):
        md.append(f"{index}. **{title}** — {fix}")

    md.extend(
        [
            "",
            "---",
            f"*Prepared by {BRAND} · Signals recorded at audit time; re-test after any "
            "site change.*",
            "",
        ]
    )
    return "\n".join(md)


# ---------------------------------------------------------------------------
# PDF report
# ---------------------------------------------------------------------------


def build_pdf(
    path: Path,
    profile: WebsiteProfile,
    lines: list[ScoreLine],
    total: int,
    note: str | None,
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
        subject=f"Website health audit for {profile.business}",
    )
    width = A4[0] - 28 * mm
    story: list = []

    # Header
    story.append(
        Table(
            [
                [
                    p(BRAND.upper(), styles["brand"]),
                    p("WEBSITE HEALTH AUDIT", styles["brand_right"]),
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
    story.append(
        HRFlowable(width="100%", thickness=1.2, color=GOLD, spaceBefore=0, spaceAfter=7)
    )

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
                p("WEBSITE", styles["label"]),
                p(display_url(profile.url), styles["meta"]),
                p("INDUSTRY", styles["label"]),
                p(f"{profile.industry} · {profile.country}", styles["meta"]),
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
            [p("WEBSITE HEALTH SCORE", styles["score_label"])],
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
    summary = f"{len(lines)} signals scored · {full_marks} at full marks"
    right_text = (
        f"{html.escape(verdict(total, profile.industry, profile.country))}<br/><br/>"
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

    # Site snapshot
    story.extend(section("Site snapshot", styles))
    story.append(
        Paragraph(
            f"<b>Address:</b> <link href='{html.escape(profile.url)}' color='#14161A'>"
            f"{html.escape(display_url(profile.url))}</link>",
            styles["body"],
        )
    )
    snapshot = (
        f"<b>Load time:</b> {max(0.0, profile.load_seconds):.1f}s · "
        f"<b>Mobile:</b> {'Responsive' if profile.mobile_ok else 'Not responsive'} · "
        f"<b>Secure:</b> {'HTTPS' if profile.has_ssl else 'No HTTPS'}"
    )
    story.append(Paragraph(snapshot, styles["body"]))
    if note:
        story.append(Spacer(1, 1))
        story.append(p(note, styles["tiny"]))

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
                "No critical gaps detected. Keep the site fast and the proof fresh.",
                styles["body"],
            )
        )

    # Top 3 fixes
    story.extend(section("Top 3 fixes", styles))
    for index, (title, fix) in enumerate(top_fixes(lines), start=1):
        story.append(
            Paragraph(
                f"<b>{index}. {html.escape(title)}</b> — {html.escape(fix)}",
                styles["fix"],
            )
        )

    # Call to action
    story.append(Spacer(1, 9))
    story.append(cta_block("Ready to fix your website?", styles, width))

    def footer(canvas, _doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica-Oblique", 6.2)
        canvas.setFillColor(GREY)
        canvas.drawCentredString(
            A4[0] / 2,
            13.2 * mm,
            "Signals recorded at audit time; re-test after any site change.",
        )
        canvas.setStrokeColor(SILVER)
        canvas.setLineWidth(0.5)
        canvas.line(14 * mm, 11 * mm, A4[0] - 14 * mm, 11 * mm)
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(GREY)
        canvas.drawString(14 * mm, 7 * mm, f"{REPORT_TITLE} · Private & confidential")
        canvas.drawRightString(
            A4[0] - 14 * mm,
            7 * mm,
            f"{profile.business} · {generated.strftime('%d %b %Y')}",
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
        description="Platinum Website Health Audit - score a local business's website.",
        epilog=(
            "Run without input flags to be prompted for every value. Each signal is "
            "recorded by the auditor, so check the site on a phone before answering."
        ),
    )
    parser.add_argument("--business", help="Business name, e.g. 'Aqua Plumbing'")
    parser.add_argument("--url", help="Website URL, e.g. 'aquaplumbing.co.uk'")
    parser.add_argument("--industry", help="Industry/niche, e.g. 'plumber'")
    parser.add_argument("--country", type=parse_country, help="UK or Kenya")
    parser.add_argument("--mobile", type=parse_bool, help="Mobile responsive? y/n")
    parser.add_argument(
        "--load-time", dest="load_time", type=float, help="Homepage load time in seconds"
    )
    parser.add_argument("--ssl", type=parse_bool, help="HTTPS enabled? y/n")
    parser.add_argument("--title", type=parse_bool, help="Has a title tag? y/n")
    parser.add_argument("--meta", type=parse_bool, help="Has a meta description? y/n")
    parser.add_argument("--h1", type=parse_bool, help="Has an H1 heading? y/n")
    parser.add_argument(
        "--contact", type=parse_bool, help="Contact info visible on homepage? y/n"
    )
    parser.add_argument("--cta", type=parse_bool, help="Clear CTA on every page? y/n")
    parser.add_argument(
        "--gbp-linked",
        dest="gbp_linked",
        type=parse_bool,
        help="Google Business Profile linked? y/n",
    )
    parser.add_argument(
        "--schema", type=parse_bool, help="Has LocalBusiness schema markup? y/n"
    )
    parser.add_argument(
        "--testimonials", type=parse_bool, help="Testimonials/reviews shown? y/n"
    )
    parser.add_argument(
        "--chat-payment",
        dest="chat_payment",
        type=parse_bool,
        help="WhatsApp chat (and M-Pesa in Kenya) integrated? y/n",
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


def collect_website_metrics(args: argparse.Namespace, country: str) -> dict:
    """Prompt for, or read from flags, the scoring signals."""
    mobile_ok = (
        args.mobile if args.mobile is not None else ask_bool("Is the site mobile responsive")
    )
    load_seconds = (
        args.load_time
        if args.load_time is not None
        else ask_float("Homepage load time")
    )
    has_ssl = args.ssl if args.ssl is not None else ask_bool("Is HTTPS enabled")
    has_title = args.title if args.title is not None else ask_bool("Has a title tag")
    has_meta = args.meta if args.meta is not None else ask_bool("Has a meta description")
    has_h1 = args.h1 if args.h1 is not None else ask_bool("Has an H1 heading")
    contact_visible = (
        args.contact
        if args.contact is not None
        else ask_bool("Is contact info visible on the homepage")
    )
    has_cta = (
        args.cta if args.cta is not None else ask_bool("Is there a clear CTA on every page")
    )
    gbp_linked = (
        args.gbp_linked
        if args.gbp_linked is not None
        else ask_bool("Is the Google Business Profile linked")
    )
    has_schema = (
        args.schema
        if args.schema is not None
        else ask_bool("Does the site have LocalBusiness schema markup")
    )
    has_testimonials = (
        args.testimonials
        if args.testimonials is not None
        else ask_bool("Are testimonials or reviews shown on the site")
    )
    has_chat_payment = (
        args.chat_payment
        if args.chat_payment is not None
        else ask_bool(f"Is {chat_payment_label(country)} integrated")
    )

    return {
        "mobile_ok": mobile_ok,
        "load_seconds": load_seconds,
        "has_ssl": has_ssl,
        "has_title": has_title,
        "has_meta": has_meta,
        "has_h1": has_h1,
        "contact_visible": contact_visible,
        "has_cta": has_cta,
        "gbp_linked": gbp_linked,
        "has_schema": has_schema,
        "has_testimonials": has_testimonials,
        "has_chat_payment": has_chat_payment,
    }


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    load_environment()

    # Step 1: collect the four core inputs.
    business = args.business or ask("Business name")
    url = normalize_url(args.url or ask("Website URL"))
    industry = args.industry or ask("Industry / niche")
    country = args.country or ask_country("Country")
    model = args.model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL)

    # Step 2: collect the website signals.
    print("\nNow enter the website's health signals.\n")
    metrics = collect_website_metrics(args, country)
    profile = WebsiteProfile(
        business=business, url=url, industry=industry, country=country, **metrics
    )

    # Step 3: score, then ask Gemini to frame the gaps for this industry.
    lines = score_website(profile)
    total = total_score(lines)
    note = query_industry_note(profile, lines, model)
    generated = date.today()

    # Step 4: write both report formats.
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = f"{slugify(profile.business)}-website-health-audit-{generated:%Y%m%d}"
    md_path = outdir / f"{base}.md"
    pdf_path = outdir / f"{base}.pdf"

    md_path.write_text(
        render_markdown(profile, lines, total, note, generated), encoding="utf-8"
    )
    build_pdf(pdf_path, profile, lines, total, note, generated)

    print(f"\nScore: {total}/100 (Grade {grade(total)})")
    print(f"Markdown: {md_path.resolve()}")
    print(f"PDF:      {pdf_path.resolve()}")
    if total < 55:
        print("Priority: the website is leaking enquiries that marketing already paid for.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
