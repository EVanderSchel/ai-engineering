import * as fs from 'node:fs';
import * as path from 'node:path';
import { Validations } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AwsSolutionsChecks } from 'cdk-nag';
import { testApp } from './app';
import { ACCESS_STACK, buildApp } from '../lib/app';
import { BOUNDARY_POLICY, EXECUTION_POLICY, EXECUTION_WEB_POLICY } from '../lib/access-stack';

const env = { account: '111122223333', region: 'us-east-2' };
const app = testApp();
buildApp(app, env); // the real app: all four stacks, boundary aspect included
Validations.of(app).addPlugins(new AwsSolutionsChecks(app)); // an unacknowledged finding throws here
app.synth();
const templates = Object.fromEntries(
  ['GrantReviewData', 'GrantReviewWorkflow', 'GrantReviewAuth', 'GrantReviewUi', 'GrantReviewPipeline', ACCESS_STACK].map(
    (name) => [
    name,
    Template.fromStack(app.node.findChild(name) as any),
  ]),
);
const access = templates[ACCESS_STACK];

const policy = (name: string): any[] => {
  const found = Object.values(access.findResources('AWS::IAM::ManagedPolicy')).find(
    (p: any) => p.Properties.ManagedPolicyName === name,
  ) as any;
  return found.Properties.PolicyDocument.Statement;
};
// The role gets both halves of the execution policy: every check below is on what it can do with both.
const execution = [...policy(EXECUTION_POLICY), ...policy(EXECUTION_WEB_POLICY)];
const boundary = policy(BOUNDARY_POLICY);
const list = (v: any) => [v].flat();
const allows = (statements: any[], action: string) =>
  statements
    .filter((s) => s.Effect === 'Allow')
    .some((s) => list(s.Action).some((a: string) => new RegExp(`^${a.replace(/\*/g, '.*')}$`, 'i').test(action)));

test('the execution policy allows every call CloudFormation made in our deploys', () => {
  const recorded = JSON.parse(fs.readFileSync(path.join(__dirname, 'cloudtrail-actions.json'), 'utf8')).actions;
  const missing = recorded.filter((a: string) => !allows(execution, a));
  expect(missing).toEqual([]);
  expect(allows(execution, 'iam:PassRole')).toBe(true); // a permission check, not an API call: not in CloudTrail
});

test('nothing is allowed on every resource except account-level reads, conditioned calls, and traces', () => {
  const unscoped = (statements: any[]) =>
    statements.filter((s) => s.Effect === 'Allow' && list(s.Resource).includes('*')).map((s) => s.Sid);
  expect(unscoped(execution).sort()).toEqual([
    'AccountLevelReads',
    'ApiAccessLogDelivery',
    'CreateCloudFrontPieces',
    'CreateOurUserPool',
    'EncryptionThroughServices',
  ]);
  expect(unscoped(boundary)).toEqual(['Traces']);
  const kms = execution.find((s) => s.Sid === 'EncryptionThroughServices');
  expect(Object.keys(kms.Condition.StringEquals)).toEqual(['kms:ViaService']);
});

test('user pools: created only tagged as ours, changed only when tagged as ours', () => {
  const create = execution.find((s) => s.Sid === 'CreateOurUserPool');
  expect(create.Action).toBe('cognito-idp:CreateUserPool');
  expect(create.Condition).toEqual({ StringEquals: { 'aws:RequestTag/project': 'grant-review-workflow' } });
  const change = execution.find((s) => s.Sid === 'OurUserPools');
  expect(change.Condition).toEqual({ StringEquals: { 'aws:ResourceTag/project': 'grant-review-workflow' } });
  const pool = Object.values(templates.GrantReviewAuth.findResources('AWS::Cognito::UserPool'))[0] as any;
  expect(pool.Properties.UserPoolTags).toEqual({ project: 'grant-review-workflow' }); // what the conditions check
});

test('no statement grants every action of IAM, STS or the whole account', () => {
  for (const s of [...execution, ...boundary].filter((s) => s.Effect === 'Allow')) {
    for (const a of list(s.Action)) {
      expect(a).not.toMatch(/^(\*|iam:\*|sts:\*|organizations:.*)$/);
    }
  }
});

test('roles can only be created or changed with the boundary attached', () => {
  const roleWrites = execution.find((s) => s.Sid === 'ChangeOurRolesOnlyWithTheBoundary');
  expect(roleWrites.Action).toEqual(
    expect.arrayContaining(['iam:CreateRole', 'iam:PutRolePolicy', 'iam:AttachRolePolicy', 'iam:PutRolePermissionsBoundary']),
  );
  expect(JSON.stringify(roleWrites.Condition)).toContain(`policy/${BOUNDARY_POLICY}`);
  const unconditional = execution.filter(
    (s) => s.Effect === 'Allow' && !s.Condition && list(s.Action).some((a: string) => /^iam:(Create|Put|Attach)/.test(a)),
  );
  expect(unconditional).toEqual([]);
});

test('the guardrails deny removing the boundary, changing either policy, and creating users or keys', () => {
  const denied = execution.filter((s) => s.Effect === 'Deny').flatMap((s) => list(s.Action));
  expect(denied).toEqual(
    expect.arrayContaining([
      'iam:DeleteRolePermissionsBoundary',
      'iam:CreatePolicyVersion',
      'iam:DeletePolicy',
      'iam:SetDefaultPolicyVersion',
      'iam:CreateUser',
      'iam:CreateAccessKey',
    ]),
  );
});

