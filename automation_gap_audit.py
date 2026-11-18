#!/usr/bin/env python3
"""Platinum Automation Gap Audit.

A small CLI that:
1. asks nine yes/no discovery questions about the business's follow-up systems,
2. scores automation maturity out of 100,
3. estimates the monthly revenue leaking through the gaps, and
4. writes a branded Markdown report and PDF with n8n workflow recommendations.

Usage:
    python automation_gap_audit.py --business "Aqua Plumbing" --industry plumber \
        --country UK --avg-job-value 450 --monthly-leads 60

Run with no flags to be prompted for every value.
"""

from __future__ import annotations

import argparse
import html
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from geo_audit import (
    BRAND,
    GOLD,
    GREY,
    INK,
    LIGHT,
    SILVER,
    ask,
    ask_bool,
    ask_int,
    breakdown_table,
    cta_block,
    grade,
    p,
    parse_bool,
    pdf_styles,
    section,
    slugify,
)
from website_audit import ask_country, ask_float, parse_country

# ---------------------------------------------------------------------------
# Branding and report constants
# ---------------------------------------------------------------------------

REPORT_TITLE = "Platinum Automation Gap Audit"

# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class AutomationProfile:
    """The business being audited and its discovery-call answers."""

    business: str
    industry: str
    country: str
    avg_job_value: float
    monthly_leads: int
    fast_response: bool
    chases_leads: bool
    appointment_system: bool
    review_requests: bool
    quote_followup: bool
    invoice_chasing: bool
    repeat_campaigns: bool
    messaging_automation: bool
    owner_alerts: bool


@dataclass
class AutomationLine:
    """One scored gap, with the n8n build that closes it.

    Mirrors geo_audit.ScoreLine so the shared breakdown_table can render it,
    and adds the workflow description and revenue-leak rate.
    """

    label: str
    score: int
    maximum: int
    detail: str
    missing: str
    fix: str
    n8n: str
    leak_rate: float


# ---------------------------------------------------------------------------
# Discovery questions
# ---------------------------------------------------------------------------

# (dest, question, points, leak rate). The leak rate is the share of monthly
# enquiries this gap is assumed to cost; 0 means the gap hurts in ways other
# than lost new work, so it carries no revenue figure.
QUESTIONS: list[tuple[str, str, int, float]] = [
    (
        "fast_response",
        "Does every new enquiry get a reply within 5 minutes, automatically",
        15,
        0.12,
    ),
    (
        "chases_leads",
        "Are leads who don't book chased automatically",
        15,
        0.08,
    ),
    (
        "appointment_system",
        "Do booked jobs get automatic reminders, with no-show recovery",
        15,
        0.04,
    ),
    (
        "review_requests",
        "Is a review request sent automatically after every completed job",
        10,
        0.02,
    ),
    (
        "quote_followup",
        "Are sent quotes chased automatically when there's no response",
        10,
        0.06,
    ),
    (
        "invoice_chasing",
        "Do overdue invoices trigger automatic payment reminders",
        10,
        0.0,
    ),
    (
        "repeat_campaigns",
        "Are past customers re-engaged automatically",
        10,
        0.04,
    ),
    (
        "messaging_automation",
        "Is any customer messaging automated (WhatsApp or SMS)",
        10,
        0.0,
    ),
    (
        "owner_alerts",
        "Does the owner get notified automatically when something important happens",
        5,
        0.0,
    ),
]


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------


def format_money(amount: float, country: str) -> str:
    """Format a figure in the local currency."""
    if country == "Kenya":
        return f"KSh {amount:,.0f}"
    return f"£{amount:,.0f}"


# ---------------------------------------------------------------------------
# Scoring (weights total 100)
# ---------------------------------------------------------------------------


