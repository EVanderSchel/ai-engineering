import * as path from 'node:path';
import * as fs from 'node:fs';
import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as apigw from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpUserPoolAuthorizer } from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as destinations from 'aws-cdk-lib/aws-lambda-destinations';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import { intakeBucketName } from './data-stack';

/** What each function's role needs, written by `src/iam_policy.py --export` from the workflow's own code. */
interface Runtime {
  region: string;
  profile_regions: string[];
  checkpoint_table: string;
  checkpoint_actions: string[];
  checkpoint_read_actions: string[];
  applications_table: string;
  applications_index: string;
  workflow_record_actions: string[];
  api_record_actions: string[];
  functions: Record<'start' | 'resume', { inference_profiles: string[] }>;
}

export function loadRuntime(file = path.join(__dirname, '..', 'runtime.json')): Runtime {
  return JSON.parse(fs.readFileSync(file, 'utf8')) as Runtime;
}

/** The workflow's container image (../Dockerfile) with one handler as its command: one image, many functions. */
export function workflowImage(handler: string): lambda.DockerImageCode {
  // Built from the project folder; .dockerignore keeps everything but pyproject, uv.lock, src/ and prompts/ out.
  return lambda.DockerImageCode.fromImageAsset(path.join(__dirname, '..', '..'), {
    cmd: [handler],
    platform: ecrAssets.Platform.LINUX_AMD64,
  });
}

export interface WorkflowStackProps extends cdk.StackProps {
  readonly table: dynamodb.ITable;
  /** Where each application stands (step 8): the workflow writes it, the reviewer API reads and claims it. */
  readonly applications: dynamodb.ITable;
  /** Who may call the reviewer API: the reviewers' user pool and the UI's app client (GrantReviewAuth). */
  readonly reviewers: { userPool: cognito.IUserPool; client: cognito.IUserPoolClient };
  readonly runtime?: Runtime;
}

/** The alerts topic (owned here): alarms and the pipeline's approval requests go to it; people subscribe by hand. */
export const ALERTS_TOPIC = 'GrantReview-alerts';

/** The reviewer API's routes: what src/api.py's handler routes on (its route keys). */
export const API_ROUTES: [apigw.HttpMethod, string][] = [
  [apigw.HttpMethod.GET, '/applications'],
  [apigw.HttpMethod.GET, '/applications/{application_id}'],
  [apigw.HttpMethod.POST, '/applications/{application_id}/decision'],
];

/**
 * The workflow on Lambda (step 7): `start` runs an application when its form.json lands in the intake bucket,
 * `resume` continues it with the reviewer's decision. Both run the same container image (../Dockerfile) with a
 * different handler, and each has its own role built from runtime.json: the step 6d policy, split by function.
 *
 * The reviewer API (step 8) runs on the same image too: an HTTP API whose every route needs a reviewer's access
 * token (API Gateway's JWT authorizer, for the GrantReviewAuth user pool), in front of a third function that reads
 * applications and hands decisions to `resume`.
 */
export class WorkflowStack extends cdk.Stack {
  readonly start: lambda.Function;
  readonly resume: lambda.Function;
  readonly api: lambda.Function;
  readonly httpApi: apigw.HttpApi;
  readonly failed: sqs.Queue;
  readonly alerts: sns.Topic;

