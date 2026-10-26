import { Validations } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AwsSolutionsChecks } from 'cdk-nag';
import { testApp } from './app';
import { REVIEWS_TABLE, STATE_MACHINE, StepFunctionsStack } from '../lib/step-functions-stack';

const env = { account: '111122223333', region: 'us-east-2' };
const app = testApp();
const stack = new StepFunctionsStack(app, 'Sfn', { env });
Validations.of(app).addPlugins(new AwsSolutionsChecks(app)); // an unacknowledged finding throws here
app.synth();
const template = Template.fromStack(stack);

/** The definition as JSON, with CloudFormation references replaced by a placeholder string. */
const definition = (() => {
  const sm = (Object.values(template.findResources('AWS::StepFunctions::StateMachine'))[0] as any).Properties;
  const parts = sm.DefinitionString['Fn::Join'][1] as any[];
  return JSON.parse(parts.map((p) => (typeof p === 'string' ? p : 'REF')).join(''));
})();
const states = definition.States;
const branchStates = states.Read.Branches.map((b: any) => Object.entries(b.States)[0]);
const all: Record<string, any> = { ...states, ...Object.fromEntries(branchStates) };

test('a Standard JSONata state machine, named for the execution policy', () => {
  template.hasResourceProperties('AWS::StepFunctions::StateMachine', {
    StateMachineName: STATE_MACHINE,
    StateMachineType: 'STANDARD',
    TracingConfiguration: { Enabled: true },
  });
  expect(definition.QueryLanguage).toBe('JSONata');
});

test('the path mirrors the graph: intake, the parallel reading stage, the checks, then the route', () => {
  const next = (name: string) => states[name].Next;
  expect([definition.StartAt, next('Intake'), next('Read'), next('Join'), next('CrossCheck'), next('Eligibility'), next('Route')])
    .toEqual(['Intake', 'Read', 'Join', 'CrossCheck', 'Eligibility', 'Route', 'Routed']);
  expect(branchStates.map(([name]: any) => name)).toEqual(['Extract', 'Research', 'ReadProposal']);
  // Join gets the state from before the Parallel state and the three branches' states, in branch order.
  expect(states.Read.Assign).toEqual({ before: '{% $states.input %}' });
  expect(states.Join.Arguments.Payload.branches).toEqual({
    extract: '{% $states.input[0] %}',
    research: '{% $states.input[1] %}',
    read_proposal: '{% $states.input[2] %}',
  });
});

test('declined by the rules goes to the letter; everything else waits for a reviewer', () => {
  expect(states.Routed.Choices).toEqual([
    { Condition: '{% $states.input.route = "decline_ineligible" %}', Next: 'DraftLetter' },
  ]);
  expect(states.Routed.Default).toBe('HumanReview');
});

test('the reviewer wait leaves its task token in the reviews table, for at most 30 days', () => {
  const wait = states.HumanReview;
  // Waits for SendTaskSuccess (a plain lambda:invoke or dynamodb:putItem would carry on at once).
  expect(wait.Resource).toContain(':states:::lambda:invoke.waitForTaskToken');
  expect(wait.Arguments.Payload).toEqual({
    step: 'await_review',
    application_id: '{% $states.input.application_id %}',
    task_token: '{% $states.context.Task.Token %}',
  });
  const policies = JSON.stringify(template.findResources('AWS::IAM::Policy'));
  expect(policies).toContain('LeaveTaskTokens');
  expect(wait.TimeoutSeconds).toBe(30 * 24 * 3600);
  expect(wait.Assign).toEqual({ pending: '{% $states.input %}' });
  expect(states.ApplyReview.Arguments.Payload).toMatchObject({ step: 'apply_review', state: '{% $pending %}' });
  template.hasResourceProperties('AWS::DynamoDB::Table', { TableName: REVIEWS_TABLE });
});

test('a decision goes to the letter; corrections alone go back through the rules', () => {
  expect(states.Decided.Choices).toEqual([
    { Condition: '{% $states.input.review.decision != null %}', Next: 'DraftLetter' },
  ]);
  expect(states.Decided.Default).toBe('Eligibility');
  expect(states.DraftLetter.End).toBe(true);
});

test('only the model steps retry an outage, after 30 s then 90 s, as the graph\'s carry_on waits', () => {
  const outageRetry = (s: any) => (s.Retry ?? []).find((r: any) => r.ErrorEquals.includes('States.TaskFailed'));
  const withOutageRetry = Object.entries(all).filter(([, s]) => outageRetry(s)).map(([name]) => name).sort();
  expect(withOutageRetry).toEqual(['DraftLetter', 'Extract', 'ReadProposal', 'Research']);
  expect(outageRetry(all.Extract)).toMatchObject({ IntervalSeconds: 30, MaxAttempts: 2, BackoffRate: 3 });
});

test('every step is the one steps function, with the start function\'s models and no checkpoint table', () => {
  template.resourceCountIs('AWS::Lambda::Function', 1);
  template.hasResourceProperties('AWS::Lambda::Function', { ImageConfig: { Command: ['sfn_steps.handler'] } });
  const policies = JSON.stringify(template.findResources('AWS::IAM::Policy'));
  expect(policies).toContain('us.anthropic.claude-sonnet-4-6');
  expect(policies).not.toContain('grant-review-checkpoints');
  expect(policies).not.toContain('grant-review-applications'); // never in the reviewer UI's queue
});
