import * as fs from 'node:fs';
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib/core';

/** An App with cdk.json's context (its feature flags), so tests synthesize exactly as `cdk synth` does. */
export function testApp(): cdk.App {
  const config = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'cdk.json'), 'utf8'));
  return new cdk.App({ context: config.context });
}