  constructor(scope: Construct, id: string, props: WorkflowStackProps) {
    super(scope, id, props);
    const runtime = props.runtime ?? loadRuntime();


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

    this.start = workflowFunction(this, 'Start', {
      handler: 'handlers.start',
      image: workflowImage('handlers.start'),
      profiles: runtime.functions.start.inference_profiles,
      runtime,
      checkpoints: { table: props.table, actions: runtime.checkpoint_actions },
      records: { table: props.applications, actions: runtime.workflow_record_actions },
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

    this.resume = workflowFunction(this, 'Resume', {
      handler: 'handlers.resume',
      image: workflowImage('handlers.resume'),
      profiles: runtime.functions.resume.inference_profiles,
      runtime,
      checkpoints: { table: props.table, actions: runtime.checkpoint_actions },
      records: { table: props.applications, actions: runtime.workflow_record_actions },
      timeout: cdk.Duration.minutes(2), // eligibility, route and a letter: seconds
    });

    // --- The reviewer API (step 8) -----------------------------------------------------------------------------

    // No model calls: reads checkpoints (never writes them), reads and claims status records, runs `resume`.
    this.api = workflowFunction(this, 'Api', {
      handler: 'api.handler',
      image: workflowImage('api.handler'),
      profiles: [],
      runtime,
      checkpoints: { table: props.table, actions: runtime.checkpoint_read_actions },
      records: { table: props.applications, actions: runtime.api_record_actions, index: runtime.applications_index },
      timeout: cdk.Duration.seconds(29), // API Gateway's own limit for an HTTP API integration
      environment: { RESUME_FUNCTION: this.resume.functionName },
    });
    // The function itself, not its versions or aliases (grantInvoke would add "<arn>:*").
    this.api.role!.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'RunResume',
        actions: ['lambda:InvokeFunction'],
        resources: [this.resume.functionArn],
      }),
    );

