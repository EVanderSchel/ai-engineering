interface ImportMetaEnv {
  readonly VITE_API_URL?: string; // the reviewer API's base URL; unset: local fixtures
  readonly VITE_COGNITO_USER_POOL_ID?: string; // the reviewers' user pool; unset: no sign-in
  readonly VITE_COGNITO_CLIENT_ID?: string; // its app client for this UI (an identifier, not a secret)
}
