import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../App';
import { type Api, RequestFailed, fixtureApi } from '../api/client';
import type { DecisionRequest, Evidence } from '../api/types';

// The fixtures for reading, and a recording stand-in for decisions: what the form sends, and how it shows each answer.
function withDecide(decide: (id: string, request: DecisionRequest) => Promise<Evidence>) {
  const sent: [string, DecisionRequest][] = [];
  const api: Api = {
    ...fixtureApi(),
    decide: (id, request) => {
      sent.push([id, request]);
      return decide(id, request);
    },
  };
  return { api, sent };
}

async function open(api: Api, id = 'gp-027') {
  window.location.hash = `#/applications/${id}`;
  render(<App api={api} />);
  return screen.findByRole('heading', { name: 'Your decision' });
}

afterEach(() => {
  window.location.hash = '';
});

async function decidedVersion(id: string, request: DecisionRequest): Promise<Evidence> {
  const before = await fixtureApi().application(id);
  return {
    ...before,
    waiting_for_review: false,
    record: { ...before.record, status: 'decided', decision: request.decision },
    review: { ...request, reviewer: 'local.reviewer@example.org', decided_at: '2026-10-23T16:00:00Z' },
    letter: `Letter for ${id}`,
  };
}

test('approving sends the decision and reason, then shows the outcome in place of the form', async () => {
  const { api, sent } = withDecide(decidedVersion);
  await open(api);
  const submit = screen.getByRole('button', { name: 'Submit' });
  expect(submit).toBeDisabled();
  expect(screen.getByText('Choose a decision.')).toBeInTheDocument();

  await userEvent.click(screen.getByRole('radio', { name: 'Approve' }));
  expect(screen.getByText('Give a reason.')).toBeInTheDocument();
  await userEvent.type(screen.getByRole('textbox', { name: 'Reason' }), '  Clear outcomes.  ');
  await userEvent.click(screen.getByRole('button', { name: 'Approve application' }));

  expect(sent).toEqual([['gp-027', { decision: 'approve', corrections: {}, reason: 'Clear outcomes.' }]]);
  expect(await screen.findByRole('heading', { name: 'Outcome' })).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Your decision' })).not.toBeInTheDocument();
  expect(screen.getByText('Letter for gp-027')).toBeInTheDocument();
});

test('the reason is quoted in the letter, and the form says so', async () => {
  await open(withDecide(decidedVersion).api);
  await userEvent.click(screen.getByRole('radio', { name: 'Decline' }));
  expect(screen.getByRole('textbox', { name: 'Reason' })).toHaveAccessibleDescription(
    'Quoted word for word in the letter to the applicant.',
  );
});

test('corrections go on their own, as whole numbers, with no decision', async () => {
  const { api, sent } = withDecide(async (id) => fixtureApi().application(id));
  await open(api);
  await userEvent.click(screen.getByRole('radio', { name: /Correct figures only/ }));
  const button = screen.getByRole('button', { name: 'Apply corrections' });
  await userEvent.type(screen.getByRole('textbox', { name: 'Reason' }), 'Employees misread.');
  expect(screen.getByText('Enter at least one corrected figure.')).toBeInTheDocument();

  const employees = screen.getByRole('textbox', { name: 'Employees' });
  await userEvent.type(employees, '12.5');
  expect(screen.getByText('Whole numbers only: Employees.')).toBeInTheDocument();
  expect(button).toBeDisabled();
  await userEvent.clear(employees);
  await userEvent.type(employees, '12');
  await userEvent.click(button);
  expect(sent).toEqual([['gp-027', { decision: null, corrections: { employees: 12 }, reason: 'Employees misread.' }]]);
});

test('figures typed before switching to a decision are not sent with it', async () => {
  const { api, sent } = withDecide(decidedVersion);
  await open(api);
  await userEvent.click(screen.getByRole('radio', { name: /Correct figures only/ }));
  await userEvent.type(screen.getByRole('textbox', { name: 'Employees' }), '12');
  await userEvent.click(screen.getByRole('radio', { name: 'Approve' }));
  await userEvent.type(screen.getByRole('textbox', { name: 'Reason' }), 'Fine.');
  await userEvent.click(screen.getByRole('button', { name: 'Approve application' }));
  expect(sent[0][1].corrections).toEqual({});
});

test('a decision someone else got to first is explained, and the form stays', async () => {
  const { api } = withDecide(() => Promise.reject(new RequestFailed(409, "isn't waiting")));
  await open(api);
  await userEvent.click(screen.getByRole('radio', { name: 'Approve' }));
  await userEvent.type(screen.getByRole('textbox', { name: 'Reason' }), 'Fine.');
  await userEvent.click(screen.getByRole('button', { name: 'Approve application' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Someone else has decided this application');
  expect(screen.getByRole('button', { name: 'Approve application' })).toBeEnabled();
});

test('while a decision is being applied the form is locked', async () => {
  let finish: (e: Evidence) => void = () => {};
  const { api, sent } = withDecide(() => new Promise((resolve) => (finish = resolve)));
  await open(api);
  await userEvent.click(screen.getByRole('radio', { name: 'Approve' }));
  await userEvent.type(screen.getByRole('textbox', { name: 'Reason' }), 'Fine.');
  const button = screen.getByRole('button', { name: 'Approve application' });
  await userEvent.click(button);
  expect(screen.getByText('Applying the decision...')).toBeInTheDocument();
  expect(button).toBeDisabled(); // a second click can't send a second request
  await userEvent.click(button);
  expect(sent).toHaveLength(1);
  finish(await decidedVersion('gp-027', sent[0][1]));
  expect(await screen.findByRole('heading', { name: 'Outcome' })).toBeInTheDocument();
});

test('the fixtures are read-only and say how to decide locally', async () => {
  await open(fixtureApi());
  await userEvent.click(screen.getByRole('radio', { name: 'Approve' }));
  await userEvent.type(screen.getByRole('textbox', { name: 'Reason' }), 'Fine.');
  await userEvent.click(screen.getByRole('button', { name: 'Approve application' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('npm run dev:api');
});

test('only an application waiting for review has the form', async () => {
  window.location.hash = '#/applications/gp-003'; // decided
  render(<App api={fixtureApi()} />);
  const outcome = await screen.findByRole('heading', { name: 'Outcome' });
  expect(within(outcome.parentElement!).getByText('approve')).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'Your decision' })).not.toBeInTheDocument();
});

test('after a correction the page shows the figure the rules used, beside the one read from the return', async () => {
  const { api } = withDecide(async (id, request) => {
    const before = await fixtureApi().application(id);
    const review = { ...request, reviewer: 'local.reviewer@example.org', decided_at: '2026-10-23T16:00:00Z' };
    return { ...before, review };
  });
  await open(api, 'gp-026'); // the return reads 464 employees
  await userEvent.click(screen.getByRole('radio', { name: /Correct figures only/ }));
  await userEvent.type(screen.getByRole('textbox', { name: 'Employees' }), '186');
  await userEvent.type(screen.getByRole('textbox', { name: 'Reason' }), 'Seasonal staff counted.');
  await userEvent.click(screen.getByRole('button', { name: 'Apply corrections' }));

  const row = (await screen.findByRole('rowheader', { name: 'Employees' })).closest('tr')!;
  expect(within(row).getByText('186', { selector: 'strong' })).toBeInTheDocument();
  expect(within(row).getByText('corrected; the return reads 464')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Corrections so far' })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Your decision' })).toBeInTheDocument(); // still waiting: decide next
});
