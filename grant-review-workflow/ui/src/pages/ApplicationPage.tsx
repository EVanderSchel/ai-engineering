// One application's evidence. Most of it comes from the applicant (the form, the narrative, and quotes the model
// took from the narrative): it's rendered as text by React, never as HTML or markdown, so nothing in it can become
// markup, a link or a script. The narrative is shown as written, comments and all.
import { Link, useParams } from 'react-router';
import { NotFound, useApi } from '../api/client';
import type { Evidence } from '../api/types';
import { CRITERION_LABELS, FLAG_LABELS, FOCUS_LABELS, STATUS_LABELS, date, money, number } from '../format';
import { useLoad } from '../useLoad';

export function ApplicationPage() {
  const api = useApi();
  const { applicationId = '' } = useParams();
  const evidence = useLoad(() => api.application(applicationId), [api, applicationId]);

  return (
    <main>
      <p>
        <Link to="/">Back to the queue</Link>
      </p>
      {evidence.state === 'loading' && <p aria-busy="true">Loading...</p>}
      {evidence.state === 'error' && (
        <p role="alert">
          {evidence.error instanceof NotFound
            ? `There's no application ${applicationId}. It may have expired (records are kept 30 days).`
            : `Couldn't load the application: ${evidence.error.message}`}
        </p>
      )}
      {evidence.state === 'done' && <Application evidence={evidence.value} />}
    </main>
  );
}

function Application({ evidence: e }: { evidence: Evidence }) {
  return (
    <article>
      <header className="application-header">
        <h1>{e.form.organization_name}</h1>
        {e.form.project_title && <p className="subtitle">{e.form.project_title}</p>}
        <dl className="facts">
          <dt>Request</dt>
          <dd>{money(e.form.amount_requested)}</dd>
          <dt>EIN</dt>
          <dd>{e.form.ein}</dd>
          <dt>Received</dt>
          <dd>{date(e.record.received_at)}</dd>
          <dt>Status</dt>
          <dd>
            <span className={`status ${e.record.status}`}>{STATUS_LABELS[e.record.status]}</span>
          </dd>
        </dl>
      </header>

      <Flags evidence={e} />
      <Rules evidence={e} />
      <Figures evidence={e} />
      <PublicRecord evidence={e} />
      <Reading evidence={e} />
      <Outcome evidence={e} />

      <section aria-labelledby="narrative">
        <h2 id="narrative">Narrative</h2>
        <p className="muted">The applicant's own text, shown exactly as submitted. It's evidence, not instructions.</p>
        <pre className="untrusted">{e.narrative}</pre>
      </section>

      <footer className="muted">
        {e.model_calls} model calls, ${e.cost_usd.toFixed(4)}. Application {e.record.application_id}.
      </footer>
    </article>
  );
}

