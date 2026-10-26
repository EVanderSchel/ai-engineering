"""The local API's stand-in for API Gateway's JWT authorizer, without the network: tokens are signed with a throwaway
RSA key, and the "published keys" are a stand-in for the pool's JWKS."""

import time

import jwt
import local_api
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

POOL = "us-east-2_TestPool1"
CLIENT = "testclient123"
ISSUER = f"https://cognito-idp.us-east-2.amazonaws.com/{POOL}"


def keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


POOL_KEY, OTHER_KEY = keypair(), keypair()


class PublishedKeys:
    """The pool's JWKS: only its own key."""

    def get_signing_key_from_jwt(self, token):
        class Key:
            key = POOL_KEY.public_key()

        return Key()


def token(signed_with=POOL_KEY, **changes):
    """An access token as Cognito issues it, with any claim changed (None removes it)."""
    now = int(time.time())
    claims = {
        "sub": "6b1e-test",
        "cognito:groups": ["reviewers"],
        "iss": ISSUER,
        "client_id": CLIENT,
        "token_use": "access",
        "scope": "openid email",
        "auth_time": now,
        "exp": now + 900,
        "iat": now,
        "username": "ethan",
    }
    claims = {k: v for k, v in {**claims, **changes}.items() if v is not None}
    return "Bearer " + jwt.encode(claims, signed_with, algorithm="RS256")


@pytest.fixture
def verifier():
    return local_api.CognitoVerifier(POOL, CLIENT, keys=PublishedKeys())


def test_a_valid_access_token_passes_its_claims_as_api_gateway_does(verifier):
    claims = verifier.claims(token())
    assert claims["username"] == "ethan" and claims["cognito:groups"] == "[reviewers]"
    assert all(isinstance(v, str) for v in claims.values())  # every claim as text, lists included


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Basic dXNlcjpwYXNz",
        token(signed_with=OTHER_KEY),  # not signed by the pool
        token(exp=int(time.time()) - 60),  # expired
        token(iss="https://cognito-idp.us-east-2.amazonaws.com/us-east-2_Other"),  # another pool
        token(client_id="someone-elses-app"),  # another app client
        token(token_use="id"),  # an ID token: proves identity, isn't for calling the API
        token(client_id=None),
        token(exp=None),
    ],
    ids=["none", "empty", "basic", "foreign-key", "expired", "issuer", "client", "id-token", "no-client", "no-exp"],
)
def test_everything_else_is_refused(verifier, authorization):
    assert verifier.claims(authorization) is None


def test_a_refused_request_gets_api_gateways_401_and_never_reaches_the_handler(verifier, monkeypatch):
    monkeypatch.setattr(local_api.api, "handler", lambda event: pytest.fail("the handler ran"))
    assert local_api.respond("GET", "/applications", None, token(signed_with=OTHER_KEY), verifier) == (
        local_api.UNAUTHORIZED
    )


def test_an_accepted_request_carries_the_token_claims_to_the_handler(verifier, monkeypatch):
    seen = []
    monkeypatch.setattr(local_api.api, "handler", lambda event: seen.append(event) or {"statusCode": 200, "body": "{}"})
    local_api.respond("GET", "/applications?status=decided", None, token(), verifier)
    [event] = seen
    assert event["routeKey"] == "GET /applications" and event["queryStringParameters"] == {"status": "decided"}
    assert event["requestContext"]["authorizer"]["jwt"]["claims"]["username"] == "ethan"


def test_without_a_pool_every_request_is_the_fixed_local_reviewer(monkeypatch):
    seen = []
    monkeypatch.setattr(local_api.api, "handler", lambda event: seen.append(event) or {"statusCode": 200, "body": "{}"})
    local_api.respond("GET", "/applications", None, None, verifier=None)
    assert seen[0]["requestContext"]["authorizer"]["jwt"]["claims"] == local_api.CLAIMS


def test_the_pool_ids_are_read_from_the_uis_env_file(tmp_path):
    env = tmp_path / ".env.api.local"
    env.write_text(
        "# the reviewers' pool\nVITE_COGNITO_USER_POOL_ID=us-east-2_X\nVITE_COGNITO_CLIENT_ID=abc\n", encoding="utf-8"
    )
    assert local_api.ui_env(env) == {"VITE_COGNITO_USER_POOL_ID": "us-east-2_X", "VITE_COGNITO_CLIENT_ID": "abc"}
    assert local_api.ui_env(tmp_path / "missing") == {}


def test_the_issuer_follows_the_pools_region():
    v = local_api.CognitoVerifier("eu-west-1_Abc", CLIENT, keys=PublishedKeys())
    assert v.issuer == "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_Abc"


@pytest.mark.parametrize(
    "authorization, reason",
    [
        (None, "no Authorization header"),
        ("Basic dXNlcjpwYXNz", "not a Bearer token"),
        (token(signed_with=OTHER_KEY), "InvalidSignatureError"),
        (token(exp=int(time.time()) - 60), "ExpiredSignatureError"),
        (token(token_use="id"), "a id token, not an access token"),
        (token(client_id="someone-elses-app"), "issued to another app client"),
    ],
)
def test_a_refusal_says_why_without_the_token(verifier, authorization, reason, capsys):
    assert verifier.check(authorization)[1].startswith(reason)
    local_api.respond("GET", "/applications?status=decided", None, authorization, verifier)
    logged = capsys.readouterr().err
    assert logged.startswith("401 GET /applications: " + reason)
    assert authorization is None or authorization.split(" ", 1)[1] not in logged  # never the token itself
