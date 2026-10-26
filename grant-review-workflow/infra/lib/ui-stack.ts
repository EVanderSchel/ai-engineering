import * as cdk from 'aws-cdk-lib/core';
import { Validations } from 'aws-cdk-lib';
import * as apigw from 'aws-cdk-lib/aws-apigatewayv2';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

/** Where the UI stack records its address, for the auth stack's callback URLs (see AuthStack). */
export const UI_URL_PARAMETER = '/grant-review/reviewer-ui-url';

export interface UiStackProps extends cdk.StackProps {
  /** The reviewer API: CloudFront sends /api/* there. */
  readonly api: apigw.IHttpApi;
  /** The hosted sign-in page's domain (<prefix>.auth.<region>.amazoncognito.com): the only other place the app calls. */
  readonly signInDomain: string;
}

/**
 * The reviewer UI on the web (step 8, stage 5): one address for the app and its API.
 *
 * - The built app (ui/dist) lives in a private bucket that only this distribution can read (Origin Access Control).
 * - /api/* goes to the HTTP API, with "/api" stripped by a CloudFront Function, as the local dev server's proxy
 *   does: the UI calls its own origin, so there's no CORS. API responses are never cached.
 * - Every response carries security headers; the Content-Security-Policy lets the page run only its own scripts and
 *   call only its own origin and the reviewers' Cognito endpoints.
 *
 * The app is uploaded by `npm run publish` in ui/ (the pipeline runs it after each deploy), not by CDK: CDK's
 * BucketDeployment would add a helper Lambda and role, which the execution policy and boundary don't allow.
 */
export class UiStack extends cdk.Stack {
  readonly bucket: s3.Bucket;
  readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: UiStackProps) {
    super(scope, id, props);

    this.bucket = new s3.Bucket(this, 'Site', {
      bucketName: `grant-review-ui-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN, // a stack deletion leaves it; empty it and delete it by hand
    });
    Validations.of(this.bucket).acknowledge({
      id: 'AwsSolutions-S1',
      reason: 'The built app only (no applicant data); CloudFront and API Gateway record the requests that matter.',
    });

    // "/api/applications" -> "/applications": the API's routes have no prefix (src/api.py's route keys).
    const stripApi = new cloudfront.Function(this, 'StripApi', {
      functionName: 'GrantReview-strip-api',
      comment: 'Remove the /api prefix before a request goes to the reviewer API',
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(
        [
          'function handler(event) {',
          '  var request = event.request;',
          "  request.uri = request.uri.replace(/^\\/api(?=\\/|$)/, '') || '/';",
          '  return request;',
          '}',
        ].join('\n'),
      ),
    });

    // CloudFront forwards the Authorization header only when a cache policy names it. Nothing is cached: the API sets
    // no cache headers, so the default TTL of 0 applies (a policy whose TTLs are all 0 may not name headers at all).
    const noCacheWithToken = new cloudfront.CachePolicy(this, 'ApiCache', {
      cachePolicyName: 'GrantReview-api-pass-token',
      comment: 'Reviewer API: forward the access token, cache nothing',
      minTtl: cdk.Duration.seconds(0),
      defaultTtl: cdk.Duration.seconds(0),
      maxTtl: cdk.Duration.seconds(1),
      headerBehavior: cloudfront.CacheHeaderBehavior.allowList('Authorization'),
      queryStringBehavior: cloudfront.CacheQueryStringBehavior.all(), // ?status=
      cookieBehavior: cloudfront.CacheCookieBehavior.none(),
    });

    const cognito = `https://cognito-idp.${this.region}.amazonaws.com https://${props.signInDomain}`;
    const securityHeaders = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      responseHeadersPolicyName: 'GrantReview-security-headers',
      comment: 'Reviewer UI: CSP, HSTS, no framing, no sniffing, no referrer',
      securityHeadersBehavior: {
        contentSecurityPolicy: {
          contentSecurityPolicy: [
            "default-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            `connect-src 'self' ${cognito}`, // the API (same origin), sign-in discovery, token and revoke endpoints
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
          ].join('; '),
          override: true,
        },
        strictTransportSecurity: { accessControlMaxAge: cdk.Duration.days(365), includeSubdomains: true, override: true },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        // The sign-in code arrives in the address bar; no-referrer keeps it from leaking to any other site.
        referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.NO_REFERRER, override: true },
      },
    });

    // The API's own address, https://<id>.execute-api.<region>.amazonaws.com, without the scheme.
    const apiDomain = cdk.Fn.select(2, cdk.Fn.split('/', props.api.apiEndpoint));
    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: 'grant-review-workflow reviewer UI and its API',
      defaultRootObject: 'index.html',
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100, // North America and Europe: the cheapest set of edge locations
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED, // file names carry a content hash; index.html is invalidated
        responseHeadersPolicy: securityHeaders,
      },
      additionalBehaviors: {
        '/api/*': {
          origin: new origins.HttpOrigin(apiDomain, { protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY }),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL, // POST for decisions
          cachePolicy: noCacheWithToken,
          // Everything else the browser sent (Content-Type, the query string), except Host: API Gateway needs its own.
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          responseHeadersPolicy: securityHeaders,
          functionAssociations: [{ function: stripApi, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST }],
        },
      },
    });
    for (const [id, reason] of [
      ['AwsSolutions-CFR1', 'No geographic restriction: reviewers may travel; sign-in limits who can use it.'],
      ['AwsSolutions-CFR2', 'No WAF ($5+ a month): sign-in, the JWT authorizer, the reviewers group and throttling cover it.'],
      ['AwsSolutions-CFR3', 'No CloudFront access logs: API Gateway logs every API call with the reviewer; the rest is static files.'],
      ['AwsSolutions-CFR4', 'The default *.cloudfront.net certificate, so TLS versions are CloudFront\'s defaults (no custom domain).'],
    ]) {
      Validations.of(this.distribution).acknowledge({ id, reason });
    }

    const url = `https://${this.distribution.distributionDomainName}/`;
    new ssm.StringParameter(this, 'UiUrl', {
      parameterName: UI_URL_PARAMETER,
      stringValue: url,
      description: 'The reviewer UI\'s address: GrantReviewAuth adds it to the app client\'s callback URLs',
    });

    new cdk.CfnOutput(this, 'ReviewerUiUrl', { value: url });
    new cdk.CfnOutput(this, 'SiteBucket', { value: this.bucket.bucketName });
    new cdk.CfnOutput(this, 'DistributionId', { value: this.distribution.distributionId });
  }
}