test('every role in the deployable stacks carries the boundary', () => {
  let roles = 0;
  for (const name of ['GrantReviewData', 'GrantReviewWorkflow', 'GrantReviewAuth', 'GrantReviewUi', 'GrantReviewPipeline']) {
    for (const role of Object.values(templates[name].findResources('AWS::IAM::Role')) as any[]) {
      roles += 1;
      expect(JSON.stringify(role.Properties.PermissionsBoundary)).toContain(`policy/${BOUNDARY_POLICY}`);
    }
  }
  expect(roles).toBe(10); // the three Lambda roles (the reviewer API's since step 8) and the pipeline's seven
});

test('the access stack deploys with a person\'s credentials, never through the bootstrap roles', () => {
  // A bootstrapped stack has a BootstrapVersion parameter and a rule checking it; this one has neither.
  const template = access.toJSON();
  expect(Object.keys(template.Parameters ?? {})).toEqual([]);
  expect(Object.keys(template.Rules ?? {})).toEqual([]);
  expect(Object.keys(access.findResources('AWS::IAM::Role'))).toEqual([]);
});

test('the pipeline deploys every stack but the access stack', () => {
  const builds = Object.values(templates.GrantReviewPipeline.findResources('AWS::CodeBuild::Project')) as any[];
  const specs = builds.map((b) => b.Properties.Source.BuildSpec).join('\n');
  expect(specs).toContain('GrantReviewData GrantReviewAuth GrantReviewWorkflow GrantReviewUi --exclusively');
  expect(specs).not.toContain(ACCESS_STACK);
  expect(builds.map((b) => b.Properties.Name).sort()).toEqual(['grant-review-deploy', 'grant-review-diff']);
});

test('deploys can never manage reviewer accounts or add an identity provider', () => {
  const denied = execution.filter((s) => s.Effect === 'Deny').flatMap((s) => list(s.Action));
  expect(denied).toEqual(
    expect.arrayContaining([
      'cognito-idp:AdminCreateUser',
      'cognito-idp:AdminAddUserToGroup',
      'cognito-idp:AdminSetUserPassword',
      'cognito-idp:CreateIdentityProvider',
    ]),
  );
});

test('HTTP APIs: created only tagged as ours, changed only when tagged as ours', () => {
  const create = execution.find((s) => s.Sid === 'CreateOurHttpApi');
  expect([create.Action, create.Resource]).toEqual(['apigateway:POST', 'arn:aws:apigateway:us-east-2::/apis']);
  expect(create.Condition).toEqual({ StringEquals: { 'aws:RequestTag/project': 'grant-review-workflow' } });
  const change = execution.find((s) => s.Sid === 'OurHttpApis');
  expect(change.Condition).toEqual({ StringEquals: { 'aws:ResourceTag/project': 'grant-review-workflow' } });
  const api = Object.values(templates.GrantReviewWorkflow.findResources('AWS::ApiGatewayV2::Api'))[0] as any;
  expect(api.Properties.Tags).toEqual({ project: 'grant-review-workflow' }); // what the conditions check
});

test('the boundary lets a role claim a status record and run our functions, nothing wider', () => {
  const checkpoints = boundary.find((s) => s.Sid === 'Checkpoints');
  expect(checkpoints.Action).toContain('dynamodb:UpdateItem');
  expect(checkpoints.Action).not.toContain('dynamodb:DeleteItem');
  const invoke = boundary.find((s) => s.Sid === 'InvokeOurFunctions');
  expect([invoke.Action, invoke.Resource]).toEqual([
    'lambda:InvokeFunction',
    'arn:aws:lambda:us-east-2:111122223333:function:GrantReview*',
  ]);
});

test("CloudFront: only the reviewer UI's kinds of resource in this account, and our function by name", () => {
  const cf = execution.find((s) => s.Sid === 'OurCloudFront');
  expect(list(cf.Resource).every((r: string) => r.startsWith('arn:aws:cloudfront::111122223333:'))).toBe(true);
  expect(list(cf.Resource)).toContain('arn:aws:cloudfront::111122223333:function/GrantReview*');
  const fn = Object.values(templates.GrantReviewUi.findResources('AWS::CloudFront::Function'))[0] as any;
  expect(fn.Properties.Name).toBe('GrantReview-strip-api'); // what the policy allows
  const ssmParams = execution.find((s) => s.Sid === 'OurParameters');
  expect(ssmParams.Resource).toBe('arn:aws:ssm:us-east-2:111122223333:parameter/grant-review/*');
});

test('the CloudFront creates that need "*" are exactly the four that support no resource', () => {
  const creates = execution.find((s) => s.Sid === 'CreateCloudFrontPieces');
  expect([...creates.Action].sort()).toEqual([
    'cloudfront:CreateCachePolicy',
    'cloudfront:CreateFunction',
    'cloudfront:CreateOriginAccessControl',
    'cloudfront:CreateResponseHeadersPolicy',
  ]);
});

test('each managed policy stays well under IAM\'s 6,144-character limit', () => {
  // IAM counts the policy without whitespace. The limit failed a deploy once (step 8): keep 10% headroom.
  for (const name of [EXECUTION_POLICY, EXECUTION_WEB_POLICY, BOUNDARY_POLICY]) {
    const size = JSON.stringify({ Version: '2012-10-17', Statement: policy(name) }).replace(/\s/g, '').length;
    expect([name, size < 6144 * 0.9]).toEqual([name, true]);
  }
});

test('the deploy-time guardrails protect all three policies', () => {
  const guard = execution.find((s) => s.Sid === 'NeverChangeOurPolicies');
  expect(JSON.stringify(guard.Resource)).toContain(`policy/${EXECUTION_WEB_POLICY}`);
  expect(list(guard.Resource)).toHaveLength(3);
});
