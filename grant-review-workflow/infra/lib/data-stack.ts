import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

/** The intake bucket's name: fixed, so uploaders and the workflow's role name it without cross-stack lookups. */
export function intakeBucketName(account: string): string {
  return `grant-review-intake-${account}`;
}

export interface DataStackProps extends cdk.StackProps {
  /**
   * True only for the one-time `cdk import` of the existing checkpoint table. An import change set may add
   * nothing but the imported resource, and its properties must match the live table, so in this mode the stack
   * is an exact copy of what src/checkpoint_table.py created (no deletion protection, no bucket).
   */
  readonly adopting?: boolean;
}

/**
 * The workflow's state: the checkpoint table (adopted from src/checkpoint_table.py, design decision 8) and the
 * intake bucket that applications arrive in. Stateful, so it lives apart from the code that uses it and is
 * never replaced or deleted by a deploy.
 */
export class DataStack extends cdk.Stack {
  readonly table: dynamodb.ITable;
  readonly intake?: s3.IBucket;
  readonly applications?: dynamodb.ITable;

  constructor(scope: Construct, id: string, props: DataStackProps = {}) {
    super(scope, id, props);

    // Exactly the table the script made: keys PK/SK as langgraph-checkpoint-aws expects, on-demand, TTL on
    // "ttl" (30 days, stamped by the saver), encrypted with DynamoDB's AWS-owned key, no point-in-time recovery.
    const table = new dynamodb.Table(this, 'Checkpoints', {
      tableName: 'grant-review-checkpoints',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN, // a stack deletion leaves the table (and its data) in place
      deletionProtection: props.adopting ? undefined : true, // turned on by the first deploy after the import
    });
    cdk.Tags.of(table).add('purpose', 'langgraph-checkpoints');
    this.table = table;
    Validations.of(table).acknowledge({
      id: 'AwsSolutions-DDB3',
      reason: 'No point-in-time recovery (decision 8): a lost checkpoint costs one rerun of an application.',
    });
    if (props.adopting) {
      return;
    }

    // Applications arrive here as <application id>/form.json, narrative.md and return.pdf. Private, encrypted,
    // TLS only, and emptied after 30 days, the same as the checkpoints (docs/data-handling.md).
    const intake = new s3.Bucket(this, 'Intake', {
      bucketName: intakeBucketName(this.account),
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(30) }],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    // Uploads go to EventBridge, where the workflow stack starts a run from them. Set on the bucket's own
    // CloudFormation resource: the L2 `eventBridgeEnabled` option deploys a helper Lambda to do the same.
    (intake.node.defaultChild as s3.CfnBucket).notificationConfiguration = {
      eventBridgeConfiguration: { eventBridgeEnabled: true },
    };
    cdk.Tags.of(intake).add('purpose', 'application-intake');
    this.intake = intake;

    // Where each application stands (step 8, src/applications.py): what the reviewer UI's queue lists. One record per
    // application, rewritten after every run; the queue is one query on the by-status index. Records expire with
    // their checkpoints (30 days, "ttl"), so the queue never lists an application whose evidence is gone.
    const applications = new dynamodb.Table(this, 'Applications', {
      tableName: 'grant-review-applications',
      partitionKey: { name: 'application_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      deletionProtection: true,
    });
    applications.addGlobalSecondaryIndex({
      indexName: 'by-status',
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'received_at', type: dynamodb.AttributeType.STRING }, // ISO 8601 in UTC: sorts as time
      projectionType: dynamodb.ProjectionType.ALL, // a record is small; the queue needs every field
    });
    cdk.Tags.of(applications).add('purpose', 'review-queue');
    this.applications = applications;
    Validations.of(applications).acknowledge({
      id: 'AwsSolutions-DDB3',
      reason: 'No point-in-time recovery, as for the checkpoints (decision 8): each record is rewritten by the next run.',
    });
    Validations.of(intake).acknowledge({
      id: 'AwsSolutions-S1',
      reason: 'No server access logs: one uploader (the fund), and CloudTrail records who changed the bucket.',
    });
  }
}
