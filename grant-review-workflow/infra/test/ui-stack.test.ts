import { Validations } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { AwsSolutionsChecks } from 'cdk-nag';
import { testApp } from './app';
import { buildApp } from '../lib/app';
import { UI_URL_PARAMETER } from '../lib/ui-stack';

const env = { account: '111122223333', region: 'us-east-2' };
const app = testApp();
buildApp(app, env);
Validations.of(app).addPlugins(new AwsSolutionsChecks(app)); // an unacknowledged finding throws here
app.synth();
const template = Template.fromStack(app.node.findChild('GrantReviewUi') as any);
const auth = Template.fromStack(app.node.findChild('GrantReviewAuth') as any);
const distribution = () =>
  (Object.values(template.findResources('AWS::CloudFront::Distribution'))[0] as any).Properties.DistributionConfig;
const apiBehavior = () => distribution().CacheBehaviors.find((b: any) => b.PathPattern === '/api/*');

test('the site bucket is private and only CloudFront can read it', () => {
  template.hasResourceProperties('AWS::S3::Bucket', {
    BucketName: 'grant-review-ui-111122223333',
    PublicAccessBlockConfiguration: {
      BlockPublicAcls: true,
      BlockPublicPolicy: true,
      IgnorePublicAcls: true,
      RestrictPublicBuckets: true,
    },
  });
  template.resourceCountIs('AWS::CloudFront::OriginAccessControl', 1);
  template.hasResourceProperties('AWS::S3::BucketPolicy', {
    PolicyDocument: {
      Statement: Match.arrayWith([
        Match.objectLike({ Principal: { Service: 'cloudfront.amazonaws.com' }, Action: 's3:GetObject' }),
      ]),
    },
  });
  template.resourceCountIs('AWS::Lambda::Function', 0); // no BucketDeployment helper: npm run publish uploads
  template.resourceCountIs('AWS::IAM::Role', 0);
});

test('/api/* goes to the reviewer API with the prefix stripped, the token forwarded, nothing cached', () => {
  const api = apiBehavior();
  expect(api.ViewerProtocolPolicy).toBe('https-only');
  expect(api.AllowedMethods).toEqual(expect.arrayContaining(['GET', 'POST']));
  expect(api.FunctionAssociations).toHaveLength(1);
  expect(api.FunctionAssociations[0].EventType).toBe('viewer-request');
  template.hasResourceProperties('AWS::CloudFront::CachePolicy', {
    CachePolicyConfig: {
      DefaultTTL: 0,
      MinTTL: 0,
      MaxTTL: 1,
      ParametersInCacheKeyAndForwardedToOrigin: Match.objectLike({
        HeadersConfig: { HeaderBehavior: 'whitelist', Headers: ['Authorization'] },
        QueryStringsConfig: { QueryStringBehavior: 'all' },
      }),
    },
  });
  const origin = distribution().Origins.find((o: any) => o.Id === api.TargetOriginId);
  expect(JSON.stringify(origin.DomainName)).toContain('ApiEndpoint'); // the HTTP API, by cross-stack reference
  expect(origin.CustomOriginConfig.OriginProtocolPolicy).toBe('https-only');
});

test('the CloudFront Function strips exactly a leading /api', () => {
  const code = (Object.values(template.findResources('AWS::CloudFront::Function'))[0] as any).Properties.FunctionCode;
  // Run the function as CloudFront would, on a few paths.
  const handler = new Function(`${code}; return handler;`)();
  const uri = (path: string) => handler({ request: { uri: path } }).uri;
  expect(uri('/api/applications')).toBe('/applications');
  expect(uri('/api/applications/gp-027/decision')).toBe('/applications/gp-027/decision');
  expect(uri('/api')).toBe('/');
  expect(uri('/apiary/x')).toBe('/apiary/x'); // only the /api segment
  expect(uri('/applications')).toBe('/applications');
});

test('every response carries the security headers, with a CSP that allows only the site and Cognito', () => {
  const headers = (Object.values(template.findResources('AWS::CloudFront::ResponseHeadersPolicy'))[0] as any).Properties
    .ResponseHeadersPolicyConfig.SecurityHeadersConfig;
  const csp: string = headers.ContentSecurityPolicy.ContentSecurityPolicy;
  expect(csp).toContain("default-src 'none'");
  expect(csp).toContain("script-src 'self'");
  expect(csp).toContain("frame-ancestors 'none'");
  expect(csp).toContain(
    "connect-src 'self' https://cognito-idp.us-east-2.amazonaws.com https://prairie-commons-review.auth.us-east-2.amazoncognito.com",
  );
  expect(csp).not.toMatch(/unsafe-inline|unsafe-eval|\*/);
  expect(headers.StrictTransportSecurity.AccessControlMaxAgeSec).toBe(365 * 24 * 3600);
  expect(headers.ReferrerPolicy.ReferrerPolicy).toBe('no-referrer');
  expect(headers.FrameOptions.FrameOption).toBe('DENY');
  expect(distribution().DefaultCacheBehavior.ResponseHeadersPolicyId).toBeDefined();
  expect(apiBehavior().ResponseHeadersPolicyId).toBeDefined();
});

test('the UI records its address, and the auth stack adds it to the callback URLs when it deploys', () => {
  template.hasResourceProperties('AWS::SSM::Parameter', { Name: UI_URL_PARAMETER, Type: 'String' });
  const parameters = auth.toJSON().Parameters;
  const fromSsm = Object.entries(parameters).find(([, p]: any) => p.Default === UI_URL_PARAMETER) as any;
  expect(fromSsm[1].Type).toBe('AWS::SSM::Parameter::Value<String>');
  const client = (Object.values(auth.findResources('AWS::Cognito::UserPoolClient'))[0] as any).Properties;
  expect(client.CallbackURLs).toEqual(['http://localhost:5174/', { Ref: fromSsm[0] }]);
  expect(client.LogoutURLs).toEqual(client.CallbackURLs);
});
