import * as path from 'node:path';
import * as fs from 'node:fs';
import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as destinations from 'aws-cdk-lib/aws-lambda-destinations';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import { intakeBucketName } from './data-stack';

/** What each function's role needs, written by `src/iam_policy.py --export` from the workflow's own code. */
interface Runtime {
  region: string;
  profile_regions: string[];
  checkpoint_table: string;
  checkpoint_actions: string[];
  functions: Record<'start' | 'resume', { inference_profiles: string[] }>;
}

export function loadRuntime(file = path.join(__dirname, '..', 'runtime.json')): Runtime {
  return JSON.parse(fs.readFileSync(file, 'utf8')) as Runtime;
}

export interface WorkflowStackProps extends cdk.StackProps {
  readonly table: dynamodb.ITable;
  readonly runtime?: Runtime;
}

/**
 * The workflow on Lambda (step 7): `start` runs an application when its form.json lands in the intake bucket,
 * `resume` continues it with the reviewer's decision. Both run the same container image (../Dockerfile) with a
 * different handler, and each has its own role built from runtime.json: the step 6d policy, split by function.
 */
export class WorkflowStack extends cdk.Stack {
  readonly start: lambda.Function;
  readonly resume: lambda.Function;
  readonly failed: sqs.Queue;

  constructor(scope: Construct, id: string, props: WorkflowStackProps) {
    super(scope, id, props);
    const runtime = props.runtime ?? loadRuntime();

    // Built from the project folder; .dockerignore keeps everything but pyproject, uv.lock, src/ and prompts/ out.
    const image = (handler: string) =>
      lambda.DockerImageCode.fromImageAsset(path.join(__dirname, '..', '..'), {
        cmd: [handler],
        platform: ecrAssets.Platform.LINUX_AMD64,
      });

    // Applications that still fail after Lambda's retries land here (the event, not the applicant's files),
    // so nothing is lost silently. Kept 14 days, the queue's maximum.
    this.failed = new sqs.Queue(this, 'FailedApplications', {
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      retentionPeriod: cdk.Duration.days(14),
    });
    Validations.of(this.failed).acknowledge({
      id: 'AwsSolutions-SQS3',
      reason: 'This queue is itself the dead-letter destination for failed applications.',
    });

    this.start = this.workflowFunction('Start', {
      handler: 'handlers.start',
      image: image('handlers.start'),
      profiles: runtime.functions.start.inference_profiles,
      runtime,
      table: props.table,
      // A run is seconds to a few minutes (Bedrock retries included); 10 minutes leaves room for an outage.
      timeout: cdk.Duration.minutes(10),
    });
    // Read the three packet files, nothing else (grantRead would add GetObject*, GetBucket* and List*).
    const bucket = intakeBucketName(this.account);
    const packetFiles = ['form.json', 'narrative.md', 'return.pdf'].map((f) => `arn:aws:s3:::${bucket}/*/${f}`);
    this.start.role!.addToPrincipalPolicy(
      new iam.PolicyStatement({ sid: 'ReadApplicationPackets', actions: ['s3:GetObject'], resources: packetFiles }),
    );
    for (const arn of packetFiles) {
      Validations.of(this.start.role!).acknowledge({
        id: `AwsSolutions-IAM5[Resource::${arn}]`,
        reason: 'Application IDs are not known in advance; reads are limited to the three packet files.',
      });
    }

    // Lambda retries a failed asynchronous run twice (the handler continues from the checkpoint), then gives up.
    new lambda.EventInvokeConfig(this, 'StartRetries', {
      function: this.start,
      retryAttempts: 2,
      maxEventAge: cdk.Duration.hours(1),
      onFailure: new destinations.SqsDestination(this.failed),
    });

    // form.json is uploaded last, so its "Object Created" event means the packet is complete.
    new events.Rule(this, 'ApplicationUploaded', {
      description: 'Start the workflow when an application form lands in the intake bucket',
      eventPattern: {
        source: ['aws.s3'],
        detailType: ['Object Created'],
        detail: {
          bucket: { name: [bucket] },
          object: { key: [{ suffix: '/form.json' }] },
        },
      },
      targets: [new targets.LambdaFunction(this.start, { retryAttempts: 2, maxEventAge: cdk.Duration.hours(1) })],
    });

    this.resume = this.workflowFunction('Resume', {
      handler: 'handlers.resume',
      image: image('handlers.resume'),
      profiles: runtime.functions.resume.inference_profiles,
      runtime,
      table: props.table,
      timeout: cdk.Duration.minutes(2), // eligibility, route and a letter: seconds
    });

    new cdk.CfnOutput(this, 'StartFunctionName', { value: this.start.functionName });
    new cdk.CfnOutput(this, 'ResumeFunctionName', { value: this.resume.functionName });
    new cdk.CfnOutput(this, 'FailedApplicationsQueue', { value: this.failed.queueUrl });
  }

