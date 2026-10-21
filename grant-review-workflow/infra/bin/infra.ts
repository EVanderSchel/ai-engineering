#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { DataStack } from '../lib/data-stack';

const app = new cdk.App();

// One project, one Region (confirmed in AWS Settings). The account comes from the signed-in profile
// (CDK_DEFAULT_ACCOUNT), so no account ID is committed.
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'us-east-2' };

new DataStack(app, 'GrantReviewData', {
  env,
  adopting: app.node.tryGetContext('adopt') === 'true',
  terminationProtection: true, // the stack holds state: deleting it takes a deliberate extra step
  description: 'grant-review-workflow: checkpoint table and intake bucket',
});

cdk.Tags.of(app).add('project', 'grant-review-workflow');
// cdk-nag: AWS Solutions security checks at synth time; an unacknowledged finding fails the synth.
Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));
