// Signing reviewers in with the Cognito user pool (step 8, stage 4): the authorization code flow with PKCE, through
// Cognito's hosted sign-in page, by oidc-client-ts. The app sees only the Auth interface, so tests use a stand-in.
//
// - Tokens live in sessionStorage: gone when the tab closes, and never in a URL. (Any script on the page could read
//   them, as with any browser storage: short lifetimes and refresh-token rotation limit what a stolen one is worth.)
// - The access token (15 minutes) goes to the API; when it expires, the refresh token gets a new one, and a new
//   refresh token (rotation). After 12 hours the reviewer signs in again.
// - Signing out revokes the refresh token, then ends Cognito's own session at its logout endpoint: revoking alone
//   would leave the hosted page's cookie able to issue new tokens.
import { createContext, useContext } from 'react';
import { User, UserManager, WebStorageStateStore } from 'oidc-client-ts';

export interface Session {
  username: string;
}

export interface Auth {
  /** The signed-in reviewer, or null. Finishes a sign-in first when the page is Cognito's redirect back. */
  start(): Promise<Session | null>;
  /** Off to the hosted sign-in page; back to the same page of the app afterwards. */
  signIn(): Promise<void>;
  signOut(): Promise<void>;
  /** A current access token for the API (renewed if it expired), or null when signed out. */
  accessToken(): Promise<string | null>;
}

export interface CognitoConfig {
  userPoolId: string; // us-east-2_AbC123: the region is its prefix
  clientId: string;
}

export const issuerFor = (userPoolId: string) =>
  `https://cognito-idp.${userPoolId.split('_')[0]}.amazonaws.com/${userPoolId}`;

/** Cognito's logout takes client_id and logout_uri (not the standard post_logout_redirect_uri). */
export function cognitoLogoutUrl(endSessionEndpoint: string, clientId: string, returnTo: string): string {
  const url = new URL(endSessionEndpoint);
  url.searchParams.set('client_id', clientId);
  url.searchParams.set('logout_uri', returnTo);
  return url.href;
}

/** Is this page load Cognito sending the reviewer back (?code=...&state=..., or ?error=...)? */
export const isRedirectBack = (search: string) => {
  const params = new URLSearchParams(search);
  return params.has('state') && (params.has('code') || params.has('error'));
};

const sessionFrom = (user: User | null): Session | null =>
  user ? { username: String(user.profile['cognito:username'] ?? user.profile.sub) } : null;

export function cognitoAuth(config: CognitoConfig, location: Location = window.location): Auth {
  const home = `${location.origin}/`; // the callback URL registered on the app client, exactly
  const store = new WebStorageStateStore({ store: window.sessionStorage });
  const manager = new UserManager({
    authority: issuerFor(config.userPoolId),
    client_id: config.clientId,
    redirect_uri: home,
    post_logout_redirect_uri: home,
    response_type: 'code', // with PKCE, oidc-client-ts's default
    scope: 'openid email',
    userStore: store,
    stateStore: store, // the PKCE verifier and state between leaving and coming back (default: localStorage)
    automaticSilentRenew: true, // with a refresh token: renews through the token endpoint, no iframe
    revokeTokenTypes: ['refresh_token'],
    loadUserInfo: false, // the ID token already has what the UI shows
  });

  async function current(): Promise<User | null> {
    const user = await manager.getUser();
    if (!user || !user.expired) return user;
    return manager.signinSilent().catch(() => null); // the refresh token has expired or been revoked
  }

  async function begin(): Promise<Session | null> {
    if (isRedirectBack(location.search)) {
      const user = await manager.signinRedirectCallback(location.href); // checks state, exchanges code + verifier
      const returnTo = typeof user.state === 'string' ? user.state : '';
      window.history.replaceState(null, '', `${home}${returnTo}`); // the code leaves the address bar
      return sessionFrom(user);
    }
    return sessionFrom(await current());
  }

  // Once per page load, however often it's asked: the code from Cognito can be exchanged only once, and React's
  // development mode runs effects twice (the second exchange failed with a 400 and could overwrite the first's result).
  let started: Promise<Session | null> | undefined;

  return {
    start() {
      started ??= begin();
      return started;
    },
    async signIn() {
      await manager.signinRedirect({ state: location.hash }); // the page to come back to
    },
    async signOut() {
      await manager.revokeTokens(['refresh_token']).catch(() => undefined); // best effort: sign out regardless
      await manager.removeUser();
      const endSession = await manager.metadataService.getEndSessionEndpoint();
      location.assign(endSession ? cognitoLogoutUrl(endSession, config.clientId, home) : home);
    },
    async accessToken() {
      return (await current())?.access_token ?? null;
    },
  };
}

export function defaultAuth(): Auth | undefined {
  const userPoolId = import.meta.env.VITE_COGNITO_USER_POOL_ID;
  const clientId = import.meta.env.VITE_COGNITO_CLIENT_ID;
  return userPoolId && clientId ? cognitoAuth({ userPoolId, clientId }) : undefined;
}

export const AuthContext = createContext<Auth | undefined>(undefined);
export const useAuth = () => useContext(AuthContext);
