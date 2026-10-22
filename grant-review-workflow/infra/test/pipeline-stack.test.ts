import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { AwsSolutionsChecks } from 'cdk-nag';
import { testApp } from './app';
import { PipelineStack } from '../lib/pipeline-stack';

const env = { account: '111122223333', region: 'us-east-2' };

const app = testApp();
const stack = new PipelineStack(app, 'Pipeline', { env, owner: 'EVanderSchel', repo: 'ai-engineering' });
Validations.of(app).addPlugins(new AwsSolutionsChecks(app)); // an unacknowledged finding throws here
app.synth();
const template = Template.fromStack(stack);

const roleStatements = (logicalPrefix: string): any[] => {
  const policies = template.findResources('AWS::IAM::Policy');
  return Object.entries(policies)
    .filter(([id]) => id.startsWith(logicalPrefix))
    .flatMap(([, p]: [string, any]) => p.Properties.PolicyDocument.Statement);
};

test('a GitHub connection, completed once in the console, is the only link to GitHub', () => {
  template.hasResourceProperties('AWS::CodeConnections::Connection', {
    ConnectionName: 'grant-review-github',
    ProviderType: 'GitHub',
  });
  expect(JSON.stringify(template.toJSON())).not.toMatch(/OIDCProvider|AccessKey|oauth/i);
});

test('only pushes to main that touch the project start the pipeline', () => {
  template.hasResourceProperties('AWS::CodePipeline::Pipeline', {
    PipelineType: 'V2',
    ExecutionMode: 'QUEUED',
    Triggers: [
      Match.objectLike({
        ProviderType: 'CodeStarSourceConnection',
        GitConfiguration: Match.objectLike({
          Push: [{ Branches: { Includes: ['main'] }, FilePaths: { Includes: ['grant-review-workflow/**'] } }],
        }),
      }),
    ],
  });
});

test('a person approves between the diff and the deploy', () => {
  const pipeline = Object.values(template.findResources('AWS::CodePipeline::Pipeline'))[0] as any;
  const stages = pipeline.Properties.Stages.map((s: any) => [s.Name, s.Actions.map((a: any) => a.ActionTypeId.Provider)]);
  expect(stages).toEqual([
    ['Source', ['CodeStarSourceConnection']],
    ['Diff', ['CodeBuild']],
    ['Approve', ['Manual']],
    ['Deploy', ['CodeBuild']],
  ]);
});

test('the build roles can only assume CDK bootstrap roles, the deploy one a few more', () => {
  const assumeTargets = (prefix: string) =>
    roleStatements(prefix)
      .filter((s) => s.Sid === 'AssumeCdkBootstrapRoles')
      .flatMap((s) => s.Resource)
      .map((arn: string) => arn.split('/')[1])
      .sort(); // CDK sorts policy resources (a cdk.json feature flag); order isn't meaningful
  expect(assumeTargets('DiffRole')).toEqual(
    ['cdk-hnb659fds-lookup-role-111122223333-us-east-2', 'cdk-hnb659fds-deploy-role-111122223333-us-east-2'].sort(),
  );
  expect(assumeTargets('DeployRole')).toEqual(
    [
      'cdk-hnb659fds-lookup-role-111122223333-us-east-2',
      'cdk-hnb659fds-deploy-role-111122223333-us-east-2',
      'cdk-hnb659fds-file-publishing-role-111122223333-us-east-2',
      'cdk-hnb659fds-image-publishing-role-111122223333-us-east-2',
    ].sort(),
  );
  for (const prefix of ['DiffRole', 'DeployRole']) {
    const text = JSON.stringify(roleStatements(prefix));
    expect(text).not.toMatch(/"(cloudformation|lambda|dynamodb|iam):/); // nothing that deploys directly
  }
});

test('only the deploy build runs Docker', () => {
  template.hasResourceProperties('AWS::CodeBuild::Project', {
    Description: Match.stringLikeRegexp('cdk deploy'),
    Environment: Match.objectLike({ PrivilegedMode: true, Image: 'aws/codebuild/standard:8.0' }),
  });
  template.hasResourceProperties('AWS::CodeBuild::Project', {
    Description: Match.stringLikeRegexp('cdk diff'),
    Environment: Match.objectLike({ PrivilegedMode: false }),
  });
});

test('every cdk-nag finding is fixed or acknowledged with a reason', () => {
  expect(stack).toBeDefined(); // the synth above throws on an unacknowledged finding
});
