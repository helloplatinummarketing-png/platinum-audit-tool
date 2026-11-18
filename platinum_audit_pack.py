#!/usr/bin/env python3
"""Platinum Business Audit.

Runs all three audits in one sitting and merges them into a single report:
1. GEO / AI visibility (30% of the combined score),
2. Website health (30%),
3. Automation gaps (40%).

Usage:
    python platinum_audit_pack.py --business "Aqua Plumbing" --url aquaplumbing.co.uk \
        --industry plumber --country UK --area "Bristol"

Run with no flags to be prompted for every value.
"""

from __future__ import annotations

import argparse
import html
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import automation_gap_audit as automation
import geo_audit as geo
import website_audit as website
from geo_audit import (
    BRAND,
    DEFAULT_MODEL,
    GOLD,
    GREY,
    INK,
    LIGHT,
    SILVER,
    WHITE,
    ask,
    breakdown_table,
    cta_block,
    grade,
    load_environment,
    p,
    pdf_styles,
    section,
    slugify,
)

# ---------------------------------------------------------------------------
# Branding and report constants
# ---------------------------------------------------------------------------

REPORT_TITLE = "Platinum Business Audit"

GEO_WEIGHT = 0.30
WEBSITE_WEIGHT = 0.30
AUTOMATION_WEIGHT = 0.40

# The flags each sub-audit's collector reads. The pack leaves them unanswered
# so every signal is prompted for on the call.
GEO_FLAGS = (
    "reviews",
    "last_review",
    "website",
    "faq",
    "directories",
    "gbp_complete",
    "schema",
)
WEBSITE_FLAGS = (
    "mobile",
    "load_time",
    "ssl",
    "title",
    "meta",
    "h1",
    "contact",
    "cta",
    "gbp_linked",
    "schema",
    "testimonials",
    "chat_payment",
)
AUTOMATION_FLAGS = ("avg_job_value", "monthly_leads") + tuple(
    dest for dest, _question, _points, _rate in automation.QUESTIONS
)

# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class AuditSection:
    """One scored audit inside the combined pack."""

    number: int
    title: str
    score: int
    weight: float
    verdict: str
    lines: list = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    fixes: list[tuple[str, str]] = field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------------------
# Combined scoring
# ---------------------------------------------------------------------------


def combined_score(sections: list[AuditSection]) -> int:
    """Weighted average of the three audit scores."""
    return round(sum(part.score * part.weight for part in sections))


def combined_verdict(score: int) -> str:
    """A direct, client-friendly read on the combined score."""
    if score >= 85:
        return "Strong across the board. Protect the position and keep the systems running."
    if score >= 70:
        return "Competitive, but specific gaps are capping growth."
    if score >= 55:
        return (
            "Developing. The business wins work despite its systems rather than "
            "because of them."
        )
    if score >= 40:
        return "At risk. Competitors are more visible and follow up faster."
    return "Critical. Visibility, website and follow-up are all losing customers today."


def priority_plan(
    sections: list[AuditSection], limit: int = 5
) -> list[tuple[str, str, str]]:
    """Rank every gap across all three audits by weighted points lost."""
    ranked: list[tuple[float, str, str, str]] = []
    for part in sections:
        for line in part.lines:
            lost = line.maximum - line.score
            if lost <= 0:
                continue
            ranked.append((lost * part.weight, part.title, line.label, line.fix))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [(area, label, fix) for _, area, label, fix in ranked[:limit]]


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def render_markdown(
    business: str,
    industry: str,
    area: str,
    country: str,
    url: str,
    sections: list[AuditSection],
    total: int,
    generated: date,
) -> str:
    """Build the combined Markdown report."""
    md: list[str] = [
        "# Platinum Business Audit",
        "",
        f"**Business:** {business}  ",
        f"**Website:** {website.display_url(url)}  ",
        f"**Industry:** {industry}  ",
        f"**Area:** {area}  ",
        f"**Country:** {country}  ",
        f"**Audit date:** {generated.strftime('%d %b %Y')}",
        "",
        f"## Combined Score: {total}/100 — Grade {grade(total)}",
        "",
        f"> {combined_verdict(total)}",
        "",
        "| Audit | Score | Weight |",
        "| --- | ---: | ---: |",
    ]

    for part in sections:
        md.append(f"| {part.title} | {part.score}/100 | {part.weight:.0%} |")
    md.append(f"| **Combined** | **{total}/100** | |")

    for part in sections:
        md.extend(
            [
                "",
                f"## Section {part.number}: {part.title}",
                "",
                f"**Score: {part.score}/100 — Grade {grade(part.score)}**",
                "",
                f"> {part.verdict}",
            ]
        )
        if part.note:
            md.extend(["", f"**{part.note}**"])
        md.extend(
            [
                "",
                "| Signal | Score | Max | Detail |",
                "| --- | ---: | ---: | --- |",
            ]
        )
        for line in part.lines:
            md.append(
                f"| {line.label} | {line.score} | {line.maximum} | {line.detail} |"
            )

        md.extend(["", "**What's missing**", ""])
        if part.missing:
            md.extend(f"- {item}" for item in part.missing)
        else:
            md.append("- No critical gaps detected in this area.")

    md.extend(["", "## Priority action plan", ""])
    plan = priority_plan(sections)
    if plan:
        for index, (area_title, label, fix) in enumerate(plan, start=1):
            md.append(f"{index}. **{label}** ({area_title}) — {fix}")
    else:
        md.append("1. **Hold the position** — re-run this audit quarterly.")

    md.extend(
        [
            "",
            "---",
            f"*Prepared by {BRAND} · Combined score weights GEO {GEO_WEIGHT:.0%}, "
            f"Website {WEBSITE_WEIGHT:.0%}, Automation {AUTOMATION_WEIGHT:.0%}.*",
            "",
        ]
    )
    return "\n".join(md)