def score_automation(profile: AutomationProfile) -> list[AutomationLine]:
    """Score every discovery answer and return the breakdown for the report."""
    channel = "WhatsApp" if profile.country == "Kenya" else "SMS"

    specs: list[tuple[str, str, str, str, str]] = [
        (
            "fast_response",
            "Lead response speed",
            "Enquiries sit unanswered — the first business to reply usually wins the "
            "job, and yours is replying last.",
            "Acknowledge every enquiry within seconds, around the clock.",
            f"Webhook on the contact form, missed calls and Google Business messages "
            f"→ instant {channel} acknowledgement to the lead → job card to the "
            "owner. Fires in seconds, day or night.",
        ),
        (
            "chases_leads",
            "Lead follow-up",
            "Leads who don't book straight away are never chased — that work is "
            "simply abandoned after one attempt.",
            "Run a three-touch follow-up sequence on every unconverted lead.",
            "Scheduled sequence: no booking after 24h sends follow-up 1, day 3 sends "
            "follow-up 2, day 7 sends a final offer. The sequence stops the moment "
            "they reply or book.",
        ),
        (
            "appointment_system",
            "Appointment management",
            "No automatic reminders — no-shows burn diary slots that could have been "
            "sold to someone else.",
            "Send booking reminders and recover no-shows the same day.",
            "Calendar trigger → reminder 24h and 2h before the slot → if marked "
            "no-show, send a one-tap rebooking link within the hour.",
        ),
        (
            "review_requests",
            "Review collection",
            "Reviews depend on someone remembering to ask — so most happy customers "
            "never leave one.",
            "Ask every finished job for a review, automatically.",
            f"Job marked complete → 2h delay → {channel} with a one-tap Google "
            "review link → single reminder after 3 days if no review lands.",
        ),
        (
            "quote_followup",
            "Quote follow-up",
            "Sent quotes go quiet and stay quiet — priced work is lost without anyone "
            "finding out why.",
            "Chase every quote on a fixed schedule until you get an answer.",
            "Quote-sent trigger → chase on day 2, day 5 and day 10 → alert the "
            "owner when a quote is opened but not accepted.",
        ),
        (
            "invoice_chasing",
            "Invoice chasing",
            "Overdue invoices are chased by hand, so cash arrives late and some of it "
            "never arrives at all.",
            "Automate the payment reminder ladder.",
            "Daily check over unpaid invoices → polite reminder at day 1, 7 and 14 "
            "past due → escalation to the owner at day 21.",
        ),
        (
            "repeat_campaigns",
            "Repeat business",
            "Past customers are never contacted again — the cheapest work you can win "
            "is being left on the table.",
            "Re-engage past customers on a schedule.",
            "Scheduled campaign: service anniversary and seasonal reminders, segmented "
            "by job type, each with a direct booking link.",
        ),
        (
            "messaging_automation",
            f"{channel} automation",
            f"No automated {channel} — messages that customers actually open are being "
            "sent by hand, or not at all.",
            f"Put {channel} behind every customer touchpoint.",
            f"{channel} Business API wired into every workflow above, so "
            "acknowledgements, reminders and review requests all go out on the channel "
            "customers actually read.",
        ),
        (
            "owner_alerts",
            "Internal alerts",
            "Nothing tells the owner when something needs attention — problems are "
            "found late, by accident.",
            "Route the events that matter straight to the owner's phone.",
            "Priority routing: high-value enquiry, missed call, negative review or "
            "failed payment → instant push to the owner's phone or a team channel.",
        ),
    ]

    spec_by_dest = {spec[0]: spec[1:] for spec in specs}
    lines: list[AutomationLine] = []
    for dest, _question, points, leak_rate in QUESTIONS:
        label, missing, fix, workflow = spec_by_dest[dest]
        automated = getattr(profile, dest)
        lines.append(
            AutomationLine(
                label=label,
                score=points if automated else 0,
                maximum=points,
                detail="Automated" if automated else "Manual / none",
                missing="" if automated else missing,
                fix=fix,
                n8n=workflow,
                leak_rate=0.0 if automated else leak_rate,
            )
        )

    return lines


def total_score(lines: list[AutomationLine]) -> int:
    """Sum the signal scores. The weights are designed to total 100."""
    return sum(line.score for line in lines)


