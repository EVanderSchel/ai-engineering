import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { Construct } from 'constructs';
import { intakeBucketName } from './data-stack';
import { loadRuntime, workflowFunction, workflowImage } from './workflow-stack';

export const STATE_MACHINE = 'GrantReview-workflow-sfn';
export const REVIEWS_TABLE = 'grant-review-sfn-reviews';

/**
 * The same workflow orchestrated by AWS Step Functions instead of LangGraph (step 9), to compare the two on
 * evidence. Not in the pipeline, and deleted after the comparison; the code and results stay.
 *
 *   Intake -> Read (Parallel: extract | research | read_proposal) -> Join -> CrossCheck -> Eligibility -> Route
 *     Route: declined by the rules -> DraftLetter
 *            otherwise -> HumanReview (waits for a task token) -> ApplyReview
 *              decided -> DraftLetter;  corrections only -> Eligibility (the rules again)
 *
 * Every step is one Lambda function (src/sfn_steps.py) on the workflow's image, running the same step code as the
 * graph; the application's state travels between states as JSON (a few KB: the return PDF stays in S3). Step
 * Functions keeps each execution's state, so there are no checkpoints, and declares its retries per state. The
 * reviewer's wait writes the task token to a table of its own, so these executions never show up in the reviewer
 * UI's queue; the comparison harness (src/compare.py) answers them with SendTaskSuccess.
 *
 * A Standard workflow: Express can't wait for a task token and lasts 5 minutes at most. Its execution history
 * holds the applications' text (the dev packets: synthetic narratives) and goes when the stack is deleted.
 */
export class StepFunctionsStack extends cdk.Stack {
  readonly stateMachine: sfn.StateMachine;
  readonly reviews: dynamodb.Table;

