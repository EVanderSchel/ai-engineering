#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { DataStack } from '../lib/data-stack';
import { WorkflowStack } from '../lib/workflow-stack';
import { PipelineStack } from '../lib/pipeline-stack';

const app = new cdk.App();

// One project, one Region (confirmed in AWS Settings). The account comes from the signed-in profile
// (CDK_DEFAULT_ACCOUNT), so no account ID is committed.
// Every stack is tied to this one account (bucket names, role ARNs), so an unknown account is an error, not a
// placeholder: CDK sets CDK_DEFAULT_ACCOUNT from the credentials, and an expired sign-in leaves it unset.
if (!process.env.CDK_DEFAULT_ACCOUNT) {
  throw new Error('No AWS account: sign in first (aws login --profile agent-toolkit), then set AWS_PROFILE=agent-toolkit.');
}
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'us-east-2' };

const adopting = app.node.tryGetContext('adopt') === 'true';
const data = new DataStack(app, 'GrantReviewData', {
  env,
  adopting,
  terminationProtection: true, // the stack holds state: deleting it takes a deliberate extra step
  description: 'grant-review-workflow: checkpoint table and intake bucket',
});

if (!adopting) {
  // The code that uses the data; replaceable at any time, unlike the data stack.
  const workflow = new WorkflowStack(app, 'GrantReviewWorkflow', {
    env,
    table: data.table,
    description: 'grant-review-workflow: start and resume functions, upload rule, failed-applications queue',
  });
  workflow.addStackDependency(data); // the bucket is named, not referenced, so state the order explicitly
}

// Deploys the two stacks above on merge to main, after a person approves the diff (stage 6). Deployed once from a
// laptop; it doesn't deploy itself.
new PipelineStack(app, 'GrantReviewPipeline', {
  env,
  owner: 'EVanderSchel',
  repo: 'ai-engineering',
  description: 'grant-review-workflow: GitHub connection and the diff, approve, deploy pipeline',
});

cdk.Tags.of(app).add('project', 'grant-review-workflow');
// cdk-nag: AWS Solutions security checks at synth time; an unacknowledged finding fails the synth.
Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));