def verdict(score: int, industry: str) -> str:
    """A direct, client-friendly interpretation of the score."""
    if score >= 85:
        return "Well automated. The follow-up runs without anyone remembering to do it."
    if score >= 70:
        return "Mostly automated, with a few gaps still being covered by hand."
    if score >= 55:
        return (
            "Half-automated. The busiest weeks are exactly when the manual follow-up "
            "gets dropped."
        )
    if score >= 40:
        return (
            f"Largely manual. This {industry} business is winning work it then loses "
            "to slow or forgotten follow-up."
        )
    return "Fully manual. Every lead depends on someone remembering — and they won't."


def missing_items(lines: list[AutomationLine]) -> list[str]:
    """Return missing-signal messages, biggest point loss first."""
    ordered = sorted(lines, key=lambda line: line.maximum - line.score, reverse=True)
    return [line.missing for line in ordered if line.missing]


def top_automations(
    lines: list[AutomationLine], limit: int = 3
) -> list[AutomationLine]:
    """Choose the highest-value automations to build first."""
    gaps = [line for line in lines if line.maximum > line.score]
    gaps.sort(key=lambda line: (line.maximum - line.score, line.leak_rate), reverse=True)
    return gaps[:limit]


# ---------------------------------------------------------------------------
# Revenue impact
# ---------------------------------------------------------------------------


def revenue_leaks(
    profile: AutomationProfile, lines: list[AutomationLine]
) -> list[tuple[str, float, float]]:
    """Return (label, rate, monthly amount) for each gap that costs won work."""
    leaks: list[tuple[str, float, float]] = []
    for line in lines:
        if line.leak_rate <= 0:
            continue
        amount = profile.monthly_leads * line.leak_rate * profile.avg_job_value
        leaks.append((line.label, line.leak_rate, amount))
    leaks.sort(key=lambda item: item[2], reverse=True)
    return leaks


def leak_basis(profile: AutomationProfile) -> str:
    """Spell out the arithmetic so the figures can be challenged."""
    return (
        f"Based on your figures: {profile.monthly_leads} enquiries a month at an "
        f"average job value of {format_money(profile.avg_job_value, profile.country)}. "
        "Each gap is costed separately and gaps overlap, so treat the total as an "
        "upper bound, not a forecast."
    )


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def render_markdown(
    profile: AutomationProfile,
    lines: list[AutomationLine],
    total: int,
    generated: date,
) -> str:
    """Build the Markdown report."""
    missing = missing_items(lines)
    leaks = revenue_leaks(profile, lines)
    leak_total = sum(amount for _, _, amount in leaks)

    md: list[str] = [
        "# Platinum Automation Gap Audit",
        "",
        f"**Business:** {profile.business}  ",
        f"**Industry:** {profile.industry}  ",
        f"**Country:** {profile.country}  ",
        f"**Audit date:** {generated.strftime('%d %b %Y')}",
        "",
        f"## Automation Score: {total}/100 — Grade {grade(total)}",
        "",
        f"> {verdict(total, profile.industry)}",
        "",
        "### Score breakdown",
        "",
        "| Signal | Score | Max | Detail |",
        "| --- | ---: | ---: | --- |",
    ]

    for line in lines:
        md.append(f"| {line.label} | {line.score} | {line.maximum} | {line.detail} |")

    md.extend(["", "### Estimated monthly revenue impact", ""])
    if leaks:
        md.extend(
            [
                "| Gap | Assumed loss | Monthly impact |",
                "| --- | ---: | ---: |",
            ]
        )
        for label, rate, amount in leaks:
            md.append(
                f"| {label} | {rate:.0%} of enquiries | "
                f"{format_money(amount, profile.country)} |"
            )
        md.append(
            f"| **Total** | | **{format_money(leak_total, profile.country)}** |"
        )
        md.extend(["", f"*{leak_basis(profile)}*"])
    else:
        md.append(
            "- No revenue-losing gaps found. The follow-up systems are already in place."
        )

    md.extend(["", "### What's missing", ""])
    if missing:
        md.extend(f"- {item}" for item in missing)
    else:
        md.append("- No critical gaps detected. Keep the workflows monitored.")

    md.extend(["", "### Top 3 automations to build", ""])
    recommendations = top_automations(lines)
    if recommendations:
        for index, line in enumerate(recommendations, start=1):
            md.extend(
                [
                    f"{index}. **{line.label}** — {line.fix}",
                    f"   - *n8n workflow:* {line.n8n}",
                ]
            )
    else:
        md.append("1. **Monitor what's running** — review workflow logs monthly.")

    md.extend(
        [
            "",
            "---",
            f"*Prepared by {BRAND} · Revenue figures are indicative and based on "
            "client-supplied numbers.*",
            "",
        ]
    )
    return "\n".join(md)