# ---------------------------------------------------------------------------
# PDF report
# ---------------------------------------------------------------------------


def cover_styles() -> dict[str, ParagraphStyle]:
    """Extra styles used only on the cover page."""
    return {
        "cover_title": ParagraphStyle(
            "cover_title",
            fontName="Helvetica-Bold",
            fontSize=30,
            leading=33,
            textColor=INK,
        ),
        "cover_business": ParagraphStyle(
            "cover_business",
            fontName="Helvetica",
            fontSize=16,
            leading=19,
            textColor=INK,
        ),
        "cover_meta": ParagraphStyle(
            "cover_meta",
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=GREY,
        ),
        "cover_score": ParagraphStyle(
            "cover_score",
            fontName="Helvetica-Bold",
            fontSize=48,
            leading=52,
            textColor=WHITE,
        ),
        "cover_grade": ParagraphStyle(
            "cover_grade",
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            textColor=GOLD,
        ),
        "cover_verdict": ParagraphStyle(
            "cover_verdict",
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            textColor=INK,
        ),
    }


def summary_table(
    sections: list[AuditSection], total: int, styles: dict, width: float
) -> Table:
    """Build the three-audit summary table used on the cover."""
    rows = [
        [
            p("AUDIT", styles["table_head"]),
            p("SCORE", styles["table_head_right"]),
            p("GRADE", styles["table_head_right"]),
            p("WEIGHT", styles["table_head_right"]),
        ]
    ]
    for part in sections:
        rows.append(
            [
                p(part.title, styles["table_cell"]),
                p(f"{part.score}/100", styles["table_cell_right"]),
                p(grade(part.score), styles["table_cell_right"]),
                p(f"{part.weight:.0%}", styles["table_cell_right"]),
            ]
        )
    rows.append(
        [
            p("Combined", styles["table_cell"]),
            p(f"{total}/100", styles["table_cell_right"]),
            p(grade(total), styles["table_cell_right"]),
            p("", styles["table_cell_right"]),
        ]
    )

    table = Table(
        rows,
        colWidths=[width - 90 * mm, 30 * mm, 30 * mm, 30 * mm],
        repeatRows=1,
    )
    last = len(rows) - 1
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), INK),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 1), (-1, last - 1), 0.3, SILVER),
                ("BACKGROUND", (0, last), (-1, last), LIGHT),
                ("LINEABOVE", (0, last), (-1, last), 0.8, INK),
            ]
        )
    )
    return table


def build_cover(
    story: list,
    business: str,
    industry: str,
    area: str,
    country: str,
    sections: list[AuditSection],
    total: int,
    generated: date,
    styles: dict,
    width: float,
) -> None:
    """Add the cover page to the story."""
    covers = cover_styles()

    story.append(p(BRAND.upper(), styles["brand"]))
    story.append(Spacer(1, 2))
    story.append(p("Local search intelligence for the AI era", styles["subtitle"]))
    story.append(Spacer(1, 6))
    story.append(
        HRFlowable(width="100%", thickness=1.4, color=GOLD, spaceBefore=0, spaceAfter=22)
    )

    story.append(p("Business Audit", covers["cover_title"]))
    story.append(Spacer(1, 14))
    story.append(p(business, covers["cover_business"]))
    story.append(Spacer(1, 4))
    story.append(
        p(
            f"{industry} · {area} · {country}",
            covers["cover_meta"],
        )
    )
    story.append(p(generated.strftime("%d %B %Y"), covers["cover_meta"]))
    story.append(Spacer(1, 24))

    score_stack = Table(
        [
            [p("COMBINED SCORE", styles["score_label"])],
            [p(f"{total}/100", covers["cover_score"])],
            [p(f"GRADE {grade(total)}", covers["cover_grade"])],
        ],
        colWidths=[60 * mm],
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

    banner = Table(
        [[score_stack, Paragraph(html.escape(combined_verdict(total)), covers["cover_verdict"])]],
        colWidths=[72 * mm, width - 72 * mm],
    )
    banner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), INK),
                ("BACKGROUND", (1, 0), (1, 0), LIGHT),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (0, 0), 12),
                ("RIGHTPADDING", (0, 0), (0, 0), 12),
                ("LEFTPADDING", (1, 0), (1, 0), 12),
                ("RIGHTPADDING", (1, 0), (1, 0), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 16),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 16),
            ]
        )
    )
    story.append(banner)
    story.append(Spacer(1, 20))
    story.append(summary_table(sections, total, styles, width))


