// Labels and number formats in one place. The label maps are typed by the API's own unions: when the Python API
// adds a flag kind or a status, the regenerated types make this file fail to compile until it has a label.
import type { Status } from './api/client';
import type { Flag, ProposalReading, ReturnFigures, RubricScore } from './api/types';

export const STATUS_LABELS: Record<Status, string> = {
  waiting_for_review: 'Waiting for review',
  deciding: 'Being decided',
  decided: 'Decided',
  declined_by_rules: 'Declined by the rules',
};

export const FLAG_LABELS: Record<Flag['kind'], string> = {
  budget_mismatch: 'Budget differs from the return',
  staff_mismatch: 'Staff count differs from the return',
  name_mismatch: 'Name differs from the public record',
  out_of_focus: 'Outside the focus areas',
  injection: 'Text addressed to a reviewer or an AI',
  not_found: 'EIN not in the public record',
  figures_disagree: 'Return and public record disagree',
  arithmetic: "Return's totals don't add up",
  unverified_reading: "Reading not found in the narrative",
  sensitive_data: 'Contains a sensitive identifier',
};

export const FOCUS_LABELS: Record<NonNullable<ProposalReading['focus_area']>, string> = {
  food_security: 'Food security',
  health: 'Health',
  education: 'Education',
  housing: 'Housing',
  human_services: 'Human services',
};

export const CRITERION_LABELS: Record<RubricScore['criterion'], string> = {
  clear_outcomes: 'Clear outcomes',
  budget_matches_request: 'Budget matches the request',
  feasible_for_size: 'Feasible for its size',
};

// The figures a reviewer can correct: the return's numbers (not its EIN, name or period).
export type CorrectableFigure = {
  [K in keyof ReturnFigures]-?: ReturnFigures[K] extends number | null ? K : never;
}[keyof ReturnFigures];

export const FIGURE_LABELS: Record<CorrectableFigure, string> = {
  contributions_and_grants_current_year: 'Contributions and grants',
  program_service_revenue_current_year: 'Program service revenue',
  investment_income_current_year: 'Investment income',
  other_revenue_current_year: 'Other revenue',
  total_revenue_current_year: 'Total revenue',
  total_expenses_current_year: 'Total expenses',
  total_assets_end_of_year: 'Total assets, end of year',
  total_liabilities_end_of_year: 'Total liabilities, end of year',
  employees: 'Employees',
};

const dollars = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
const count = new Intl.NumberFormat('en-US');

export const money = (value: number | null | undefined) => (value == null ? 'not reported' : dollars.format(value));
export const number = (value: number | null | undefined) => (value == null ? 'not reported' : count.format(value));
export const date = (iso: string) =>
  new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' });
export const dateTime = (iso: string) =>
  new Date(iso).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }) + ' UTC';
