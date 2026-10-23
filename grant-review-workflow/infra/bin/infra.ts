#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { buildApp } from '../lib/app';

const app = new cdk.App();

// One project, one Region (confirmed in AWS Settings). The account comes from the signed-in profile
// (CDK_DEFAULT_ACCOUNT), so no account ID is committed.
// Every stack is tied to this one account (bucket names, role ARNs), so an unknown account is an error, not a
// placeholder: CDK sets CDK_DEFAULT_ACCOUNT from the credentials, and an expired sign-in leaves it unset.
if (!process.env.CDK_DEFAULT_ACCOUNT) {
  throw new Error('No AWS account: sign in first (aws login --profile agent-toolkit), then set AWS_PROFILE=agent-toolkit.');
}
buildApp(app, { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'us-east-2' });

// cdk-nag: AWS Solutions security checks at synth time; an unacknowledged finding fails the synth.
Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));