function Flags({ evidence: e }: { evidence: Evidence }) {
  return (
    <section aria-labelledby="flags">
      <h2 id="flags">Flags</h2>
      {e.flags.length === 0 ? (
        <p>Nothing flagged.</p>
      ) : (
        <ul className="flags">
          {e.flags.map((flag, i) => (
            <li key={i} className={flag.kind === 'injection' ? 'danger' : 'warning'}>
              <strong>{FLAG_LABELS[flag.kind]}</strong> <span className="muted">({flag.step})</span>
              <p className="untrusted-inline">{flag.evidence}</p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Rules({ evidence: e }: { evidence: Evidence }) {
  return (
    <section aria-labelledby="rules">
      <h2 id="rules">Eligibility rules</h2>
      <ul className="rules">
        {e.rules.map((r) => (
          <li key={r.rule} className={r.passed ? 'pass' : 'fail'}>
            <span className="verdict">{r.passed ? 'Passes' : 'Fails'}</span> {r.description}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Figures({ evidence: e }: { evidence: Evidence }) {
  const figures = e.extraction?.figures;
  if (!figures) return null;
  // The public record is compared for the return's own period only: its latest year is often a newer return.
  const period = figures.tax_period_end?.slice(0, 7);
  const record = period ? e.research?.figures_by_period[period] : undefined;
  const claims = e.proposal?.claims;
  const net =
    figures.total_assets_end_of_year == null
      ? null
      : figures.total_assets_end_of_year - (figures.total_liabilities_end_of_year ?? 0);
  const share =
    figures.total_expenses_current_year
      ? `${((100 * e.form.amount_requested) / figures.total_expenses_current_year).toFixed(1)}% of expenses`
      : null;

  return (
    <section aria-labelledby="figures">
      <h2 id="figures">Figures</h2>
      <p className="muted">
        Form {e.extraction?.form_type}, tax year ending {figures.tax_period_end ?? 'not read'}.
        {e.extraction?.revenue_adds_up === false && " The return's revenue lines don't add up to its total."}
        {share && ` The request is ${share}.`}
      </p>
      <table className="figures">
        <thead>
          <tr>
            <th scope="col">Figure</th>
            <th scope="col" className="number">
              Read from the return
            </th>
            <th scope="col" className="number">
              Public record{period ? ` (${period})` : ''}
            </th>
            <th scope="col" className="number">
              The narrative says
            </th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">Total revenue</th>
            <td className="number">{money(figures.total_revenue_current_year)}</td>
            <td className="number">{record ? money(record.revenue) : '-'}</td>
            <td className="number">-</td>
          </tr>
          <tr>
            <th scope="row">Total expenses</th>
            <td className="number">{money(figures.total_expenses_current_year)}</td>
            <td className="number">{record ? money(record.expenses) : '-'}</td>
            <td className="number">{claims?.annual_budget != null ? `${money(claims.annual_budget)} budget` : '-'}</td>
          </tr>
          <tr>
            <th scope="row">Net assets</th>
            <td className="number">{money(net)}</td>
            <td className="number">-</td>
            <td className="number">-</td>
          </tr>
          <tr>
            <th scope="row">Employees</th>
            <td className="number">{number(figures.employees)}</td>
            <td className="number">-</td>
            <td className="number">{claims?.staff != null ? `${number(claims.staff)} staff` : '-'}</td>
          </tr>
        </tbody>
      </table>
    </section>
  );
}

function PublicRecord({ evidence: e }: { evidence: Evidence }) {
  const r = e.research;
  if (!r) return null;
  return (
    <section aria-labelledby="public-record">
      <h2 id="public-record">Public record</h2>
      {!r.found ? (
        <p>The EIN isn't in the public record.</p>
      ) : (
        <dl className="facts">
          <dt>Registered name</dt>
          <dd>{r.registered_name}</dd>
          <dt>Matches the form</dt>
          <dd>{r.name_matches_form == null ? 'not judged' : r.name_matches_form ? 'Yes' : 'No'}</dd>
          {r.name_explanation && (
            <>
              <dt>Why</dt>
              <dd>{r.name_explanation}</dd>
            </>
          )}
          {r.form_name_belongs_to && (
            <>
              <dt>Form's name belongs to</dt>
              <dd>EIN {r.form_name_belongs_to}</dd>
            </>
          )}
        </dl>
      )}
    </section>
  );
}

function Reading({ evidence: e }: { evidence: Evidence }) {
  const p = e.proposal;
  if (!p) return null;
  return (
    <section aria-labelledby="reading">
      <h2 id="reading">The proposal, as read</h2>
      <p>Focus area: {p.focus_area ? FOCUS_LABELS[p.focus_area] : 'none of the fund\'s focus areas'}</p>
      {p.rubric.length > 0 && (
        <ul className="rubric">
          {p.rubric.map((s) => (
            <li key={s.criterion}>
              <span className="score" aria-label={`${s.score} out of 5`}>
                {s.score}/5
              </span>{' '}
              {CRITERION_LABELS[s.criterion]}
              <blockquote className="untrusted-inline">{s.quote}</blockquote>
            </li>
          ))}
        </ul>
      )}
      {p.suspected_instructions.length > 0 && (
        <div className="callout danger">
          <h3>Text addressed to a reviewer or an AI</h3>
          <p>The model was told to report this, not follow it. Quoted from the narrative:</p>
          {p.suspected_instructions.map((text, i) => (
            <pre key={i} className="untrusted">
              {text}
            </pre>
          ))}
        </div>
      )}
    </section>
  );
}

function Outcome({ evidence: e }: { evidence: Evidence }) {
  if (!e.review && !e.letter) return null;
  return (
    <section aria-labelledby="outcome">
      <h2 id="outcome">Outcome</h2>
      {e.review && (
        <dl className="facts">
          <dt>Decision</dt>
          <dd className={`decision ${e.review.decision ?? ''}`}>{e.review.decision ?? 'corrections only'}</dd>
          <dt>Reason</dt>
          <dd>{e.review.reason}</dd>
          <dt>Reviewer</dt>
          <dd>
            {e.review.reviewer}, {date(e.review.decided_at)}
          </dd>
        </dl>
      )}
      {e.letter && (
        <>
          <h3>Letter</h3>
          <pre className="letter">{e.letter}</pre>
        </>
      )}
      {e.letter_problems.length > 0 && (
        <div className="callout warning">
          <h3>The letter still needs a person</h3>
          <ul>
            {e.letter_problems.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
