import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

/** Names the other stacks and the bootstrap refer to: fixed, so nothing has to look them up. */
export const BOUNDARY_POLICY = 'grant-review-workload-boundary';
export const EXECUTION_POLICY = 'grant-review-cdk-execution';
/** The reviewer UI's pieces of the execution policy (step 8): one managed policy may hold 6,144 characters at most. */
export const EXECUTION_WEB_POLICY = 'grant-review-cdk-execution-web';
/** Statements in the web half: everything the reviewer UI added (Cognito, API Gateway, CloudFront, alerts). */
const WEB_STATEMENTS = new Set([
  'CreateOurUserPool',
  'OurUserPools',
  'CreateOurHttpApi',
  'OurHttpApis',
  'ApiAccessLogDelivery',
  'OurCloudFront',
  'CreateCloudFrontPieces',
  'OurParameters',
  'OurAlarmTopic',
  'OurAlarms',
  'NeverManageReviewerAccounts',
]);
/** The tag every resource of the project carries (app.ts). Resources with generated IDs are scoped by it. */
export const PROJECT_TAG = { key: 'project', value: 'grant-review-workflow' };
const QUALIFIER = 'hnb659fds'; // cdk bootstrap's default

/**
 * Narrows what a deploy can do (step 7, stage 6, part 2). The CDK bootstrap gave CloudFormation's execution role
 * AdministratorAccess; with the pipeline deploying on merge, that meant anything that reached main could create
 * anything in the account. Two managed policies replace it:
 *
 * - the execution policy (`cdk bootstrap --cloudformation-execution-policies`): what CloudFormation may do,
 *   taken from CloudTrail's record of every call the execution role made in our deploys (plus the deletes a
 *   replacement needs), limited to this project's resource names;
 * - the permissions boundary: the most any role our stacks create may ever do. The execution policy may create or
 *   change a role only with this boundary attached, so it can't mint a role stronger than the boundary.
 *
 * Both are protected by explicit denies, and this stack is deployed with a person's own credentials
 * (CliCredentialsStackSynthesizer), never through the bootstrap roles: the pipeline can't loosen its own limits.
 */
export class AccessStack extends cdk.Stack {
  readonly boundary: iam.ManagedPolicy;
  readonly execution: iam.ManagedPolicy;
  readonly executionWeb: iam.ManagedPolicy;