def build_section_page(story: list, part: AuditSection, styles: dict, width: float) -> None:
    """Add one audit's section page to the story."""
    story.extend(section(f"Section {part.number} — {part.title}", styles))

    headline = (
        f"<b>{part.score}/100</b> · Grade {grade(part.score)} · "
        f"{part.weight:.0%} of the combined score"
    )
    story.append(Paragraph(headline, styles["body"]))
    story.append(Spacer(1, 2))
    story.append(p(part.verdict, styles["body"]))
    if part.note:
        story.append(Spacer(1, 2))
        story.append(Paragraph(f"<b>{html.escape(part.note)}</b>", styles["body"]))
    story.append(Spacer(1, 6))

    story.append(breakdown_table(part.lines, styles, width))

    story.extend(section("What's missing", styles))
    if part.missing:
        for item in part.missing:
            story.append(p(f"- {item}", styles["bullet"]))
    else:
        story.append(p("No critical gaps detected in this area.", styles["body"]))

    if part.fixes:
        story.extend(section("Fixes for this area", styles))
        for index, (label, fix) in enumerate(part.fixes, start=1):
            story.append(
                Paragraph(
                    f"<b>{index}. {html.escape(label)}</b> — {html.escape(fix)}",
                    styles["fix"],
                )
            )


def build_pdf(
    path: Path,
    business: str,
    industry: str,
    area: str,
    country: str,
    sections: list[AuditSection],
    total: int,
    generated: date,
) -> None:
    """Render the combined multi-page PDF."""
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
        subject=f"Combined business audit for {business}",
    )
    width = A4[0] - 28 * mm
    story: list = []

    build_cover(
        story, business, industry, area, country, sections, total, generated, styles, width
    )

    for part in sections:
        story.append(PageBreak())
        build_section_page(story, part, styles, width)

    # Priority action plan
    story.append(PageBreak())
    story.extend(section("Priority action plan", styles))
    story.append(
        p(
            "The five highest-value fixes across all three audits, ranked by weighted "
            "points lost.",
            styles["body"],
        )
    )
    story.append(Spacer(1, 6))

    plan = priority_plan(sections)
    if plan:
        for index, (area_title, label, fix) in enumerate(plan, start=1):
            story.append(
                Paragraph(
                    f"<b>{index}. {html.escape(label)}</b> "
                    f"<font size='7' color='#6B7280'>{html.escape(area_title)}</font>"
                    f"<br/>{html.escape(fix)}",
                    styles["fix"],
                )
            )
            story.append(Spacer(1, 4))
    else:
        story.append(
            p("No gaps found. Re-run this audit quarterly to hold the position.", styles["body"])
        )

    story.append(Spacer(1, 12))
    story.append(cta_block("Ready to fix all three?", styles, width))

    def footer(canvas, doc_ref) -> None:
        canvas.saveState()
        canvas.setStrokeColor(SILVER)
        canvas.setLineWidth(0.5)
        canvas.line(14 * mm, 11 * mm, A4[0] - 14 * mm, 11 * mm)
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(GREY)
        canvas.drawString(14 * mm, 7 * mm, f"{REPORT_TITLE} · Private & confidential")
        canvas.drawCentredString(A4[0] / 2, 7 * mm, str(doc_ref.page))
        canvas.drawRightString(
            A4[0] - 14 * mm,
            7 * mm,
            f"{business} · {generated.strftime('%d %b %Y')}",
        )
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)


