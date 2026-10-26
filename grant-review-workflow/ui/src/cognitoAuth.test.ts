// cognitoAuth against a stand-in for oidc-client-ts's UserManager: what it asks the library to do, not Cognito.
const library = vi.hoisted(() => ({
  callbacks: [] as string[],
  revoked: [] as string[][],
  removed: 0,
  user: null as null | { expired: boolean; access_token: string; profile: Record<string, string>; state?: string },
}));

vi.mock('oidc-client-ts', () => ({
  WebStorageStateStore: class {},
  UserManager: class {
    metadataService = { getEndSessionEndpoint: async () => 'https://pool.auth.us-east-2.amazoncognito.com/logout' };
    async signinRedirectCallback(url: string) {
      library.callbacks.push(url);
      return { access_token: 't1', expired: false, profile: { 'cognito:username': 'ethan' }, state: '#/applications/gp-027' };
    }
    async getUser() {
      return library.user;
    }
    async signinSilent() {
      return null;
    }
    async revokeTokens(types: string[]) {
      library.revoked.push(types);
    }
    async removeUser() {
      library.removed += 1;
    }
  },
}));

import { cognitoAuth } from './auth';

function fakeLocation(search: string) {
  const assigned: string[] = [];
  const location = {
    origin: 'http://localhost:5174',
    search,
    hash: '',
    href: `http://localhost:5174/${search}`,
    assign: (url: string) => void assigned.push(url),
  } as unknown as Location;
  return { location, assigned };
}

beforeEach(() => {
  library.callbacks = [];
  library.revoked = [];
  library.removed = 0;
  library.user = null;
});

test("Cognito's redirect back is processed once, however often start() is called", async () => {
  const { location } = fakeLocation('?code=abc&state=s1');
  const replaced = vi.spyOn(window.history, 'replaceState').mockImplementation(() => undefined);
  const auth = cognitoAuth({ userPoolId: 'us-east-2_Pool', clientId: 'client1' }, location);
  const [first, second] = await Promise.all([auth.start(), auth.start()]); // React's development mode does this
  expect(library.callbacks).toHaveLength(1);
  expect(first).toEqual({ username: 'ethan' });
  expect(second).toEqual(first);
  // The code leaves the address bar, and the reviewer is back on the page they were on before signing in.
  expect(replaced).toHaveBeenCalledExactlyOnceWith(null, '', 'http://localhost:5174/#/applications/gp-027');
  replaced.mockRestore();
});

test('an expired access token with no way to renew it means signed out', async () => {
  library.user = { expired: true, access_token: 'old', profile: { 'cognito:username': 'ethan' } };
  const auth = cognitoAuth({ userPoolId: 'us-east-2_Pool', clientId: 'client1' }, fakeLocation('').location);
  expect(await auth.accessToken()).toBeNull();
  expect(await auth.start()).toBeNull();
});

test('signing out revokes the refresh token, forgets the user, and ends the Cognito session', async () => {
  const { location, assigned } = fakeLocation('');
  await cognitoAuth({ userPoolId: 'us-east-2_Pool', clientId: 'client1' }, location).signOut();
  expect(library.revoked).toEqual([['refresh_token']]);
  expect(library.removed).toBe(1);
  expect(assigned).toEqual([
    'https://pool.auth.us-east-2.amazoncognito.com/logout?client_id=client1&logout_uri=http%3A%2F%2Flocalhost%3A5174%2F',
  ]);
});
