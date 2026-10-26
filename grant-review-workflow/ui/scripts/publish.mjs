// Builds the UI for the deployed site and uploads it (step 8, stage 5): `npm run publish`, after the GrantReviewAuth
// and GrantReviewUi stacks are deployed. The pipeline runs it after each deploy; run by hand with AWS_PROFILE set.
//
//   1. read the stacks' outputs: the user pool and app client, the site bucket and the distribution
//   2. build with the production settings (the API at /api/ on the same origin, the pool's IDs; no fixtures)
//   3. upload: hashed assets cached for a year (a new build has new names), index.html never cached
//   4. ask CloudFront to drop its copy of index.html, so the new build is served at once
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REGION = process.env.AWS_REGION ?? 'us-east-2';
const UI = fileURLToPath(new URL('..', import.meta.url)); // every command runs here: dist/ is relative to it

/** The build's settings and the upload steps, from the stacks' outputs: pure, so it's tested without AWS. */
export function plan(auth, ui) {
  for (const [stack, outputs, keys] of [
    ['GrantReviewAuth', auth, ['UserPoolId', 'ClientId']],
    ['GrantReviewUi', ui, ['SiteBucket', 'DistributionId', 'ReviewerUiUrl']],
  ]) {
    const missing = keys.filter((k) => !outputs[k]);
    if (missing.length) throw new Error(`${stack} has no output ${missing.join(', ')}: is it deployed?`);
  }
  const bucket = `s3://${ui.SiteBucket}`;
  return {
    env: {
      VITE_API_URL: '/api/',
      VITE_COGNITO_USER_POOL_ID: auth.UserPoolId,
      VITE_COGNITO_CLIENT_ID: auth.ClientId,
    },
    uploads: [
      ['s3', 'sync', 'dist/assets', `${bucket}/assets`, '--cache-control', 'public, max-age=31536000, immutable'],
      ['s3', 'cp', 'dist/index.html', `${bucket}/index.html`, '--cache-control', 'no-cache'],
    ],
    invalidate: ['cloudfront', 'create-invalidation', '--distribution-id', ui.DistributionId, '--paths', '/', '/index.html'],
    url: ui.ReviewerUiUrl,
  };
}

function aws(args) {
  return execFileSync('aws', [...args, '--region', REGION, '--output', 'json'], { encoding: 'utf8', cwd: UI });
}

function outputs(stack) {
  const found = JSON.parse(aws(['cloudformation', 'describe-stacks', '--stack-name', stack]));
  return Object.fromEntries((found.Stacks[0].Outputs ?? []).map((o) => [o.OutputKey, o.OutputValue]));
}

function main() {
  const steps = plan(outputs('GrantReviewAuth'), outputs('GrantReviewUi'));
  console.log('Building for', steps.url);
  const build = spawnSync('npm', ['run', 'build'], { stdio: 'inherit', shell: true, cwd: UI, env: { ...process.env, ...steps.env } });
  if (build.status !== 0) process.exit(build.status ?? 1);
  for (const upload of steps.uploads) {
    console.log('aws', upload.slice(0, 4).join(' '));
    aws(upload);
  }
  const invalidation = JSON.parse(aws(steps.invalidate));
  console.log(`Published. CloudFront invalidation ${invalidation.Invalidation.Id}; the new build is live within a minute.`);
  console.log(steps.url);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}
