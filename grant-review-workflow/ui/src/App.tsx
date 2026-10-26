import { useEffect, useState } from 'react';
import { HashRouter, Route, Routes } from 'react-router';
import { type Api, ApiContext } from './api/client';
import { type Auth, AuthContext, type Session } from './auth';
import { ApplicationPage } from './pages/ApplicationPage';
import { QueuePage } from './pages/QueuePage';

type SignIn = { state: 'checking' } | { state: 'error'; error: Error } | { state: 'done'; session: Session | null };

// Hash routes (#/applications/gp-039): the app is static files in a bucket (stage 5), and the part after # never
// reaches the server, so every route works without rewrite rules.
//
// With `auth` (a user pool configured), nothing is shown or fetched until a reviewer has signed in. Without it: the
// fixtures, or the local API's fixed reviewer.
export function App({ api, auth }: { api: Api; auth?: Auth }) {
  const [signIn, setSignIn] = useState<SignIn>(
    auth ? { state: 'checking' } : { state: 'done', session: null },
  );
  useEffect(() => {
    if (!auth) return;
    auth.start().then(
      (session) => setSignIn({ state: 'done', session }),
      (error: Error) => setSignIn({ state: 'error', error }),
    );
  }, [auth]);

  const signedIn = signIn.state === 'done' && signIn.session;
  return (
    <AuthContext value={auth}>
      <ApiContext value={api}>
        <HashRouter>
          <div className="banner" role="note">
            <span>
              Prairie Commons Fund: grant review{import.meta.env.VITE_API_URL ? '' : ' (local fixtures)'}
            </span>
            {auth && signedIn && (
              <span className="account">
                Signed in as {signedIn.username}{' '}
                <button type="button" onClick={() => auth.signOut()}>
                  Sign out
                </button>
              </span>
            )}
          </div>
          {auth && signIn.state === 'checking' && (
            <main>
              <p aria-busy="true">Checking your sign-in...</p>
            </main>
          )}
          {auth && signIn.state === 'error' && (
            <main>
              <p role="alert">Signing in didn't work: {signIn.error.message}</p>
              <button type="button" onClick={() => auth.signIn()}>
                Try again
              </button>
            </main>
          )}
          {auth && signIn.state === 'done' && !signIn.session && (
            <main className="sign-in">
              <h1>Sign in to review applications</h1>
              <p>Reviewers sign in with their Prairie Commons Fund account and an authenticator app.</p>
              <button type="button" onClick={() => auth.signIn()}>
                Sign in
              </button>
            </main>
          )}
          {(!auth || signedIn) && (
            <Routes>
              <Route path="/" element={<QueuePage />} />
              <Route path="/applications/:applicationId" element={<ApplicationPage />} />
              <Route path="*" element={<main><p>Page not found.</p></main>} />
            </Routes>
          )}
        </HashRouter>
      </ApiContext>
    </AuthContext>
  );
}
