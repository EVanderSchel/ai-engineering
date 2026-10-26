import { Link, useSearchParams } from 'react-router';
import { STATUSES, type Status, useApi } from '../api/client';
import { FLAG_LABELS, STATUS_LABELS, dateTime, money } from '../format';
import { LoadError } from '../LoadError';
import { useLoad } from '../useLoad';

export function QueuePage() {
  const api = useApi();
  const [params, setParams] = useSearchParams();
  const requested = params.get('status');
  const status: Status = STATUSES.find((s) => s === requested) ?? 'waiting_for_review';
  const queue = useLoad(() => api.queue(status), [api, status]);

  return (
    <main>
      <h1>Applications</h1>
      <div role="tablist" aria-label="Status" className="tabs">
        {STATUSES.map((s) => (
          <button
            key={s}
            role="tab"
            aria-selected={s === status}
            onClick={() => setParams(s === 'waiting_for_review' ? {} : { status: s })}
          >
            {STATUS_LABELS[s]}
          </button>
        ))}
      </div>

      {queue.state === 'loading' && <p aria-busy="true">Loading...</p>}
      {queue.state === 'error' && <LoadError what="the applications" error={queue.error} />}
      {queue.state === 'done' && queue.value.applications.length === 0 && <p>No applications.</p>}
      {queue.state === 'done' && queue.value.applications.length > 0 && (
        <table className="queue">
          <caption className="visually-hidden">{STATUS_LABELS[status]}, oldest first</caption>
          <thead>
            <tr>
              <th scope="col">Received</th>
              <th scope="col">Organization</th>
              <th scope="col" className="number">
                Request
              </th>
              <th scope="col">{status === 'declined_by_rules' ? 'Rules failed' : 'Flags'}</th>
              {status === 'decided' && <th scope="col">Decision</th>}
            </tr>
          </thead>
          <tbody>
            {queue.value.applications.map((a) => (
              <tr key={a.application_id}>
                <td>{dateTime(a.received_at)}</td>
                <td>
                  <Link to={`/applications/${encodeURIComponent(a.application_id)}`}>{a.organization_name}</Link>
                  <div className="muted">{a.application_id}</div>
                </td>
                <td className="number">{money(a.amount_requested)}</td>
                <td>
                  {status === 'declined_by_rules'
                    ? a.failed_rules.join(', ')
                    : a.flags.map((kind, i) => (
                        <span key={`${kind}-${i}`} className={`badge${kind === 'injection' ? ' danger' : ''}`}>
                          {FLAG_LABELS[kind]}
                        </span>
                      ))}
                </td>
                {status === 'decided' && <td className={`decision ${a.decision}`}>{a.decision}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}
