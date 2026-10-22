import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as codeconnections from 'aws-cdk-lib/aws-codeconnections';
import * as codepipeline from 'aws-cdk-lib/aws-codepipeline';
import * as actions from 'aws-cdk-lib/aws-codepipeline-actions';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface PipelineStackProps extends cdk.StackProps {
  /** GitHub owner and repository the pipeline deploys from. */
  readonly owner: string;
  readonly repo: string;
  readonly branch?: string;
  /** The CDK bootstrap qualifier (cdk bootstrap's default). */
  readonly qualifier?: string;
}

/**
 * Deploys on merge, with an approval (step 7, stage 6). The project's service control policy blocks IAM identity
 * providers, so GitHub Actions can't assume a role through OIDC. Instead the pipeline runs inside AWS: a
 * CodeConnections GitHub App (approved once in the console) notifies it of pushes to main that touch
 * grant-review-workflow/, and then:
 *
 *   Source (GitHub) -> Diff (tests, synth, cdk diff) -> Approve (a person reads the diff) -> Deploy (cdk deploy)
 *
 * Python tests aren't repeated here: main only accepts merges whose GitHub Actions checks passed. The build roles
 * hold no deploy permissions of their own; they may only assume CDK's bootstrap roles, which do the deploying.
 */
export class PipelineStack extends cdk.Stack {
  readonly connection: codeconnections.CfnConnection;
  readonly pipeline: codepipeline.Pipeline;

