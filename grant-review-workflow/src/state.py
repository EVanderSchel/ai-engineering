"""The state of one application as it moves through the workflow (see "State" in docs/design.md).

Each field has one owner: only `extract` writes `extraction`, only `research` writes `research`, and so on.
That's what lets extract, research and read_proposal run in parallel. The two shared lists, `flags` and
`calls`, are only ever appended to.

Pydantic validates every value a step hands on, and the whole state round-trips through JSON exactly:
step 4's checkpoints save it to DynamoDB after every step.
"""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from bedrock import CallRecord

FocusArea = Literal["food_security", "health", "education", "housing", "human_services"]
RuleId = Literal["form_type", "revenue_range", "return_current", "net_assets", "request_range", "request_share"]
Route = Literal["decline_ineligible", "review"]
FlagKind = Literal[
    "budget_mismatch",  # the narrative's annual budget is far from the return's expenses
    "staff_mismatch",  # the narrative's staff count is far from the return's employees
    "name_mismatch",  # the form's name doesn't match the EIN's registered name
    "out_of_focus",  # the proposal funds something outside the focus areas
    "injection",  # the narrative contains text addressed to the reviewer or an AI
    "not_found",  # the EIN isn't in the public record
    "figures_disagree",  # figures read from the return disagree with the public record's
    "arithmetic",  # the return's own totals don't add up as read
]
Criterion = Literal["clear_outcomes", "budget_matches_request", "feasible_for_size"]


class ApplicationForm(BaseModel):
    """What the applicant typed: checked, not trusted."""

    ein: str = Field(pattern=r"^\d{9}$")
    organization_name: str = Field(min_length=1, max_length=200)
    amount_requested: int = Field(gt=0)  # whether it's within the fund's range is an eligibility rule
    project_title: str | None = Field(default=None, max_length=200)

    @field_validator("ein", mode="before")
    @classmethod
    def strip_dash(cls, value):
        """EINs are often written 12-3456789."""
        return value.replace("-", "").strip() if isinstance(value, str) else value


class ReturnFigures(BaseModel):
    """Part I figures from the latest Form 990, named as in the IRS answer keys (and fund_rules). None means
    the line is blank on the form, not zero."""

    ein: str | None = None
    organization_name: str | None = None
    tax_period_end: dt.date | None = None
    contributions_and_grants_current_year: int | None = None
    program_service_revenue_current_year: int | None = None
    investment_income_current_year: int | None = None
    other_revenue_current_year: int | None = None
    total_revenue_current_year: int | None = None
    total_expenses_current_year: int | None = None
    total_assets_end_of_year: int | None = None
    total_liabilities_end_of_year: int | None = None
    employees: int | None = Field(default=None, ge=0)


class Extraction(BaseModel):
    """Written by `extract`: the figures read from the return's page images."""

    form_type: str
    figures: ReturnFigures
    revenue_adds_up: bool | None  # the four revenue lines sum to total revenue (None: a line couldn't be read)


class PeriodFigures(BaseModel):
    """One period's figures in the public record (None: not reported)."""

    revenue: int | None = None
    expenses: int | None = None


class Research(BaseModel):
    """Written by `research`: what the public record (ProPublica, from IRS data) says about the EIN."""

    found: bool
    registered_name: str | None = None
    name_matches_form: bool | None = None
    name_explanation: str | None = None  # why the names were judged the same or different
    form_name_belongs_to: str | None = None  # EIN of another organization registered under the form's name
    # Revenue and expenses for every period the record has figures for, keyed by period end ("2022-06").
    # Compared only with the return for the same period: the record's latest year is often a newer return.
    figures_by_period: dict[str, PeriodFigures] = {}


class Claims(BaseModel):
    """Figures the narrative states about the organization (None: it doesn't say)."""

    annual_budget: int | None = Field(default=None, ge=0)
    staff: int | None = Field(default=None, ge=0)


class RubricScore(BaseModel):
    criterion: Criterion
    score: int = Field(ge=1, le=5)
    quote: str = Field(min_length=1)  # the narrative's own words that justify the score


class ProposalReading(BaseModel):
    """Written by `read_proposal`. The narrative is untrusted: this records what it says, never what it asks."""

    focus_area: FocusArea | None  # None: the proposal doesn't fund direct services in a focus area
    claims: Claims
    rubric: list[RubricScore]
    suspected_instructions: list[str] = []  # quotes of text that addresses the reviewer or an AI


class Flag(BaseModel):
    """Something a reviewer should look at, with the evidence to check it without redoing the work."""

    step: str
    kind: FlagKind
    evidence: str = Field(min_length=1)


class Eligibility(BaseModel):
    """Written by `eligibility`: the fund's rules applied by code (fund_rules)."""

    failed_rules: list[RuleId]

    @property
    def passed(self) -> bool:
        return not self.failed_rules


class ReviewerDecision(BaseModel):
    """Written by `human_review`: the person's decision. Corrections replace figures the extraction got wrong
    (field name in ReturnFigures -> corrected value) and send the application back through eligibility."""

    decision: Literal["approve", "decline"] | None  # None: only corrections, no decision yet
    corrections: dict[str, int] = {}
    reason: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    decided_at: dt.datetime

    @field_validator("corrections")
    @classmethod
    def only_known_figures(cls, corrections: dict[str, int]) -> dict[str, int]:
        unknown = sorted(set(corrections) - set(ReturnFigures.model_fields))
        if unknown:
            raise ValueError(f"not figures on the return: {unknown}")
        return corrections


class ApplicationState(BaseModel):
    application_id: str
    received_at: dt.datetime
    form: ApplicationForm
    narrative: str  # untrusted
    return_pdf: str  # local path now; an S3 key once deployed (step 7)

    extraction: Extraction | None = None
    research: Research | None = None
    proposal: ProposalReading | None = None
    flags: list[Flag] = []
    eligibility: Eligibility | None = None
    route: Route | None = None
    review: ReviewerDecision | None = None
    letter: str | None = None
    calls: list[CallRecord] = []  # every model call: model, tokens, cost, latency

    @property
    def cost_usd(self) -> float:
        return sum(call.cost_usd for call in self.calls)
