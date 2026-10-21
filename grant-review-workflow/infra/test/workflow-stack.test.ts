import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { AwsSolutionsChecks } from 'cdk-nag';
import { DataStack } from '../lib/data-stack';
import { WorkflowStack, loadRuntime } from '../lib/workflow-stack';

const env = { account: '111122223333', region: 'us-east-2' };
const SONNET = 'us.anthropic.claude-sonnet-4-6';
const HAIKU = 'us.anthropic.claude-haiku-4-5-20251001-v1:0';

function synth() {
  const app = new cdk.App();
  const data = new DataStack(app, 'Data', { env });
  const stack = new WorkflowStack(app, 'Workflow', { env, table: data.table });
  Validations.of(app).addPlugins(new AwsSolutionsChecks(app)); // an unacknowledged finding throws here
  app.synth();
  return { stack, template: Template.fromStack(stack) };
}

const { stack, template } = synth();

/** Every IAM statement attached to the role of the function whose handler is `handler`. */
function statementsFor(handler: string): any[] {
  const fn = Object.values(template.findResources('AWS::Lambda::Function')).find(
    (f: any) => f.Properties.ImageConfig?.Command?.[0] === handler,
  ) as any;
  const roleId = fn.Properties.Role['Fn::GetAtt'][0];
  return Object.values(template.findResources('AWS::IAM::Policy'))
    .filter((p: any) => p.Properties.Roles.some((r: any) => r.Ref === roleId))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
}

const text = (statements: any[]) => JSON.stringify(statements);

test('both functions run the same image with their own handler', () => {
  template.resourceCountIs('AWS::Lambda::Function', 2);
  for (const handler of ['handlers.start', 'handlers.resume']) {
    template.hasResourceProperties('AWS::Lambda::Function', {
      PackageType: 'Image',
      ImageConfig: { Command: [handler] },
      Architectures: ['x86_64'],
      MemorySize: 1024,
    });
  }
});

test('the roles come from runtime.json, which the Python code writes', () => {
  const runtime = loadRuntime();
  expect(runtime.functions.start.inference_profiles).toEqual([HAIKU, SONNET]);
  expect(runtime.functions.resume.inference_profiles).toEqual([HAIKU]);
});

test('start may call both models, only through their inference profiles', () => {
  const statements = statementsFor('handlers.start');
  expect(text(statements)).toContain(`inference-profile/${SONNET}`);
  expect(text(statements)).toContain(`inference-profile/${HAIKU}`);
  const viaProfiles = statements.find((s) => s.Sid === 'InvokeTheirModelsOnlyThroughThoseProfiles');
  expect(viaProfiles.Condition.StringEquals['bedrock:InferenceProfileArn']).toHaveLength(2);
  expect(viaProfiles.Resource).toHaveLength(6); // 2 models x 3 Regions the us. profiles route to
});

test('resume can only call the letter writer\'s model and never touches the intake bucket', () => {
  const statements = statementsFor('handlers.resume');
  expect(text(statements)).toContain(`inference-profile/${HAIKU}`);
  expect(text(statements)).not.toContain('sonnet');
  expect(text(statements)).not.toContain('s3:');
});

test('start reads exactly the three packet files from the intake bucket', () => {
  const read = statementsFor('handlers.start').find((s) => s.Sid === 'ReadApplicationPackets');
  expect(read.Action).toBe('s3:GetObject');
  expect(read.Resource).toEqual(
    ['form.json', 'narrative.md', 'return.pdf'].map((f) => `arn:aws:s3:::grant-review-intake-111122223333/*/${f}`),
  );
});

test('checkpoint access is the saver\'s five calls on the table, nothing broader', () => {
  for (const handler of ['handlers.start', 'handlers.resume']) {
    const checkpoints = statementsFor(handler).find((s) => s.Sid === 'ReadAndWriteCheckpoints');
    expect(checkpoints.Action).toEqual(loadRuntime().checkpoint_actions);
    expect(text(statementsFor(handler))).not.toMatch(/dynamodb:\*|DeleteItem|Scan|DeleteTable/);
  }
});

test('no role uses an AWS managed policy', () => {
  for (const role of Object.values(template.findResources('AWS::IAM::Role')) as any[]) {
    expect(role.Properties.ManagedPolicyArns).toBeUndefined();
  }
});

test('an uploaded form.json in the intake bucket starts a run', () => {
  template.hasResourceProperties('AWS::Events::Rule', {
    EventPattern: {
      source: ['aws.s3'],
      'detail-type': ['Object Created'],
      detail: { bucket: { name: ['grant-review-intake-111122223333'] }, object: { key: [{ suffix: '/form.json' }] } },
    },
    Targets: [Match.objectLike({ RetryPolicy: { MaximumRetryAttempts: 2, MaximumEventAgeInSeconds: 3600 } })],
  });
});

test('a run that keeps failing goes to the failed-applications queue after two retries', () => {
  template.hasResourceProperties('AWS::Lambda::EventInvokeConfig', {
    MaximumRetryAttempts: 2,
    DestinationConfig: { OnFailure: { Destination: Match.anyValue() } },
  });
  template.hasResourceProperties('AWS::SQS::Queue', { MessageRetentionPeriod: 14 * 24 * 3600, SqsManagedSseEnabled: true });
});

test('logs are kept 30 days', () => {
  template.resourcePropertiesCountIs('AWS::Logs::LogGroup', { RetentionInDays: 30 }, 2);
});

test('every cdk-nag finding is fixed or acknowledged with a reason', () => {
  expect(stack).toBeDefined(); // synth() above throws on an unacknowledged finding
});

test('both functions are traced through the ADOT wrapper, with JSON logs and no prompt capture', () => {
  for (const handler of ['handlers.start', 'handlers.resume']) {
    template.hasResourceProperties('AWS::Lambda::Function', {
      ImageConfig: { Command: [handler] },
      TracingConfig: { Mode: 'Active' },
      LoggingConfig: Match.objectLike({ LogFormat: 'JSON', ApplicationLogLevel: 'INFO' }),
      Environment: {
        Variables: Match.objectLike({
          AWS_LAMBDA_EXEC_WRAPPER: '/opt/otel-instrument',
          OTEL_AWS_APPLICATION_SIGNALS_ENABLED: 'false',
          OTEL_AWS_LAMBDA_FAST_START: 'true',
          OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: 'false',
        }),
      },
    });
  }
});

test('the only wildcard resource is X-Ray, for exactly its two write actions', () => {
  for (const handler of ['handlers.start', 'handlers.resume']) {
    const wildcard = statementsFor(handler).filter((s) => s.Resource === '*');
    expect(wildcard).toHaveLength(1);
    expect([wildcard[0].Action].flat().sort()).toEqual(['xray:PutTelemetryRecords', 'xray:PutTraceSegments']);
  }
});
