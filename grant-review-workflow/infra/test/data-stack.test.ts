import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { AwsSolutionsChecks } from 'cdk-nag';
import { testApp } from './app';
import { DataStack } from '../lib/data-stack';

const env = { account: '111122223333', region: 'us-east-2' };

function synth(adopting: boolean) {
  const app = testApp();
  const stack = new DataStack(app, 'Data', { env, adopting });
  Validations.of(app).addPlugins(new AwsSolutionsChecks(app)); // an unacknowledged finding throws here
  app.synth();
  return { stack, template: Template.fromStack(stack) };
}

// What src/checkpoint_table.py created (and describe-table reports). An import only works if the template
// matches the live table, so this is the contract.
const LIVE_TABLE = {
  TableName: 'grant-review-checkpoints',
  KeySchema: [
    { AttributeName: 'PK', KeyType: 'HASH' },
    { AttributeName: 'SK', KeyType: 'RANGE' },
  ],
  AttributeDefinitions: [
    { AttributeName: 'PK', AttributeType: 'S' },
    { AttributeName: 'SK', AttributeType: 'S' },
  ],
  BillingMode: 'PAY_PER_REQUEST',
  TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
};

describe('adopting the existing table', () => {
  const { template } = synth(true);

  test('the template is an exact copy of the live table and nothing else', () => {
    template.resourceCountIs('AWS::DynamoDB::Table', 1);
    expect(Object.keys(template.toJSON().Resources)).toHaveLength(1);
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      ...LIVE_TABLE,
      DeletionProtectionEnabled: Match.absent(),
      PointInTimeRecoverySpecification: Match.absent(),
    });
  });

  test('a stack deletion would leave the table in place', () => {
    template.hasResource('AWS::DynamoDB::Table', { DeletionPolicy: 'Retain', UpdateReplacePolicy: 'Retain' });
  });
});

describe('after the import', () => {
  const { template } = synth(false);

  test('the table keeps its shape and gains deletion protection', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', { ...LIVE_TABLE, DeletionProtectionEnabled: true });
  });

  test('the intake bucket is private, encrypted, TLS-only and emptied after 30 days', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: 'grant-review-intake-111122223333',
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [{ ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } }],
      },
      LifecycleConfiguration: { Rules: [Match.objectLike({ ExpirationInDays: 30, Status: 'Enabled' })] },
      NotificationConfiguration: { EventBridgeConfiguration: { EventBridgeEnabled: true } },
    });
    template.hasResourceProperties('AWS::S3::BucketPolicy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({ Effect: 'Deny', Condition: { Bool: { 'aws:SecureTransport': 'false' } } }),
        ]),
      },
    });
    template.hasResource('AWS::S3::Bucket', { DeletionPolicy: 'Retain' });
  });

  test('no helper Lambda is deployed for the bucket notification', () => {
    template.resourceCountIs('AWS::Lambda::Function', 0);
    template.resourceCountIs('AWS::IAM::Role', 0);
  });

  test('every cdk-nag finding is fixed or acknowledged with a reason', () => {
    expect(() => synth(true)).not.toThrow();
    expect(() => synth(false)).not.toThrow();
  });
});
