import { Validations } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { AwsSolutionsChecks } from 'cdk-nag';
import { testApp } from './app';
import { AuthStack, REVIEWERS_GROUP } from '../lib/auth-stack';

const env = { account: '111122223333', region: 'us-east-2' };
const app = testApp();
const stack = new AuthStack(app, 'Auth', {
  env,
  domainPrefix: 'prairie-commons-review',
  callbackUrls: ['http://localhost:5174/'],
});
Validations.of(app).addPlugins(new AwsSolutionsChecks(app)); // an unacknowledged finding throws here
app.synth();
const template = Template.fromStack(stack);
const client = () => Object.values(template.findResources('AWS::Cognito::UserPoolClient'))[0].Properties;

test('only an administrator can create accounts, and every account needs an authenticator app', () => {
  template.hasResourceProperties('AWS::Cognito::UserPool', {
    UserPoolTier: 'ESSENTIALS',
    AdminCreateUserConfig: Match.objectLike({ AllowAdminCreateUserOnly: true }),
    MfaConfiguration: 'ON',
    EnabledMfas: ['SOFTWARE_TOKEN_MFA'],
    DeletionProtection: 'ACTIVE',
    Policies: { PasswordPolicy: Match.objectLike({ MinimumLength: 14 }) },
  });
  template.hasResource('AWS::Cognito::UserPool', { DeletionPolicy: 'Retain' });
  template.resourceCountIs('AWS::IAM::Role', 0); // no SMS, so no role for Cognito to send it
});

test('the UI is a public client using the code flow with PKCE, nothing else', () => {
  const c = client();
  expect(c.GenerateSecret).toBe(false);
  expect(c.AllowedOAuthFlows).toEqual(['code']);
  expect(c.AllowedOAuthFlowsUserPoolClient).toBe(true);
  expect(c.CallbackURLs).toEqual(['http://localhost:5174/']);
  expect(c.SupportedIdentityProviders).toEqual(['COGNITO']);
});

test('tokens are short-lived, refresh tokens rotate and can be revoked', () => {
  const c = client();
  expect([c.AccessTokenValidity, c.IdTokenValidity, c.TokenValidityUnits.AccessToken]).toEqual([15, 15, 'minutes']);
  expect([c.RefreshTokenValidity, c.TokenValidityUnits.RefreshToken]).toEqual([720, 'minutes']);
  expect(c.RefreshTokenRotation).toEqual({ Feature: 'ENABLED', RetryGracePeriodSeconds: 10 });
  expect(c.EnableTokenRevocation).toBe(true);
});

test('the auth flows are set explicitly, without the one that conflicts with rotation', () => {
  // Unset, Cognito would default to a set including ALLOW_REFRESH_TOKEN_AUTH, which rotation rejects.
  expect(client().ExplicitAuthFlows).toEqual(['ALLOW_USER_SRP_AUTH']);
});

test('the reviewers group, the managed sign-in page and its style exist', () => {
  template.hasResourceProperties('AWS::Cognito::UserPoolGroup', { GroupName: REVIEWERS_GROUP });
  template.hasResourceProperties('AWS::Cognito::UserPoolDomain', {
    Domain: 'prairie-commons-review',
    ManagedLoginVersion: 2,
  });
  template.hasResourceProperties('AWS::Cognito::ManagedLoginBranding', { UseCognitoProvidedValues: true });
});