  constructor(scope: Construct, id: string, props: cdk.StackProps) {
    super(scope, id, { ...props, synthesizer: new cdk.CliCredentialsStackSynthesizer() });
    const a = this.account;
    const r = this.region;
    const arn = (service: string, resource: string, region = r) => `arn:aws:${service}:${region}:${a}:${resource}`;
    const ourRoles = `arn:aws:iam::${a}:role/GrantReview*`;
    const boundaryArn = `arn:aws:iam::${a}:policy/${BOUNDARY_POLICY}`;
    const executionArn = `arn:aws:iam::${a}:policy/${EXECUTION_POLICY}`;
    const executionWebArn = `arn:aws:iam::${a}:policy/${EXECUTION_WEB_POLICY}`;
    const ourBuckets = ['arn:aws:s3:::grant-review-*', 'arn:aws:s3:::grantreview*'];

    this.boundary = new iam.ManagedPolicy(this, 'WorkloadBoundary', {
      managedPolicyName: BOUNDARY_POLICY,
      description: 'The most any grant-review-workflow role may do: a cap, not a grant',
      statements: [
        new iam.PolicyStatement({
          sid: 'InvokeClaudeOnBedrock',
          actions: ['bedrock:InvokeModel'],
          resources: [arn('bedrock', 'inference-profile/*'), 'arn:aws:bedrock:*::foundation-model/anthropic.*'],
        }),
        new iam.PolicyStatement({
          sid: 'Checkpoints', // and status records (step 8): UpdateItem is the reviewer UI's claim
          actions: [
            'dynamodb:GetItem',
            'dynamodb:PutItem',
            'dynamodb:UpdateItem',
            'dynamodb:Query',
            'dynamodb:BatchGetItem',
            'dynamodb:BatchWriteItem',
          ],
          resources: [arn('dynamodb', 'table/grant-review-*')], // indexes too: table/<name>/index/<index>
        }),
        new iam.PolicyStatement({
          sid: 'InvokeOurFunctions', // the reviewer UI's API runs the resume function
          actions: ['lambda:InvokeFunction'],
          resources: [arn('lambda', 'function:GrantReview*')],
        }),
        new iam.PolicyStatement({
          sid: 'OurBuckets',
          actions: ['s3:GetObject*', 's3:PutObject*', 's3:DeleteObject*', 's3:Abort*', 's3:GetBucket*', 's3:List*'],
          resources: ourBuckets.flatMap((b) => [b, `${b}/*`]),
        }),
        new iam.PolicyStatement({
          sid: 'OurLogs',
          actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [arn('logs', 'log-group:GrantReview*'), arn('logs', 'log-group:/aws/codebuild/grant-review-*')],
        }),
        new iam.PolicyStatement({
          sid: 'Traces', // X-Ray writes don't support resource-level permissions
          actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords'],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          sid: 'FailedApplicationsQueue',
          actions: ['sqs:SendMessage', 'sqs:GetQueueAttributes', 'sqs:GetQueueUrl'],
          resources: [arn('sqs', 'GrantReview*')],
        }),
        new iam.PolicyStatement({
          sid: 'PipelineAndBuilds',
          actions: [
            'codebuild:StartBuild',
            'codebuild:BatchGetBuilds',
            'codebuild:StopBuild',
            'codebuild:CreateReport',
            'codebuild:CreateReportGroup',
            'codebuild:UpdateReport',
            'codebuild:BatchPutTestCases',
            'codebuild:BatchPutCodeCoverages',
            'codestar-connections:UseConnection',
            'codeconnections:UseConnection',
          ],
          resources: [
            arn('codebuild', 'project/grant-review-*'),
            arn('codebuild', 'report-group/grant-review-*'),
            arn('codestar-connections', 'connection/*'),
            arn('codeconnections', 'connection/*'),
          ],
        }),
        new iam.PolicyStatement({
          sid: 'AssumeBootstrapAndOurRoles', // the builds assume CDK's roles; the pipeline its action roles
          actions: ['sts:AssumeRole'],
          resources: [`arn:aws:iam::${a}:role/cdk-${QUALIFIER}-*`, ourRoles],
        }),
      ],
    });

    // What CloudFormation may do, as one list; it's split into two managed policies below only because IAM limits a
    // managed policy to 6,144 characters. The role gets both (cdk bootstrap --cloudformation-execution-policies).
    const executionStatements: iam.PolicyStatement[] = [
        // --- What CloudFormation creates and changes, by our names (from CloudTrail, plus deletes) ---
        new iam.PolicyStatement({
          sid: 'CheckpointTable',
          actions: ['dynamodb:*'],
          resources: [arn('dynamodb', 'table/grant-review-*')],
        }),
        new iam.PolicyStatement({ sid: 'OurBuckets', actions: ['s3:*'], resources: ourBuckets }),
        new iam.PolicyStatement({
          sid: 'OurFunctions',
          actions: ['lambda:*'],
          resources: [arn('lambda', 'function:GrantReview*'), arn('lambda', 'function:GrantReview*:*')],
        }),
        new iam.PolicyStatement({
          sid: 'OurLogGroups',
          actions: ['logs:*'],
          resources: [arn('logs', 'log-group:GrantReview*'), arn('logs', 'log-group:GrantReview*:*')],
        }),
        new iam.PolicyStatement({ sid: 'OurQueue', actions: ['sqs:*'], resources: [arn('sqs', 'GrantReview*')] }),
        new iam.PolicyStatement({ sid: 'OurRules', actions: ['events:*'], resources: [arn('events', 'rule/GrantReview*')] }),
        new iam.PolicyStatement({
          sid: 'OurPipeline',
          actions: ['codebuild:*', 'codepipeline:*'],
          resources: [arn('codebuild', 'project/grant-review-*'), arn('codepipeline', 'grant-review-*')],
        }),
        new iam.PolicyStatement({
          sid: 'GitHubConnection',
          actions: ['codeconnections:*', 'codestar-connections:*'],
          resources: [arn('codeconnections', 'connection/*'), arn('codestar-connections', 'connection/*')],
        }),
        // User pools get generated IDs (us-east-2_AbC123), so they're scoped by the project tag instead of a name:
        // a pool may only be created tagged as ours, and only pools tagged as ours may be changed.
        new iam.PolicyStatement({
          sid: 'CreateOurUserPool', // CreateUserPool has no resource to scope to before the pool exists
          actions: ['cognito-idp:CreateUserPool'],
          resources: ['*'],
          conditions: { StringEquals: { [`aws:RequestTag/${PROJECT_TAG.key}`]: PROJECT_TAG.value } },
        }),
        new iam.PolicyStatement({
          sid: 'OurUserPools',
          actions: ['cognito-idp:*'],
          resources: [arn('cognito-idp', 'userpool/*')],
          conditions: { StringEquals: { [`aws:ResourceTag/${PROJECT_TAG.key}`]: PROJECT_TAG.value } },
        }),
        // HTTP APIs get generated IDs too: created only tagged as ours, changed only when tagged as ours. API Gateway
        // applies an API's tags to its routes, integrations, stages and authorizers when checking permissions.
        new iam.PolicyStatement({
          sid: 'CreateOurHttpApi',
          actions: ['apigateway:POST'],
          resources: [`arn:aws:apigateway:${r}::/apis`],
          conditions: { StringEquals: { [`aws:RequestTag/${PROJECT_TAG.key}`]: PROJECT_TAG.value } },
        }),
        new iam.PolicyStatement({
          sid: 'OurHttpApis',
          actions: ['apigateway:*'],
          resources: [`arn:aws:apigateway:${r}::/apis/*`, `arn:aws:apigateway:${r}::/tags/*`],
          conditions: { StringEquals: { [`aws:ResourceTag/${PROJECT_TAG.key}`]: PROJECT_TAG.value } },
        }),
        new iam.PolicyStatement({
          sid: 'ApiAccessLogDelivery', // HTTP API access logs: CloudWatch Logs' delivery calls have no resource
          actions: [
            'logs:CreateLogDelivery',
            'logs:GetLogDelivery',
            'logs:UpdateLogDelivery',
            'logs:DeleteLogDelivery',
            'logs:ListLogDeliveries',
            'logs:PutResourcePolicy',
          ],
          resources: ['*'],
        }),
        // CloudFront (the reviewer UI, step 8): distributions, cache and header policies and origin access controls get
        // generated IDs, and CloudFront's tag conditions don't cover them all, so these are scoped to the resource
        // types in this account (it has no other CloudFront resources). The function is scoped by its name.
        new iam.PolicyStatement({
          sid: 'OurCloudFront',
          actions: ['cloudfront:*'],
          resources: [
            `arn:aws:cloudfront::${a}:distribution/*`,
            `arn:aws:cloudfront::${a}:origin-access-control/*`,
            `arn:aws:cloudfront::${a}:cache-policy/*`,
            `arn:aws:cloudfront::${a}:origin-request-policy/*`,
            `arn:aws:cloudfront::${a}:response-headers-policy/*`,
            `arn:aws:cloudfront::${a}:function/GrantReview*`,
          ],
        }),
        new iam.PolicyStatement({
          // Creating these has no resource to scope to (CloudFront supports no resource-level permissions for these
          // creates); everything done to them afterwards is limited by OurCloudFront.
          sid: 'CreateCloudFrontPieces',
          actions: [
            'cloudfront:CreateCachePolicy',
            'cloudfront:CreateOriginAccessControl',
            'cloudfront:CreateResponseHeadersPolicy',
            'cloudfront:CreateFunction',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          sid: 'OurParameters', // the UI's address, for the auth stack's callback URLs
          actions: ['ssm:*'],
          resources: [arn('ssm', 'parameter/grant-review/*')],
        }),
        new iam.PolicyStatement({ sid: 'OurAlarmTopic', actions: ['sns:*'], resources: [arn('sns', 'GrantReview*')] }),
        new iam.PolicyStatement({
          sid: 'OurAlarms',
          actions: [
            'cloudwatch:PutMetricAlarm',
            'cloudwatch:DeleteAlarms',
            'cloudwatch:DescribeAlarms',
            'cloudwatch:TagResource',
            'cloudwatch:UntagResource',
            'cloudwatch:ListTagsForResource',
          ],
          resources: [arn('cloudwatch', 'alarm:GrantReview*')],
        }),
        new iam.PolicyStatement({
          sid: 'PullTheLambdaImage', // CreateFunction checks the caller can read the image
          actions: ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'],
          resources: [arn('ecr', `repository/cdk-${QUALIFIER}-container-assets-${a}-${r}`)],
        }),
        new iam.PolicyStatement({
          sid: 'BootstrapVersion',
          actions: ['ssm:GetParameters'],
          resources: [arn('ssm', `parameter/cdk-bootstrap/${QUALIFIER}/version`)],
        }),
        new iam.PolicyStatement({
          sid: 'ReadStackOutputs', // Fn::GetStackOutput between our stacks
          actions: ['cloudformation:DescribeStacks'],
          resources: [arn('cloudformation', 'stack/GrantReview*/*')],
        }),
        new iam.PolicyStatement({
          sid: 'EncryptionThroughServices', // Lambda environment variables, S3 and SSM use AWS-managed keys
          actions: ['kms:Decrypt', 'kms:Encrypt', 'kms:GenerateDataKey', 'kms:DescribeKey'],
          resources: ['*'],
          conditions: {
            StringEquals: {
              'kms:ViaService': [`lambda.${r}.amazonaws.com`, `s3.${r}.amazonaws.com`, `ssm.${r}.amazonaws.com`],
            },
          },
        }),
        new iam.PolicyStatement({
          sid: 'AccountLevelReads', // these calls have no resource to scope to
          actions: [
            'lambda:GetAccountSettings',
            'logs:DescribeLogGroups',
            'logs:DescribeResourcePolicies',
            'logs:DescribeIndexPolicies',
            'iam:GetAccountSummary',
            'cognito-idp:DescribeUserPoolDomain', // looked up by domain name, not by pool
          ],
          resources: ['*'],
        }),

        // --- IAM: our roles only, and only with the boundary attached ---
        new iam.PolicyStatement({
          sid: 'ChangeOurRolesOnlyWithTheBoundary',
          actions: [
            'iam:CreateRole',
            'iam:PutRolePolicy',
            'iam:DeleteRolePolicy',
            'iam:AttachRolePolicy',
            'iam:DetachRolePolicy',
            'iam:PutRolePermissionsBoundary',
          ],
          resources: [ourRoles],
          conditions: { StringEquals: { 'iam:PermissionsBoundary': boundaryArn } },
        }),
        new iam.PolicyStatement({
          sid: 'ManageOurRoles',
          actions: [
            'iam:GetRole',
            'iam:GetRolePolicy',
            'iam:ListRolePolicies',
            'iam:ListAttachedRolePolicies',
            'iam:ListRoleTags',
            'iam:TagRole',
            'iam:UntagRole',
            'iam:UpdateRole',
            'iam:UpdateRoleDescription',
            'iam:UpdateAssumeRolePolicy',
            'iam:DeleteRole',
          ],
          resources: [ourRoles],
        }),
        new iam.PolicyStatement({
          sid: 'PassOurRolesToOurServices',
          actions: ['iam:PassRole'],
          resources: [ourRoles],
          conditions: {
            StringEquals: {
              'iam:PassedToService': ['lambda.amazonaws.com', 'codebuild.amazonaws.com', 'codepipeline.amazonaws.com'],
            },
          },
        }),

        // --- Guardrails that no allow above can override ---
        new iam.PolicyStatement({
          sid: 'NeverRemoveTheBoundary',
          effect: iam.Effect.DENY,
          actions: ['iam:DeleteRolePermissionsBoundary'],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          sid: 'NeverChangeOurPolicies',
          effect: iam.Effect.DENY,
          actions: [
            'iam:CreatePolicyVersion',
            'iam:DeletePolicy',
            'iam:DeletePolicyVersion',
            'iam:SetDefaultPolicyVersion',
            'iam:TagPolicy',
            'iam:UntagPolicy',
          ],
          resources: [boundaryArn, executionArn, executionWebArn],
        }),
        new iam.PolicyStatement({
          // Reviewer accounts are managed by people, never by deploys: a merged template must not be able to add an
          // account to the reviewers group, set a password, or let identities from another provider sign in.
          sid: 'NeverManageReviewerAccounts',
          effect: iam.Effect.DENY,
          actions: [
            'cognito-idp:AdminCreateUser',
            'cognito-idp:AdminAddUserToGroup',
            'cognito-idp:AdminSetUserPassword',
            'cognito-idp:AdminResetUserPassword',
            'cognito-idp:AdminUpdateUserAttributes',
            'cognito-idp:AdminConfirmSignUp',
            'cognito-idp:AdminEnableUser',
            'cognito-idp:AdminSetUserMFAPreference',
            'cognito-idp:AdminLinkProviderForUser',
            'cognito-idp:CreateIdentityProvider',
            'cognito-idp:UpdateIdentityProvider',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          sid: 'NeverCreateUsersOrKeys',
          effect: iam.Effect.DENY,
          actions: ['iam:CreateUser', 'iam:CreateAccessKey', 'iam:CreateLoginProfile', 'iam:AttachUserPolicy'],
          resources: ['*'],
        }),
    ];
    this.execution = new iam.ManagedPolicy(this, 'CdkExecution', {
      managedPolicyName: EXECUTION_POLICY,
      description: 'What CloudFormation may do when deploying grant-review-workflow (replaces AdministratorAccess)',
      statements: executionStatements.filter((s) => !WEB_STATEMENTS.has(s.sid!)),
    });
    this.executionWeb = new iam.ManagedPolicy(this, 'CdkExecutionWeb', {
      managedPolicyName: EXECUTION_WEB_POLICY,
      description: 'What CloudFormation may do for the grant-review-workflow reviewer UI (the second half)',
      statements: executionStatements.filter((s) => WEB_STATEMENTS.has(s.sid!)),
    });

    // cdk-nag flags every wildcard. Here they are the design: name-prefix patterns for resources whose generated
    // names aren't known in advance (GrantReview*, grant-review-*), wildcard actions only on those, and in the
    // boundary a ceiling rather than a grant. Each finding is acknowledged individually, from the statements.
    const findings = new Set<string>();
    for (const doc of [this.boundary.document, this.execution.document, this.executionWeb.document]) {
      for (const s of doc.toJSON().Statement as any[]) {
        if (s.Effect !== 'Allow') continue;
        for (const action of [s.Action].flat()) if (action.includes('*')) findings.add(`Action::${action}`);
        for (const res of [s.Resource].flat()) if (res !== '*' && res.includes('*')) findings.add(`Resource::${res}`);
        if ([s.Resource].flat().includes('*')) findings.add('Resource::*');
      }
    }
    for (const finding of findings) {
      Validations.of(this).acknowledge({
        id: `AwsSolutions-IAM5[${finding}]`,
        reason: 'Prefix-scoped patterns for generated resource names; in the boundary, a ceiling rather than a grant.',
      });
    }

    new cdk.CfnOutput(this, 'ExecutionPolicyArns', {
      value: cdk.Fn.join(',', [this.execution.managedPolicyArn, this.executionWeb.managedPolicyArn]),
      description: 'cdk bootstrap --cloudformation-execution-policies <this>',
    });
    new cdk.CfnOutput(this, 'BoundaryPolicyArn', { value: this.boundary.managedPolicyArn });
  }
}