  constructor(scope: Construct, id: string, props: PipelineStackProps) {
    super(scope, id, props);
    const branch = props.branch ?? 'main';
    const qualifier = props.qualifier ?? 'hnb659fds';
    const bootstrapRole = (kind: string) =>
      `arn:aws:iam::${this.account}:role/cdk-${qualifier}-${kind}-role-${this.account}-${this.region}`;

    // Created PENDING: a person completes it once in the console (Developer Tools > Settings > Connections) by
    // installing AWS's GitHub App on the repository. No keys or tokens are stored anywhere.
    this.connection = new codeconnections.CfnConnection(this, 'GitHub', {
      connectionName: 'grant-review-github',
      providerType: 'GitHub',
    });

    const artifacts = new s3.Bucket(this, 'Artifacts', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(30) }], // a source snapshot per run; nothing to keep
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: false,
    });
    Validations.of(artifacts).acknowledge({
      id: 'AwsSolutions-S1',
      reason: 'Pipeline artifact bucket: only the pipeline writes to it, and CloudTrail records who changed it.',
    });

    const diff = this.cdkProject('Diff', {
      description: 'grant-review-workflow: infra tests, cdk synth and cdk diff (read by the approver)',
      privileged: false,
      commands: ['npm test', 'npx cdk synth --strict', 'npx cdk diff GrantReviewData GrantReviewWorkflow'],
      assume: [bootstrapRole('lookup'), bootstrapRole('deploy')],
    });
    const deploy = this.cdkProject('Deploy', {
      description: 'grant-review-workflow: cdk deploy of the data and workflow stacks',
      privileged: true, // builds the Lambda container image with Docker
      commands: ['npx cdk deploy GrantReviewData GrantReviewWorkflow --require-approval never'],
      assume: [
        bootstrapRole('lookup'),
        bootstrapRole('deploy'),
        bootstrapRole('file-publishing'),
        bootstrapRole('image-publishing'),
      ],
    });

    const source = new codepipeline.Artifact('Source');
    const sourceAction = new actions.CodeStarConnectionsSourceAction({
      actionName: 'GitHub',
      connectionArn: this.connection.attrConnectionArn,
      owner: props.owner,
      repo: props.repo,
      branch,
      output: source,
      codeBuildCloneOutput: false,
    });

    this.pipeline = new codepipeline.Pipeline(this, 'Pipeline', {
      pipelineName: 'grant-review-workflow',
      pipelineType: codepipeline.PipelineType.V2,
      executionMode: codepipeline.ExecutionMode.QUEUED, // each merge is deployed in order, none skipped
      artifactBucket: artifacts,
      restartExecutionOnUpdate: false,
      triggers: [
        {
          providerType: codepipeline.ProviderType.CODE_STAR_SOURCE_CONNECTION,
          gitConfiguration: {
            sourceAction,
            pushFilter: [{ branchesIncludes: [branch], filePathsIncludes: ['grant-review-workflow/**'] }],
          },
        },
      ],
      stages: [
        { stageName: 'Source', actions: [sourceAction] },
        { stageName: 'Diff', actions: [new actions.CodeBuildAction({ actionName: 'Diff', project: diff, input: source })] },
        {
          stageName: 'Approve',
          actions: [
            new actions.ManualApprovalAction({
              actionName: 'Approve',
              additionalInformation:
                'Read the Diff stage log (cdk diff) before approving: it lists every resource this deploy changes.',
            }),
          ],
        },
        {
          stageName: 'Deploy',
          actions: [new actions.CodeBuildAction({ actionName: 'Deploy', project: deploy, input: source })],
        },
      ],
    });
    // CDK's own grants for a pipeline and its builds: wildcard *actions* on this stack's artifact bucket, and each
    // build's default log and report groups. Every resource is this stack's own; nothing reaches beyond it.
    const logicalId = (c: Construct) => cdk.Stack.of(this).getLogicalId(c.node.defaultChild as cdk.CfnElement);
    // cdk-nag prints the partition as written in the template: 'aws' when CDK resolved it (cdk.json's feature
    // flags), a placeholder otherwise.
    const partition = cdk.Token.isUnresolved(this.partition) ? '<AWS::Partition>' : this.partition;
    const ownArtifacts = [
      ...['s3:Abort*', 's3:DeleteObject*', 's3:GetBucket*', 's3:GetObject*', 's3:List*'].map((a) => `Action::${a}`),
      `Resource::<${logicalId(artifacts)}.Arn>/*`,
    ];
    const ownBuildGroups = [diff, deploy].flatMap((p) => [
      `Resource::arn:${partition}:logs:${this.region}:${this.account}:log-group:/aws/codebuild/<${logicalId(p)}>:*`,
      `Resource::arn:${partition}:codebuild:${this.region}:${this.account}:report-group/<${logicalId(p)}>-*`,
    ]);
    for (const finding of ownArtifacts) {
      Validations.of(this).acknowledge({
        id: `AwsSolutions-IAM5[${finding}]`,
        reason: "CDK's pipeline and CodeBuild grants on this stack's own artifact bucket (read and write run artifacts).",
      });
    }
    for (const finding of ownBuildGroups) {
      Validations.of(this).acknowledge({
        id: `AwsSolutions-IAM5[${finding}]`,
        reason: "CodeBuild's default grants on the build's own log group and test-report groups.",
      });
    }

    new cdk.CfnOutput(this, 'ConnectionArn', {
      value: this.connection.attrConnectionArn,
      description: 'Complete this connection once in the console (Developer Tools > Settings > Connections)',
    });
    new cdk.CfnOutput(this, 'PipelineName', { value: this.pipeline.pipelineName });
  }

  /** A CodeBuild project that runs CDK commands in infra/ with a role that can only assume bootstrap roles. */
  private cdkProject(
    id: string,
    opts: { description: string; privileged: boolean; commands: string[]; assume: string[] },
  ): codebuild.PipelineProject {
    const logGroup = new logs.LogGroup(this, `${id}Logs`, {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const project = new codebuild.PipelineProject(this, id, {
      description: opts.description,
      environment: {
        buildImage: codebuild.LinuxBuildImage.STANDARD_8_0,
        computeType: codebuild.ComputeType.SMALL,
        privileged: opts.privileged,
      },
      timeout: cdk.Duration.minutes(30),
      logging: { cloudWatch: { logGroup } },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: {
          install: {
            'runtime-versions': { nodejs: 22 },
            commands: ['cd grant-review-workflow/infra', 'npm ci'],
          },
          build: { commands: opts.commands },
        },
      }),
    });
    project.addToRolePolicy(
      new iam.PolicyStatement({ sid: 'AssumeCdkBootstrapRoles', actions: ['sts:AssumeRole'], resources: opts.assume }),
    );
    Validations.of(project).acknowledge({
      id: 'AwsSolutions-CB4',
      reason: 'Build artifacts are encrypted with the S3-managed key of the artifact bucket; no secrets are built.',
    });
    if (opts.privileged) {
      Validations.of(project).acknowledge({
        id: 'AwsSolutions-CB3',
        reason: 'Privileged mode runs Docker to build the Lambda container image during cdk deploy.',
      });
    }
    return project;
  }
}
