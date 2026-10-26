import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

export const REVIEWERS_GROUP = 'reviewers';

export interface AuthStackProps extends cdk.StackProps {
  /** The sign-in page's address: <prefix>.auth.<region>.amazoncognito.com (global, so it's checked to be free). */
  readonly domainPrefix: string;
  /** Where the sign-in page may send a reviewer back to: the UI's address, exactly (scheme, host, port, path). */
  readonly callbackUrls: string[];
  /**
   * An SSM parameter holding the deployed UI's address (UiStack writes it), added to the callback URLs. Read when this
   * stack deploys, not when CDK builds it: the UI stack needs the API, the API needs this pool, so a direct reference
   * would be a cycle. In a new account, deploy this stack once more after the UI stack exists.
   */
  readonly uiUrlParameter?: string;
}

/**
 * Who may review (step 8, stage 4): a Cognito user pool for the reviewer UI.
 *
 * - Reviewers are invited by an administrator; nobody can sign themselves up. Applicant data isn't for anyone who
 *   can make an account.
 * - MFA with an authenticator app is required (free; SMS would cost money and need an IAM role).
 * - The UI signs in with the authorization code flow and PKCE, through the hosted sign-in page, as a public client
 *   with no secret (a browser can't keep one).
 * - Access tokens last 15 minutes. Refresh tokens last 12 hours, rotate on every use and can be revoked: a stolen
 *   token is useful briefly, and signing out ends the session.
 * - API Gateway's JWT authorizer (stage 5) checks the access token; the API itself checks the reviewers group.
 *
 * Essentials feature plan: the default, and free for 10,000 monthly active users (the free tier doesn't expire).
 */
export class AuthStack extends cdk.Stack {
  readonly userPool: cognito.UserPool;
  readonly client: cognito.UserPoolClient;
  readonly domain: cognito.UserPoolDomain;
  /** <prefix>.auth.<region>.amazoncognito.com: the hosted sign-in page, token and logout endpoints. */
  readonly signInDomain: string;

  constructor(scope: Construct, id: string, props: AuthStackProps) {
    super(scope, id, props);
    this.signInDomain = `${props.domainPrefix}.auth.${this.region}.amazoncognito.com`;
    const callbackUrls = [
      ...props.callbackUrls,
      ...(props.uiUrlParameter ? [ssm.StringParameter.valueForStringParameter(this, props.uiUrlParameter)] : []),
    ];

    this.userPool = new cognito.UserPool(this, 'Reviewers', {
      userPoolName: 'grant-review-reviewers',
      featurePlan: cognito.FeaturePlan.ESSENTIALS,
      selfSignUpEnabled: false,
      signInAliases: { username: true },
      signInCaseSensitive: false,
      standardAttributes: { email: { required: true, mutable: true } },
      autoVerify: { email: true },
      mfa: cognito.Mfa.REQUIRED,
      mfaSecondFactor: { otp: true, sms: false },
      passwordPolicy: {
        minLength: 14,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
        tempPasswordValidity: cdk.Duration.days(3),
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      email: cognito.UserPoolEmail.withCognito(), // Cognito's own sender: invitations and resets only
      userInvitation: {
        emailSubject: 'Your Prairie Commons Fund reviewer account',
        emailBody:
          'You have been added as a grant reviewer. Username: {username}. Temporary password: {####}. ' +
          'You will choose a new password and set up an authenticator app when you first sign in.',
      },
      deletionProtection: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN, // reviewer accounts outlive any one deploy
    });

    new cognito.CfnUserPoolGroup(this, 'ReviewersGroup', {
      userPoolId: this.userPool.userPoolId,
      groupName: REVIEWERS_GROUP,
      description: 'May read applications and decide them',
    });

    this.client = this.userPool.addClient('ReviewerUi', {
      userPoolClientName: 'grant-review-ui',
      generateSecret: false,
      // Set explicitly: left unset, Cognito picks a default set that includes ALLOW_REFRESH_TOKEN_AUTH, which conflicts
      // with refresh-token rotation. SRP never sends the password itself; the UI uses the hosted page either way, and
      // refreshes through the OAuth token endpoint.
      authFlows: { userSrp: true },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
        callbackUrls,
        logoutUrls: callbackUrls,
      },
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      accessTokenValidity: cdk.Duration.minutes(15),
      idTokenValidity: cdk.Duration.minutes(15),
      refreshTokenValidity: cdk.Duration.hours(12),
      refreshTokenRotationGracePeriod: cdk.Duration.seconds(10),
      enableTokenRevocation: true,
      preventUserExistenceErrors: true,
    });

    this.domain = this.userPool.addDomain('SignIn', {
      cognitoDomain: { domainPrefix: props.domainPrefix },
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    });
    // Managed login needs a style for each app client; Cognito's own default look is enough.
    new cognito.CfnManagedLoginBranding(this, 'SignInStyle', {
      userPoolId: this.userPool.userPoolId,
      clientId: this.client.userPoolClientId,
      useCognitoProvidedValues: true,
    });

    Validations.of(this.userPool).acknowledge({
      id: 'AwsSolutions-COG8',
      reason:
        'Threat protection (adaptive authentication, compromised-credential checks) needs the Plus plan, ' +
        '$0.02 per user with no free tier. Sign-up is closed, MFA is required and passwords are long instead.',
    });

    // What the UI needs to send reviewers to the sign-in page: identifiers, not secrets.
    new cdk.CfnOutput(this, 'UserPoolId', { value: this.userPool.userPoolId });
    new cdk.CfnOutput(this, 'ClientId', { value: this.client.userPoolClientId });
    new cdk.CfnOutput(this, 'SignInDomain', { value: this.signInDomain });
  }
}