  /** A function on the shared image with its own log group and a role holding only what runtime.json lists. */
  private workflowFunction(
    id: string,
    opts: {
      handler: string;
      image: lambda.DockerImageCode;
      profiles: string[];
      runtime: Runtime;
      table: dynamodb.ITable;
      timeout: cdk.Duration;
    },
  ): lambda.Function {
    const logGroup = new logs.LogGroup(this, `${id}Logs`, {
      retention: logs.RetentionDays.ONE_MONTH, // the same 30 days as the checkpoints
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // No AWS managed policies (AWSLambdaBasicExecutionRole allows logging to any group): everything is listed.
    const role = new iam.Role(this, `${id}Role`, {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: `grant-review-workflow ${opts.handler}: its Bedrock profiles, the checkpoint table, its logs`,
    });
    logGroup.grantWrite(role);

    const profileArns = opts.profiles.map((p) => `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/${p}`);
    const modelArns = opts.profiles.flatMap((p) =>
      opts.runtime.profile_regions.map((r) => `arn:aws:bedrock:${r}::foundation-model/${p.replace(/^us\./, '')}`),
    );
    role.addToPolicy(
      new iam.PolicyStatement({ sid: 'InvokeItsInferenceProfiles', actions: ['bedrock:InvokeModel'], resources: profileArns }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeTheirModelsOnlyThroughThoseProfiles',
        actions: ['bedrock:InvokeModel'],
        resources: modelArns,
        conditions: { StringEquals: { 'bedrock:InferenceProfileArn': profileArns } },
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ReadAndWriteCheckpoints',
        actions: opts.runtime.checkpoint_actions,
        resources: [opts.table.tableArn],
      }),
    );

    const fn = new lambda.DockerImageFunction(this, id, {
      code: opts.image,
      role,
      logGroup,
      architecture: lambda.Architecture.X86_64,
      memorySize: 1024, // page rendering and the LangGraph imports; more memory also means more CPU
      timeout: opts.timeout,
      description: `grant-review-workflow ${opts.handler}`,
      // Observability (stage 5). Active tracing runs Lambda's X-Ray daemon (and grants the role permission to
      // write to it); the ADOT wrapper unpacked into the image sends each step's span and the AWS SDK calls inside
      // it there over UDP. No CloudWatch Transaction Search is needed.
      tracing: lambda.Tracing.ACTIVE,
      environment: {
        AWS_LAMBDA_EXEC_WRAPPER: '/opt/otel-instrument',
        OTEL_SERVICE_NAME: 'grant-review-workflow',
        OTEL_AWS_APPLICATION_SIGNALS_ENABLED: 'false', // on by default; needs a broad managed policy
        // Lite mode: a small SDK sending spans over UDP to the X-Ray daemon, instrumenting botocore, requests and
        // urllib3 only. The full auto-instrumentation bootstrap took longer than Lambda's 10-second init limit.
        OTEL_AWS_LAMBDA_FAST_START: 'true',
        OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: 'false', // never copy prompts or answers into spans
      },
      // Every log line is a JSON object with the request ID; the handlers' summaries add their fields and trace ID.
      loggingFormat: lambda.LoggingFormat.JSON,
      applicationLogLevelV2: lambda.ApplicationLogLevel.INFO,
      systemLogLevelV2: lambda.SystemLogLevel.INFO, // Lambda's own START/REPORT records: duration, memory, init
    });
    Validations.of(role).acknowledge({
      id: 'AwsSolutions-IAM5[Resource::*]',
      reason: 'X-Ray trace writes (PutTraceSegments, PutTelemetryRecords) do not support resource-level permissions.',
    });
    Validations.of(role).acknowledge({
      id: `AwsSolutions-IAM5[Resource::<${cdk.Names.uniqueId(logGroup)}>:*]`,
      reason: 'Log streams in this function\'s own log group: stream names are created by Lambda.',
    });
    return fn;
  }
}