  constructor(scope: Construct, id: string, props: cdk.StackProps) {
    super(scope, id, props);
    const runtime = loadRuntime();
    const bucket = intakeBucketName(this.account);

    // One function for every step: the start function's models (the graph's start runs every step too).
    const steps = workflowFunction(this, 'Steps', {
      handler: 'sfn_steps.handler',
      image: workflowImage('sfn_steps.handler'),
      profiles: runtime.functions.start.inference_profiles,
      runtime,
      timeout: cdk.Duration.minutes(5), // one step: seconds, plus Bedrock's own retries
    });
    const packetFiles = ['form.json', 'narrative.md', 'return.pdf'].map((f) => `arn:aws:s3:::${bucket}/*/${f}`);
    steps.role!.addToPrincipalPolicy(
      new iam.PolicyStatement({ sid: 'ReadApplicationPackets', actions: ['s3:GetObject'], resources: packetFiles }),
    );
    for (const arn of packetFiles) {
      Validations.of(steps.role!).acknowledge({
        id: `AwsSolutions-IAM5[Resource::${arn}]`,
        reason: 'Application IDs are not known in advance; reads are limited to the three packet files.',
      });
    }

    // Where a waiting execution leaves its task token for whoever decides (here: the comparison harness).
    this.reviews = new dynamodb.Table(this, 'Reviews', {
      tableName: REVIEWS_TABLE,
      partitionKey: { name: 'application_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.DESTROY, // comparison only: goes with the stack
    });
    Validations.of(this.reviews).acknowledge({
      id: 'AwsSolutions-DDB3',
      reason: 'Task tokens for the comparison only, deleted with the stack; nothing to recover.',
    });
    steps.addEnvironment('REVIEWS_TABLE', this.reviews.tableName);
    steps.role!.addToPrincipalPolicy(
      new iam.PolicyStatement({ sid: 'LeaveTaskTokens', actions: ['dynamodb:PutItem'], resources: [this.reviews.tableArn] }),
    );

    // A step: the function with {"step", "state", ...}; its result (the new state) becomes the state's output.
    const step = (
      name: string,
      payload: Record<string, unknown>,
      opts: { retryOutages?: boolean; assign?: Record<string, string> } = {},
    ) => {
      const task = tasks.LambdaInvoke.jsonata(this, name, {
        lambdaFunction: steps,
        payload: sfn.TaskInput.fromObject(payload),
        outputs: '{% $states.result.Payload %}',
        assign: opts.assign,
        taskTimeout: sfn.Timeout.duration(cdk.Duration.minutes(5)),
      });
      if (opts.retryOutages) {
        // A model step that still fails after Bedrock's own retries: wait 30 s, then 90 s, and run it again (the
        // graph's carry_on after an outage waits the same). Lambda's own service errors are retried by default.
        task.addRetry({ errors: ['States.TaskFailed'], interval: cdk.Duration.seconds(30), maxAttempts: 2, backoffRate: 3 });
      }
      return task;
    };
    const withState = (stepName: string) => ({ step: stepName, state: '{% $states.input %}', bucket: '{% $bucket %}' });

    // Variables last the whole execution: the bucket for every step (here), the pre-parallel state for Join (Read).
    const intake = step(
      'Intake',
      { step: 'intake', application_id: '{% $states.input.application_id %}', bucket: '{% $states.input.bucket %}' },
      { assign: { bucket: '{% $states.input.bucket %}' } },
    );

    const read = sfn.Parallel.jsonata(this, 'Read', {
      assign: { before: '{% $states.input %}' },
      outputs: '{% $states.result %}', // one state per branch, in branch order
    });
    for (const [name, stepName] of [
      ['Extract', 'extract'],
      ['Research', 'research'],
      ['ReadProposal', 'read_proposal'],
    ]) {
      read.branch(step(name, withState(stepName), { retryOutages: true }));
    }
    const join = step('Join', {
      step: 'join',
      state: '{% $before %}',
      branches: {
        extract: '{% $states.input[0] %}',
        research: '{% $states.input[1] %}',
        read_proposal: '{% $states.input[2] %}',
      },
    });
    const crossCheck = step('CrossCheck', withState('cross_check'));
    const eligibility = step('Eligibility', withState('eligibility'));
    const route = step('Route', withState('route'));
    const draftLetter = step('DraftLetter', withState('draft_letter'), { retryOutages: true });

    // Wait for the reviewer: the steps function leaves the task token in the reviews table, and the execution waits
    // until someone sends the decision with it (SendTaskSuccess); that decision is this state's output. A Lambda task
    // does it because Step Functions' DynamoDB integration can't wait for a task token.
    const humanReview = tasks.LambdaInvoke.jsonata(this, 'HumanReview', {
      lambdaFunction: steps,
      integrationPattern: sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
      payload: sfn.TaskInput.fromObject({
        step: 'await_review',
        application_id: '{% $states.input.application_id %}',
        task_token: '{% $states.context.Task.Token %}',
      }),
      assign: { pending: '{% $states.input %}' },
      outputs: '{% $states.result %}',
      taskTimeout: sfn.Timeout.duration(cdk.Duration.days(30)), // a decision within the data's 30-day lifetime
    });
    const applyReview = step('ApplyReview', {
      step: 'apply_review',
      state: '{% $pending %}',
      decision: '{% $states.input %}',
      bucket: '{% $bucket %}',
    });

    const routed = sfn.Choice.jsonata(this, 'Routed')
      .when(sfn.Condition.jsonata('{% $states.input.route = "decline_ineligible" %}'), draftLetter)
      .otherwise(humanReview.next(applyReview));
    applyReview.next(
      sfn.Choice.jsonata(this, 'Decided')
        .when(sfn.Condition.jsonata('{% $states.input.review.decision != null %}'), draftLetter)
        .otherwise(eligibility), // corrections only: the rules run again on the corrected figures
    );

    this.stateMachine = new sfn.StateMachine(this, 'Workflow', {
      stateMachineName: STATE_MACHINE,
      stateMachineType: sfn.StateMachineType.STANDARD,
      queryLanguage: sfn.QueryLanguage.JSONATA,
      definitionBody: sfn.DefinitionBody.fromChainable(
        intake.next(read).next(join).next(crossCheck).next(eligibility).next(route).next(routed),
      ),
      timeout: cdk.Duration.days(31),
      tracingEnabled: true,
      comment: 'grant-review-workflow on Step Functions (step 9 comparison)',
    });
    Validations.of(this.stateMachine).acknowledge({
      id: 'AwsSolutions-SF1',
      reason:
        "No CloudWatch execution logging: it would copy each application's text into another log group. The " +
        'execution history (kept with the state machine, deleted with it) and X-Ray traces cover the comparison.',
    });
    // cdk-nag names the function by its CloudFormation logical ID.
    const stepsId = this.getLogicalId(steps.node.defaultChild as cdk.CfnElement);
    Validations.of(this.stateMachine).acknowledge({
      id: `AwsSolutions-IAM5[Resource::<${stepsId}.Arn>:*]`,
      reason: "CDK's grant for a Lambda task: the steps function and its versions, the only function it can invoke.",
    });
    Validations.of(this.stateMachine).acknowledge({
      id: 'AwsSolutions-IAM5[Resource::*]',
      reason: 'X-Ray trace writes (tracing on) do not support resource-level permissions.',
    });

    new cdk.CfnOutput(this, 'StateMachineArn', { value: this.stateMachine.stateMachineArn });
    new cdk.CfnOutput(this, 'ReviewsTable', { value: this.reviews.tableName });
  }
}
