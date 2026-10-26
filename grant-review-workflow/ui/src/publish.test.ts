// @vitest-environment node
import { plan } from '../scripts/publish.mjs';

const auth = { UserPoolId: 'us-east-2_Example123', ClientId: 'client1', SignInDomain: 'x.auth.us-east-2.amazoncognito.com' };
const ui = {
  SiteBucket: 'grant-review-ui-111122223333',
  DistributionId: 'E2EXAMPLE',
  ReviewerUiUrl: 'https://d111.cloudfront.net/',
};

test('the deployed build calls the API on its own origin and signs in with the pool', () => {
  expect(plan(auth, ui).env).toEqual({
    VITE_API_URL: '/api/',
    VITE_COGNITO_USER_POOL_ID: 'us-east-2_Example123',
    VITE_COGNITO_CLIENT_ID: 'client1',
  });
});

test('hashed assets are cached for a year, index.html never, and CloudFront drops its copy', () => {
  const steps = plan(auth, ui);
  expect(steps.uploads).toEqual([
    ['s3', 'sync', 'dist/assets', 's3://grant-review-ui-111122223333/assets', '--cache-control', 'public, max-age=31536000, immutable'],
    ['s3', 'cp', 'dist/index.html', 's3://grant-review-ui-111122223333/index.html', '--cache-control', 'no-cache'],
  ]);
  expect(steps.invalidate).toEqual(['cloudfront', 'create-invalidation', '--distribution-id', 'E2EXAMPLE', '--paths', '/', '/index.html']);
});

test('a stack that is not deployed yet is named, not a mysterious failure', () => {
  expect(() => plan(auth, { SiteBucket: 'b' })).toThrow('GrantReviewUi has no output DistributionId, ReviewerUiUrl');
  expect(() => plan({}, ui)).toThrow('GrantReviewAuth has no output UserPoolId, ClientId');
});
