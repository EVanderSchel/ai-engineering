import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from './App';
import { httpApi } from './api/client';
import { type Auth, type Session, cognitoLogoutUrl, isRedirectBack, issuerFor } from './auth';

// --- Cognito's specifics -------------------------------------------------------------------------------------

test('the issuer is the pool in its own region', () => {
  expect(issuerFor('us-east-2_FjnKKFBir')).toBe('https://cognito-idp.us-east-2.amazonaws.com/us-east-2_FjnKKFBir');
});

test("Cognito's logout gets client_id and logout_uri", () => {
  const url = new URL(
    cognitoLogoutUrl('https://example.auth.us-east-2.amazoncognito.com/logout', 'abc', 'http://localhost:5174/'),
  );
  expect(Object.fromEntries(url.searchParams)).toEqual({ client_id: 'abc', logout_uri: 'http://localhost:5174/' });
});

test.each([
  ['?code=xyz&state=s1', true],
  ['?error=access_denied&state=s1', true],
  ['?code=xyz', false], // no state: not ours to finish
  ['', false],
  ['?status=decided', false],
])('%s is a redirect back from sign-in: %s', (search, expected) => {
  expect(isRedirectBack(search)).toBe(expected);
});

// --- The app's gate, with a stand-in for Cognito -------------------------------------------------------------

function fakeAuth(session: Session | null, token = 'access-token-1') {
  const calls: string[] = [];
  const auth: Auth = {
    start: async () => session,
    signIn: async () => void calls.push('signIn'),
    signOut: async () => void calls.push('signOut'),
    accessToken: async () => (session ? token : null),
  };
  return { auth, calls };
}

function fakeFetch(status = 200, body: unknown = { applications: [] }) {
  const sent: Request[] = [];
  const impl = (async (url: URL, init: RequestInit) => {
    sent.push(new Request(url, init));
    return new Response(JSON.stringify(body), { status });
  }) as typeof fetch;
  return { impl, sent };
}

afterEach(() => {
  window.location.hash = '';
});

test('signed out: only the sign-in page, and nothing is fetched', async () => {
  const { auth, calls } = fakeAuth(null);
  const { impl, sent } = fakeFetch();
  render(<App api={httpApi('https://api.test/', impl, () => auth.accessToken())} auth={auth} />);
  await userEvent.click(await screen.findByRole('button', { name: 'Sign in' }));
  expect(calls).toEqual(['signIn']);
  expect(sent).toEqual([]);
  expect(screen.queryByRole('heading', { name: 'Applications' })).not.toBeInTheDocument();
});

test('signed in: the reviewer is named, and every request carries the access token', async () => {
  const { auth, calls } = fakeAuth({ username: 'ethan' });
  const { impl, sent } = fakeFetch();
  render(<App api={httpApi('https://api.test/', impl, () => auth.accessToken())} auth={auth} />);
  expect(await screen.findByText('No applications.')).toBeInTheDocument();
  expect(screen.getByText(/Signed in as ethan/)).toBeInTheDocument();
  expect(sent.map((r) => r.headers.get('authorization'))).toEqual(['Bearer access-token-1']);
  await userEvent.click(screen.getByRole('button', { name: 'Sign out' }));
  expect(calls).toEqual(['signOut']);
});

test('an expired sign-in says so and offers to sign in again', async () => {
  const { auth, calls } = fakeAuth({ username: 'ethan' });
  const { impl } = fakeFetch(401, { message: 'Unauthorized' }); // API Gateway's own refusal
  render(<App api={httpApi('https://api.test/', impl, () => auth.accessToken())} auth={auth} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Your sign-in has expired.');
  await userEvent.click(screen.getByRole('button', { name: 'Sign in again' }));
  expect(calls).toEqual(['signIn']);
});

test('a signed-in account outside the reviewers group is told why', async () => {
  const { auth } = fakeAuth({ username: 'someone' });
  const { impl } = fakeFetch(403, { error: 'only reviewers can see or decide applications' });
  render(<App api={httpApi('https://api.test/', impl, () => auth.accessToken())} auth={auth} />);
  expect(await screen.findByRole('alert')).toHaveTextContent("Your account isn't in the reviewers group.");
});

test('a failed sign-in is shown with a way to try again', async () => {
  const auth: Auth = { ...fakeAuth(null).auth, start: () => Promise.reject(new Error('No matching state found')) };
  render(<App api={httpApi('https://api.test/', fakeFetch().impl)} auth={auth} />);
  expect(await screen.findByRole('alert')).toHaveTextContent("Signing in didn't work: No matching state found");
  expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
});

test('without a user pool there is no sign-in at all', async () => {
  const { impl, sent } = fakeFetch();
  render(<App api={httpApi('https://api.test/', impl)} />);
  expect(await screen.findByText('No applications.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Sign/ })).not.toBeInTheDocument();
  expect(sent[0].headers.get('authorization')).toBeNull();
});
