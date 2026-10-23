// The reviewer's decision. Corrections are only sent on their own ("Correct figures only"): the workflow then reruns
// the fund's rules on the corrected figures and the application comes back to the queue. Sent with a decision, they'd
// go straight to the letter without the rules seeing them.
import { type FormEvent, useState } from 'react';
import { RequestFailed, useApi } from '../api/client';
import type { DecisionRequest, Evidence } from '../api/types';
import { type CorrectableFigure, FIGURE_LABELS, number } from '../format';

type Choice = 'approve' | 'decline' | 'corrections';
const FIGURES = Object.keys(FIGURE_LABELS) as CorrectableFigure[];

export function DecisionForm({ evidence, onDecided }: { evidence: Evidence; onDecided: (next: Evidence) => void }) {
  const api = useApi();
  const [choice, setChoice] = useState<Choice | null>(null);
  const [reason, setReason] = useState('');
  const [corrections, setCorrections] = useState<Partial<Record<CorrectableFigure, string>>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const corrected = Object.fromEntries(
    Object.entries(corrections).filter(([, value]) => value?.trim() !== ''),
  ) as Partial<Record<CorrectableFigure, string>>;
  const invalid = Object.entries(corrected).filter(([, value]) => !/^-?\d+$/.test(value!.trim()));
  const problem =
    choice === null
      ? 'Choose a decision.'
      : reason.trim() === ''
        ? 'Give a reason.'
        : choice === 'corrections' && Object.keys(corrected).length === 0
          ? 'Enter at least one corrected figure.'
          : invalid.length > 0
            ? `Whole numbers only: ${invalid.map(([field]) => FIGURE_LABELS[field as CorrectableFigure]).join(', ')}.`
            : null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (problem || choice === null) return;
    const request: DecisionRequest =
      choice === 'corrections'
        ? {
            decision: null,
            corrections: Object.fromEntries(Object.entries(corrected).map(([k, v]) => [k, Number(v!.trim())])),
            reason: reason.trim(),
          }
        : { decision: choice, corrections: {}, reason: reason.trim() };
    setSubmitting(true);
    setError(null);
    try {
      onDecided(await api.decide(evidence.record.application_id, request));
    } catch (e) {
      setError(
        e instanceof RequestFailed && e.status === 409
          ? 'Someone else has decided this application, or is deciding it now. Reload to see where it stands.'
          : e instanceof Error
            ? e.message
            : String(e),
      );
    } finally {
      setSubmitting(false);
    }
  }

  const figures = evidence.extraction?.figures;
  return (
    <section aria-labelledby="decide">
      <h2 id="decide">Your decision</h2>
      <form onSubmit={submit} className="decision-form" noValidate>
        <fieldset disabled={submitting}>
          <legend className="visually-hidden">Decision</legend>
          {(
            [
              ['approve', 'Approve'],
              ['decline', 'Decline'],
              ['corrections', 'Correct figures only (the rules run again, then it comes back here)'],
            ] as const
          ).map(([value, label]) => (
            <label key={value} className="choice">
              <input
                type="radio"
                name="choice"
                value={value}
                checked={choice === value}
                onChange={() => setChoice(value)}
              />{' '}
              {label}
            </label>
          ))}

          {choice === 'corrections' && figures && (
            <table className="figures corrections">
              <thead>
                <tr>
                  <th scope="col">Figure</th>
                  <th scope="col" className="number">
                    Read from the return
                  </th>
                  <th scope="col">Corrected value</th>
                </tr>
              </thead>
              <tbody>
                {FIGURES.map((field) => (
                  <tr key={field}>
                    <th scope="row">
                      <label htmlFor={`correct-${field}`}>{FIGURE_LABELS[field]}</label>
                    </th>
                    <td className="number">{number(figures[field])}</td>
                    <td>
                      <input
                        id={`correct-${field}`}
                        inputMode="numeric"
                        value={corrections[field] ?? ''}
                        onChange={(e) => setCorrections({ ...corrections, [field]: e.target.value })}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <label htmlFor="reason" className="reason-label">
            Reason
          </label>
          <p className="muted" id="reason-help">
            {choice === 'corrections'
              ? 'Recorded with the corrections.'
              : 'Quoted word for word in the letter to the applicant.'}
          </p>
          <textarea
            id="reason"
            aria-describedby="reason-help"
            rows={4}
            maxLength={2000}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />

          {error && <p role="alert">{error}</p>}
          <p className="muted">{submitting ? 'Applying the decision...' : (problem ?? '')}</p>
          <button type="submit" disabled={problem !== null || submitting}>
            {choice === 'corrections' ? 'Apply corrections' : choice ? `${choice === 'approve' ? 'Approve' : 'Decline'} application` : 'Submit'}
          </button>
        </fieldset>
      </form>
    </section>
  );
}
