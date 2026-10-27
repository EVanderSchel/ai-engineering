"""API keys: a team's key is shown once and only its SHA-256 hash is kept.

Plain language: like a password, the gateway never stores the key itself, only a fingerprint of it. A leaked
configuration file or database dump then gives nobody a working key.

Step 2 reads the hashes from the GATEWAY_KEYS environment variable ("team:hash,team:hash"), which a Kubernetes
Secret can supply; step 4 moves them into PostgreSQL with revocation and per-team limits.

    uv run python src/keys.py new <team>    # prints a new key (once) and the line to add to GATEWAY_KEYS
"""

import hashlib
import secrets
import sys

PREFIX = "gw_"


def new_key() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def parse_keys(value: str) -> dict[str, str]:
    """GATEWAY_KEYS ("team:hash,team:hash") as {hash: team}."""
    keys = {}
    for entry in filter(None, (part.strip() for part in value.split(","))):
        team, sep, digest = entry.partition(":")
        if not sep or not team or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"GATEWAY_KEYS entry {entry[:20]!r}... is not team:<sha256 hex>")
        keys[digest] = team
    return keys


def team_for(authorization: str | None, keys: dict[str, str]) -> str | None:
    """The team a Bearer key belongs to, or None. Comparing hashes means no stored key can be read back."""
    if not authorization:
        return None
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "bearer" or not key.startswith(PREFIX):
        return None
    return keys.get(key_hash(key.strip()))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "new":
        sys.exit("usage: python src/keys.py new <team>")
    key = new_key()
    print(f"Key for {sys.argv[2]} (shown once; give it to the team, don't store it):\n  {key}")
    print(f"Add to GATEWAY_KEYS:\n  {sys.argv[2]}:{key_hash(key)}")