# ---------------------------------------------------------------------------
# PDF report
# ---------------------------------------------------------------------------


def leak_table(
    profile: AutomationProfile,
    leaks: list[tuple[str, float, float]],
    styles: dict,
    width: float,
) -> Table:
    """Build the revenue-impact table."""
    rows = [
        [
            p("GAP", styles["table_head"]),
            p("ASSUMED LOSS", styles["table_head_right"]),
            p("MONTHLY IMPACT", styles["table_head_right"]),
        ]
    ]
    for label, rate, amount in leaks:
        rows.append(
            [
                p(label, styles["table_cell"]),
                p(f"{rate:.0%} of enquiries", styles["table_cell_small"]),
                p(format_money(amount, profile.country), styles["table_cell_right"]),
            ]
        )

    leak_total = sum(amount for _, _, amount in leaks)
    rows.append(
        [
            p("Total at risk", styles["table_cell"]),
            p("", styles["table_cell_small"]),
            p(format_money(leak_total, profile.country), styles["table_cell_right"]),
        ]
    )

    table = Table(
        rows,
        colWidths=[width - 74 * mm, 38 * mm, 36 * mm],
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
            ("LINEBELOW", (0, 1), (-1, -2), 0.3, SILVER),
            ("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), LIGHT),
            ("LINEABOVE", (0, len(rows) - 1), (-1, len(rows) - 1), 0.8, INK),
            ("TOPPADDING", (0, 1), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 2),
        ]
    )
    table.setStyle(style)
    return table