    const accessLogs = new logs.LogGroup(this, 'ApiAccessLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.httpApi = new apigw.HttpApi(this, 'ReviewerApi', {
      apiName: 'grant-review-reviewer-api',
      description: 'grant-review-workflow: the reviewer UI\'s API (every route needs a reviewer\'s access token)',
      createDefaultStage: false, // the stage below, with throttling and access logs
    });
    new apigw.HttpStage(this, 'ReviewerApiStage', {
      httpApi: this.httpApi,
      stageName: '$default',
      autoDeploy: true,
      throttle: { rateLimit: 10, burstLimit: 20 }, // a few reviewers: anything more is a mistake or an attack
      accessLogSettings: {
        destination: new apigw.LogGroupLogDestination(accessLogs),
        // Who called what and how it went: the reviewer's username, never the request or response bodies.
        format: apigateway.AccessLogFormat.custom(
          JSON.stringify({
            requestId: '$context.requestId',
            time: '$context.requestTime',
            route: '$context.routeKey',
            status: '$context.status',
            latencyMs: '$context.responseLatency',
            reviewer: '$context.authorizer.claims.username',
            authorizerError: '$context.authorizer.error',
            integrationError: '$context.integrationErrorMessage',
            sourceIp: '$context.identity.sourceIp',
          }),
        ),
      },
    });
    // API Gateway checks the access token (signature, issuer, the UI's app client, expiry) before the function
    // runs; src/api.py then checks the reviewers group, which the authorizer can't.
    const authorizer = new HttpUserPoolAuthorizer('Reviewers', props.reviewers.userPool, {
      userPoolClients: [props.reviewers.client],
      authorizerName: 'reviewers',
    });
    const integration = new HttpLambdaIntegration('ApiHandler', this.api);
    for (const [method, path] of API_ROUTES) {
      this.httpApi.addRoutes({ path, methods: [method], integration, authorizer });
    }

    // A decision taken but not finished (src/api.py logs "resume_failed") emails whoever subscribes to this topic.
    // The subscription is made by hand, so the address isn't in the code.
    this.alerts = new sns.Topic(this, 'Alerts', {
      topicName: ALERTS_TOPIC,
      displayName: 'Grant review alerts',
      enforceSSL: true,
    });
    Validations.of(this.alerts).acknowledge({
      id: 'AwsSolutions-SNS2',
      reason:
        'Alarm notifications carry an alarm name and an application ID, no applicant data; CloudWatch can only ' +
        'publish to an encrypted topic through a customer-managed KMS key ($1 a month).',
    });
    const resumeFailed = new logs.MetricFilter(this, 'ResumeFailedMetric', {
      logGroup: this.api.logGroup,
      filterPattern: logs.FilterPattern.stringValue('$.event', '=', 'resume_failed'),
      metricNamespace: 'GrantReview',
      metricName: 'ResumeFailed',
      metricValue: '1',
    });
    const alarm = new cloudwatch.Alarm(this, 'ResumeFailedAlarm', {
      alarmName: 'GrantReview-decision-not-finished',
      alarmDescription:
        'A reviewer\'s decision was taken but its letter wasn\'t drafted. Submitting a decision for the application ' +
        'again (after 5 minutes) finishes the run; see the API function\'s logs for the application ID.',
      metric: resumeFailed.metric({ statistic: 'Sum', period: cdk.Duration.minutes(5) }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarm.addAlarmAction(new cwActions.SnsAction(this.alerts));

    new cdk.CfnOutput(this, 'ReviewerApiUrl', { value: this.httpApi.apiEndpoint });
    new cdk.CfnOutput(this, 'AlertsTopicArn', { value: this.alerts.topicArn });
    new cdk.CfnOutput(this, 'StartFunctionName', { value: this.start.functionName });
    new cdk.CfnOutput(this, 'ResumeFunctionName', { value: this.resume.functionName });
    new cdk.CfnOutput(this, 'FailedApplicationsQueue', { value: this.failed.queueUrl });
  }
}

/** A function on the shared image with its own log group and a role holding only what runtime.json lists. */
export function workflowFunction(
  scope: Construct,
  id: string,
  opts: {
    handler: string;
    image: lambda.DockerImageCode;
    profiles: string[]; // none: the function calls no model
    runtime: Runtime;
    checkpoints?: { table: dynamodb.ITable; actions: string[] }; // none: the function keeps no checkpoints
    records?: { table: dynamodb.ITable; actions: string[]; index?: string }; // none: it writes no status records
    timeout: cdk.Duration;
    environment?: Record<string, string>;
  },
): lambda.Function {
  const stack = cdk.Stack.of(scope);
  const logGroup = new logs.LogGroup(scope, `${id}Logs`, {
    retention: logs.RetentionDays.ONE_MONTH, // the same 30 days as the checkpoints
    removalPolicy: cdk.RemovalPolicy.DESTROY,
  });

  // No AWS managed policies (AWSLambdaBasicExecutionRole allows logging to any group): everything is listed.
  const role = new iam.Role(scope, `${id}Role`, {
    assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
    description: `grant-review-workflow ${opts.handler}: its Bedrock profiles, the checkpoint table, its logs`,
  });
  logGroup.grantWrite(role);

  if (opts.profiles.length > 0) {
    const profileArns = opts.profiles.map(
      (p) => `arn:aws:bedrock:${stack.region}:${stack.account}:inference-profile/${p}`,
    );
    const modelArns = opts.profiles.flatMap((p) =>
      opts.runtime.profile_regions.map((r) => `arn:aws:bedrock:${r}::foundation-model/${p.replace(/^us\./, '')}`),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeItsInferenceProfiles',
        actions: ['bedrock:InvokeModel'],
        resources: profileArns,
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeTheirModelsOnlyThroughThoseProfiles',
        actions: ['bedrock:InvokeModel'],
        resources: modelArns,
        conditions: { StringEquals: { 'bedrock:InferenceProfileArn': profileArns } },
      }),
    );
  }
  if (opts.checkpoints) {
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'Checkpoints',
        actions: opts.checkpoints.actions,
        resources: [opts.checkpoints.table.tableArn],
      }),
    );
  }
  if (opts.records) {
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: 'StatusRecords',
        actions: opts.records.actions,
        resources: [
          opts.records.table.tableArn,
          ...(opts.records.index ? [`${opts.records.table.tableArn}/index/${opts.records.index}`] : []),
        ],
      }),
    );
  }

  const fn = new lambda.DockerImageFunction(scope, id, {
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
      ...opts.environment,
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
