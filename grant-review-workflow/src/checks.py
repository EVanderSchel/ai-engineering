"""The workflow's code steps: cross_check, eligibility, route. No model: comparisons, thresholds and rules are
code (design principle 2), so they're exact, free, and the same every run.

What research taught about the public record (step 2d) shapes cross_check: ProPublica's latest figures are
often for a newer return than the one submitted, so the return is compared only with the record's figures for
the same period (matched by period end), and for the same period they must agree exactly, because both come
from the same e-filed return. No figures for that period means nothing to compare, never a disagreement.
"""

import fund_rules
import sensitive
from state import ApplicationState, Eligibility, Flag, ReturnFigures, Route

# Flags that cast doubt on the figures the rules read. With one of these, a failing application goes to a person
# instead of being declined automatically: a misread digit near a threshold is how a wrong decline would happen.
DOUBTS_ABOUT_FIGURES = {"arithmetic", "figures_disagree"}


def _money(value: int | None) -> str:
    return "blank" if value is None else f"${value:,}"


def cross_check(state: ApplicationState) -> list[Flag]:
    """Flags from what extract, research and read_proposal found, each with the evidence to check it."""
    flags = []
    figures = state.extraction.figures if state.extraction else None
    research = state.research
    proposal = state.proposal

    if research is not None and not research.found:
        flags.append(Flag(step="cross_check", kind="not_found", evidence=f"No public record for EIN {state.form.ein}."))
    if research is not None and research.found and research.name_matches_form is False:
        evidence = f"Form says {state.form.organization_name!r}; EIN {state.form.ein} is {research.registered_name!r}."
        if research.form_name_belongs_to:
            evidence += f" The form's name belongs to EIN {research.form_name_belongs_to}."
        if research.name_explanation:
            evidence += f" {research.name_explanation}"
        flags.append(Flag(step="cross_check", kind="name_mismatch", evidence=evidence))

    if proposal is not None:
        if proposal.focus_area is None:
            flags.append(
                Flag(
                    step="cross_check",
                    kind="out_of_focus",
                    evidence="The proposal doesn't fund direct services in a focus area.",
                )
            )
        for passage in proposal.suspected_instructions:
            flags.append(
                Flag(step="cross_check", kind="injection", evidence=f"Text addressed to the reviewer: {passage!r}")
            )
        if figures is not None:
            flags += _claim_flags(proposal.claims.annual_budget, proposal.claims.staff, figures)

    if state.extraction is not None and state.extraction.revenue_adds_up is False:
        flags.append(
            Flag(
                step="cross_check",
                kind="arithmetic",
                evidence=(
                    f"Revenue lines 8-11 don't add up to line 12 ({_money(figures.total_revenue_current_year)}) "
                    "as read."
                ),
            )
        )
    if figures is not None and research is not None and figures.tax_period_end is not None:
        period = figures.tax_period_end.strftime("%Y-%m")
        record = research.figures_by_period.get(period)
        if record is not None:
            for name, read, recorded in [
                ("revenue", figures.total_revenue_current_year, record.revenue),
                ("expenses", figures.total_expenses_current_year, record.expenses),
            ]:
                if read is not None and recorded is not None and read != recorded:
                    evidence = (
                        f"Total {name} for the period ending {period}: read {_money(read)}, "
                        f"public record {_money(recorded)}."
                    )
                    flags.append(Flag(step="cross_check", kind="figures_disagree", evidence=evidence))
    flags += sensitive.flags(state)
    return flags


def _claim_flags(budget: int | None, staff: int | None, figures: ReturnFigures) -> list[Flag]:
    """The narrative's stated budget and staff against the return. Only what both state is compared."""
    flags = []
    expenses = figures.total_expenses_current_year
    if budget is not None and expenses is not None and _disagrees(budget, expenses):
        evidence = (
            f"The proposal states an annual budget of {_money(budget)}; "
            f"the return reports total expenses of {_money(expenses)}."
        )
        flags.append(Flag(step="cross_check", kind="budget_mismatch", evidence=evidence))
    employees = figures.employees
    if staff is not None and employees is not None and _disagrees(staff, employees):
        evidence = f"The proposal states {staff} paid staff; the return reports {employees} employees."
        flags.append(Flag(step="cross_check", kind="staff_mismatch", evidence=evidence))
    return flags


def _disagrees(claimed: int, actual: int) -> bool:
    if actual == 0:
        return claimed != 0  # a claim against a reported zero can't be a percentage off; any claim disagrees
    return fund_rules.claim_disagrees(claimed, actual)


def figures_for_rules(state: ApplicationState) -> ReturnFigures:
    """The extracted figures, with a reviewer's corrections laid over them."""
    figures = state.extraction.figures
    if state.review is not None and state.review.corrections:
        figures = figures.model_copy(update=state.review.corrections)
    return figures


def eligibility(state: ApplicationState) -> Eligibility:
    if state.extraction is None:
        raise ValueError("eligibility needs the extracted figures")
    figures = figures_for_rules(state).model_dump(mode="json")
    failed = fund_rules.failed_rules(figures, state.extraction.form_type, state.form.amount_requested)
    return Eligibility(failed_rules=failed)


def route(state: ApplicationState) -> Route:
    """Decline automatically only when a rule fails and the figures it read are trusted; everything else, every
    flag and every model judgment included, goes to a person (design principle 1)."""
    if state.eligibility is None:
        raise ValueError("route needs the eligibility result")
    if state.eligibility.passed:
        return "review"
    reviewer_checked = state.review is not None
    doubted = any(flag.kind in DOUBTS_ABOUT_FIGURES for flag in state.flags)
    return "decline_ineligible" if reviewer_checked or not doubted else "review"
