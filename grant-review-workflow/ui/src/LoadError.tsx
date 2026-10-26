import { RequestFailed } from './api/client';
import { useAuth } from './auth';

/** Why something couldn't load, in terms a reviewer can act on. */
export function LoadError({ what, error }: { what: string; error: Error }) {
  const auth = useAuth();
  if (error instanceof RequestFailed && error.status === 401) {
    return (
      <p role="alert">
        Your sign-in has expired.{' '}
        {auth && (
          <button type="button" onClick={() => auth.signIn()}>
            Sign in again
          </button>
        )}
      </p>
    );
  }
  if (error instanceof RequestFailed && error.status === 403) {
    return <p role="alert">Your account isn't in the reviewers group. Ask the fund's administrator to add it.</p>;
  }
  return (
    <p role="alert">
      Couldn't load {what}: {error.message}
    </p>
  );
}
