#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { DataStack } from '../lib/data-stack';
import { WorkflowStack } from '../lib/workflow-stack';

const app = new cdk.App();

// One project, one Region (confirmed in AWS Settings). The account comes from the signed-in profile
// (CDK_DEFAULT_ACCOUNT), so no account ID is committed.
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

cdk.Tags.of(app).add('project', 'grant-review-workflow');
// cdk-nag: AWS Solutions security checks at synth time; an unacknowledged finding fails the synth.
Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));
