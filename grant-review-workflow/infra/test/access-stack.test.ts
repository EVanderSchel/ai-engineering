import * as fs from 'node:fs';
import * as path from 'node:path';
import { Validations } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AwsSolutionsChecks } from 'cdk-nag';
import { testApp } from './app';
import { ACCESS_STACK, buildApp } from '../lib/app';
import { BOUNDARY_POLICY, EXECUTION_POLICY } from '../lib/access-stack';

const env = { account: '111122223333', region: 'us-east-2' };
const app = testApp();
buildApp(app, env); // the real app: all four stacks, boundary aspect included
Validations.of(app).addPlugins(new AwsSolutionsChecks(app)); // an unacknowledged finding throws here
app.synth();
const templates = Object.fromEntries(
  ['GrantReviewData', 'GrantReviewWorkflow', 'GrantReviewAuth', 'GrantReviewPipeline', ACCESS_STACK].map((name) => [
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
const execution = policy(EXECUTION_POLICY);
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
  expect(unscoped(execution).sort()).toEqual(['AccountLevelReads', 'CreateOurUserPool', 'EncryptionThroughServices']);
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
  for (const name of ['GrantReviewData', 'GrantReviewWorkflow', 'GrantReviewAuth', 'GrantReviewPipeline']) {
    for (const role of Object.values(templates[name].findResources('AWS::IAM::Role')) as any[]) {
      roles += 1;
      expect(JSON.stringify(role.Properties.PermissionsBoundary)).toContain(`policy/${BOUNDARY_POLICY}`);
    }
  }
  expect(roles).toBe(9); // the two Lambda roles and the pipeline's seven, as CloudTrail recorded them
});

test('the access stack deploys with a person\'s credentials, never through the bootstrap roles', () => {
  // A bootstrapped stack has a BootstrapVersion parameter and a rule checking it; this one has neither.
  const template = access.toJSON();
  expect(Object.keys(template.Parameters ?? {})).toEqual([]);
  expect(Object.keys(template.Rules ?? {})).toEqual([]);
  expect(Object.keys(access.findResources('AWS::IAM::Role'))).toEqual([]);
});

test('the pipeline deploys only the data and workflow stacks', () => {
  const builds = Object.values(templates.GrantReviewPipeline.findResources('AWS::CodeBuild::Project')) as any[];
  const specs = builds.map((b) => b.Properties.Source.BuildSpec).join('\n');
  expect(specs).toContain('GrantReviewData GrantReviewWorkflow --exclusively');
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