def build_pdf(
    path: Path,
    profile: AutomationProfile,
    lines: list[AutomationLine],
    total: int,
    generated: date,
) -> None:
    """Render the branded PDF."""
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
        subject=f"Automation gap audit for {profile.business}",
    )
    width = A4[0] - 28 * mm
    story: list = []

    # Header
    story.append(
        Table(
            [
                [
                    p(BRAND.upper(), styles["brand"]),
                    p("AUTOMATION GAP AUDIT", styles["brand_right"]),
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
                p("INDUSTRY", styles["label"]),
                p(profile.industry, styles["meta"]),
                p("MARKET", styles["label"]),
                p(profile.country, styles["meta"]),
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
    story.append(Spacer(1, 6))

    # Score banner
    score_stack = Table(
        [
            [p("AUTOMATION SCORE", styles["score_label"])],
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

    automated = sum(1 for line in lines if line.score == line.maximum)
    summary = f"{len(lines)} systems checked · {automated} already automated"
    right_text = (
        f"{html.escape(verdict(total, profile.industry))}<br/><br/>"
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

    # Score breakdown
    story.extend(section("Score breakdown", styles))
    story.append(breakdown_table(lines, styles, width))

    # Revenue impact
    story.extend(section("Estimated monthly revenue impact", styles))
    leaks = revenue_leaks(profile, lines)
    if leaks:
        story.append(leak_table(profile, leaks, styles, width))
        story.append(Spacer(1, 2))
        story.append(p(leak_basis(profile), styles["tiny"]))
    else:
        story.append(
            p(
                "No revenue-losing gaps found. The follow-up systems are already in place.",
                styles["body"],
            )
        )

    # What's missing
    story.extend(section("What's missing", styles))
    missing = missing_items(lines)
    if missing:
        for item in missing:
            story.append(p(f"- {item}", styles["bullet"]))
    else:
        story.append(
            p("No critical gaps detected. Keep the workflows monitored.", styles["body"])
        )

    # Top 3 automations, kept with the call to action so neither is orphaned
    # on a page of its own when the gap list runs long.
    tail: list = list(section("Top 3 automations to build", styles))
    recommendations = top_automations(lines)
    if recommendations:
        for index, line in enumerate(recommendations, start=1):
            tail.append(
                Paragraph(
                    f"<b>{index}. {html.escape(line.label)}</b> — "
                    f"{html.escape(line.fix)}",
                    styles["fix"],
                )
            )
            tail.append(
                Paragraph(
                    f"<font color='#6B7280'><b>n8n:</b> {html.escape(line.n8n)}</font>",
                    styles["body_small"],
                )
            )
            tail.append(Spacer(1, 2))
    else:
        tail.append(
            p("Monitor what's running: review workflow logs monthly.", styles["body"])
        )

    tail.append(Spacer(1, 7))
    tail.append(cta_block("Ready to plug the leaks?", styles, width))
    story.append(KeepTogether(tail))

    def footer(canvas, _doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica-Oblique", 6.2)
        canvas.setFillColor(GREY)
        canvas.drawCentredString(
            A4[0] / 2,
            13.2 * mm,
            "Revenue figures are indicative and based on client-supplied numbers.",
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


# ---------------------------------------------------------------------------
# CLI flow
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Platinum Automation Gap Audit - score a local business's follow-up systems."
        ),
        epilog=(
            "Run without input flags to be prompted for every value. The nine discovery "
            "questions are designed to be asked on a call."
        ),
    )
    parser.add_argument("--business", help="Business name, e.g. 'Aqua Plumbing'")
    parser.add_argument("--industry", help="Industry/niche, e.g. 'plumber'")
    parser.add_argument("--country", type=parse_country, help="UK or Kenya")
    parser.add_argument(
        "--avg-job-value",
        dest="avg_job_value",
        type=float,
        help="Average job value in local currency",
    )
    parser.add_argument(
        "--monthly-leads",
        dest="monthly_leads",
        type=int,
        help="Enquiries received per month",
    )
    for dest, question, points, _rate in QUESTIONS:
        parser.add_argument(
            f"--{dest.replace('_', '-')}",
            dest=dest,
            type=parse_bool,
            help=f"{question}? y/n ({points} pts)",
        )
    parser.add_argument(
        "--outdir", default="reports", help="Report output directory (default: reports)"
    )
    return parser.parse_args(argv)


def collect_automation_answers(args: argparse.Namespace) -> dict:
    """Prompt for, or read from flags, the money inputs and nine answers."""
    avg_job_value = (
        args.avg_job_value
        if args.avg_job_value is not None
        else ask_float("Average job value")
    )
    monthly_leads = (
        args.monthly_leads
        if args.monthly_leads is not None
        else ask_int("Enquiries received per month")
    )

    answers: dict = {"avg_job_value": avg_job_value, "monthly_leads": monthly_leads}
    for dest, question, _points, _rate in QUESTIONS:
        supplied = getattr(args, dest)
        answers[dest] = supplied if supplied is not None else ask_bool(question)
    return answers


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    # Step 1: collect the three core inputs.
    business = args.business or ask("Business name")
    industry = args.industry or ask("Industry / niche")
    country = args.country or ask_country("Country")

    # Step 2: run the discovery questions.
    print("\nNow the discovery questions.\n")
    answers = collect_automation_answers(args)
    profile = AutomationProfile(
        business=business, industry=industry, country=country, **answers
    )

    # Step 3: score.
    lines = score_automation(profile)
    total = total_score(lines)
    generated = date.today()

    # Step 4: write both report formats.
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = f"{slugify(profile.business)}-automation-gap-audit-{generated:%Y%m%d}"
    md_path = outdir / f"{base}.md"
    pdf_path = outdir / f"{base}.pdf"

    md_path.write_text(render_markdown(profile, lines, total, generated), encoding="utf-8")
    build_pdf(pdf_path, profile, lines, total, generated)

    leaks = revenue_leaks(profile, lines)
    leak_total = sum(amount for _, _, amount in leaks)

    print(f"\nScore: {total}/100 (Grade {grade(total)})")
    if leak_total:
        print(f"Estimated monthly revenue at risk: {format_money(leak_total, country)}")
    print(f"Markdown: {md_path.resolve()}")
    print(f"PDF:      {pdf_path.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