# ---------------------------------------------------------------------------
# CLI flow
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Platinum Business Audit - run the GEO, website and automation audits "
            "together and produce one combined report."
        ),
        epilog=(
            "Every signal is prompted for in turn: GEO first, then website, then the "
            "nine automation discovery questions."
        ),
    )
    parser.add_argument("--business", help="Business name, e.g. 'Aqua Plumbing'")
    parser.add_argument("--url", help="Website URL, e.g. 'aquaplumbing.co.uk'")
    parser.add_argument("--industry", help="Industry/niche, e.g. 'plumber'")
    parser.add_argument("--country", type=website.parse_country, help="UK or Kenya")
    parser.add_argument("--area", help="Local area for the AI query, e.g. 'Bristol'")
    parser.add_argument(
        "--outdir", default="reports", help="Report output directory (default: reports)"
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Gemini model (default: GEMINI_MODEL or {DEFAULT_MODEL})",
    )
    return parser.parse_args(argv)


def blank(*names: str) -> argparse.Namespace:
    """A namespace of unanswered flags, so each collector prompts for everything."""
    return argparse.Namespace(**{name: None for name in names})


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    load_environment()

    # Step 1: collect the core inputs shared by all three audits.
    business = args.business or ask("Business name")
    url = website.normalize_url(args.url or ask("Website URL"))
    industry = args.industry or ask("Industry / niche")
    area = args.area or ask("Local area (e.g. Bristol)")
    country = args.country or website.ask_country("Country")
    model = args.model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL)

    # Step 2: run the live AI query first, so a failure costs no answered questions.
    print(f"\nAsking AI: Who are the best {industry} in {area}?")
    ai = geo.query_ai(industry, area, model, business)
    print(f"AI result: mentioned = {geo.ai_mention_line(ai).replace('**', '')}")

    # Step 3: GEO signals.
    print("\n--- Section 1 of 3: AI visibility signals ---\n")
    geo_profile = geo.BusinessProfile(
        business=business, trade=industry, area=area, **geo.collect_metrics(blank(*GEO_FLAGS))
    )
    geo_lines = geo.score_audit(geo_profile, ai)
    geo_total = geo.total_score(geo_lines)

    # Step 4: website signals.
    print("\n--- Section 2 of 3: website health signals ---\n")
    web_profile = website.WebsiteProfile(
        business=business,
        url=url,
        industry=industry,
        country=country,
        **website.collect_website_metrics(blank(*WEBSITE_FLAGS), country),
    )
    web_lines = website.score_website(web_profile)
    web_total = website.total_score(web_lines)

    # Step 5: automation discovery questions.
    print("\n--- Section 3 of 3: automation discovery questions ---\n")
    auto_profile = automation.AutomationProfile(
        business=business,
        industry=industry,
        country=country,
        **automation.collect_automation_answers(blank(*AUTOMATION_FLAGS)),
    )
    auto_lines = automation.score_automation(auto_profile)
    auto_total = automation.total_score(auto_lines)
    leak_total = sum(
        amount for _, _, amount in automation.revenue_leaks(auto_profile, auto_lines)
    )
    leak_note = (
        f"Estimated monthly revenue at risk: "
        f"{automation.format_money(leak_total, country)}"
        if leak_total
        else ""
    )

    # Step 6: combine.
    sections = [
        AuditSection(
            number=1,
            title="GEO / AI Visibility",
            score=geo_total,
            weight=GEO_WEIGHT,
            verdict=geo.verdict(geo_total, industry, area),
            lines=geo_lines,
            missing=geo.missing_items(geo_lines),
            fixes=geo.top_fixes(geo_lines),
        ),
        AuditSection(
            number=2,
            title="Website Health",
            score=web_total,
            weight=WEBSITE_WEIGHT,
            verdict=website.verdict(web_total, industry, country),
            lines=web_lines,
            missing=website.missing_items(web_lines),
            fixes=website.top_fixes(web_lines),
        ),
        AuditSection(
            number=3,
            title="Automation Gaps",
            score=auto_total,
            weight=AUTOMATION_WEIGHT,
            verdict=automation.verdict(auto_total, industry),
            lines=auto_lines,
            missing=automation.missing_items(auto_lines),
            fixes=[
                (line.label, line.fix) for line in automation.top_automations(auto_lines)
            ],
            note=leak_note,
        ),
    ]
    total = combined_score(sections)
    generated = date.today()

    # Step 7: write the combined report.
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = f"{slugify(business)}-platinum-business-audit-{generated:%Y%m%d}"
    md_path = outdir / f"{base}.md"
    pdf_path = outdir / f"{base}.pdf"

    md_path.write_text(
        render_markdown(
            business, industry, area, country, url, sections, total, generated
        ),
        encoding="utf-8",
    )
    build_pdf(pdf_path, business, industry, area, country, sections, total, generated)

    print(f"\nCombined score: {total}/100 (Grade {grade(total)})")
    for part in sections:
        print(f"  {part.title}: {part.score}/100 ({part.weight:.0%})")

    if leak_note:
        print(leak_note)
    print(f"Markdown: {md_path.resolve()}")
    print(f"PDF:      {pdf_path.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
