import * as cdk from 'aws-cdk-lib/core';
import * as iam from 'aws-cdk-lib/aws-iam';
import { AccessStack, BOUNDARY_POLICY, PROJECT_TAG } from './access-stack';
import { AuthStack } from './auth-stack';
import { DataStack } from './data-stack';
import { PipelineStack } from './pipeline-stack';
import { WorkflowStack } from './workflow-stack';

export const ACCESS_STACK = 'GrantReviewCdkAccess';

/**
 * Every stack of the project in one app: used by bin/infra.ts and by the tests, so both build exactly the same
 * stacks with the same permissions boundary.
 */
export function buildApp(app: cdk.App, env: cdk.Environment): void {
  const adopting = app.node.tryGetContext('adopt') === 'true';
  const data = new DataStack(app, 'GrantReviewData', {
    env,
    adopting,
    terminationProtection: true, // the stack holds state: deleting it takes a deliberate extra step
    description: 'grant-review-workflow: checkpoint table and intake bucket',
  });

  // Who may review (step 8): the reviewers' user pool and the UI's sign-in. Deployed by hand from the branch in
  // stage 4; the pipeline takes it over in stage 5, with the UI's own address added to the callback URLs.
  const auth = new AuthStack(app, 'GrantReviewAuth', {
    env,
    domainPrefix: 'prairie-commons-review',
    callbackUrls: ['http://localhost:5174/'], // `npm run dev:api`
    terminationProtection: true,
    description: 'grant-review-workflow: reviewer accounts and the UI sign-in',
  });

  if (!adopting) {
    // The code that uses the data; replaceable at any time, unlike the data stack.
    const workflow = new WorkflowStack(app, 'GrantReviewWorkflow', {
      env,
      table: data.table,
      applications: data.applications!,
      reviewers: { userPool: auth.userPool, client: auth.client },
      description:
        'grant-review-workflow: start, resume and reviewer API functions, the API, upload rule, failed-applications queue',
    });
    workflow.addStackDependency(data); // the bucket is named, not referenced, so state the order explicitly
  }


  // Deploys the two stacks above on merge to main, after a person approves the diff (stage 6). Deployed once from a
  // laptop; it doesn't deploy itself.
  new PipelineStack(app, 'GrantReviewPipeline', {
    env,
    owner: 'EVanderSchel',
    repo: 'ai-engineering',
    description: 'grant-review-workflow: GitHub connection and the diff, approve, deploy pipeline',
  });

  // What deploys may do (stage 6, part 2): the CDK execution policy and the permissions boundary. Deployed by a
  // person with their own credentials, first, and never by the pipeline (which deploys only data and workflow).
  new AccessStack(app, ACCESS_STACK, {
    env,
    description: 'grant-review-workflow: CDK execution policy and the permissions boundary for its roles',
  });

  // Every role in the deployable stacks carries the boundary; the execution policy refuses to create one without it.
  for (const stack of app.node.children) {
    if (stack instanceof cdk.Stack && stack.stackName !== ACCESS_STACK) {
      iam.PermissionsBoundary.of(stack).apply(
        iam.ManagedPolicy.fromManagedPolicyName(stack, 'WorkloadBoundary', BOUNDARY_POLICY),
      );
    }
  }
  cdk.Tags.of(app).add(PROJECT_TAG.key, PROJECT_TAG.value); // the execution policy scopes user pools by it
}
