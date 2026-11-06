"""Creates (or updates) the cluster's Secret for the gateway from this PC's local settings (step 8).

    uv run python k8s/make_secret.py                    # the database password, from .env
    uv run python k8s/make_secret.py --with-anthropic   # and ANTHROPIC_API_KEY, from the environment

The values go to `kubectl apply` on its standard input: never on a command line (which other processes can read),
never in a file, never printed. The chart only names the Secret (values.yaml `secretName`), so nothing secret is in
the chart or the repo.

Keys in the Secret:
- POSTGRES_PASSWORD: the cluster database's password (the same as the compose database's, from .env);
- DATABASE_URL: how the gateway reaches it, at the chart's PostgreSQL Service;
- GRAFANA_ADMIN_PASSWORD: Grafana's admin password, from .env, when it's there (step 8 stage 4);
- ANTHROPIC_API_KEY (optional): only with --with-anthropic. On Windows it's read from the User environment variable
  if this process doesn't have it (a terminal opened before it was set won't).
"""

import argparse
import base64
import contextlib
import json
import os
import pathlib
import subprocess
import sys

AI_GATEWAY = pathlib.Path(__file__).resolve().parent.parent
NAME = "ai-gateway-secrets"
DATABASE_HOST = "ai-gateway-postgres"  # the Service the chart makes, for a release named ai-gateway


def dotenv(path: pathlib.Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values


def anthropic_key() -> str | None:
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    if sys.platform == "win32":
        import winreg

        with contextlib.suppress(OSError), winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as env:
            return winreg.QueryValueEx(env, "ANTHROPIC_API_KEY")[0]
    return None


def manifest(password: str, extra: dict[str, str]) -> dict:
    data = {
        "POSTGRES_PASSWORD": password,
        "DATABASE_URL": f"postgresql://gateway:{password}@{DATABASE_HOST}:5432/gateway",
        **extra,
    }
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": NAME, "labels": {"app.kubernetes.io/part-of": "ai-gateway"}},
        "type": "Opaque",
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--with-anthropic", action="store_true")
    args = parser.parse_args()
    env = dotenv(AI_GATEWAY / ".env")
    password = env.get("POSTGRES_PASSWORD")
    if not password:
        sys.exit("POSTGRES_PASSWORD isn't in ai-gateway/.env")
    extra = {}
    if grafana := env.get("GRAFANA_ADMIN_PASSWORD"):  # for Grafana in the cluster (chart value observability)
        extra["GRAFANA_ADMIN_PASSWORD"] = grafana
    if args.with_anthropic:
        key = anthropic_key()
        if not key:
            sys.exit("ANTHROPIC_API_KEY isn't set")
        extra["ANTHROPIC_API_KEY"] = key
    result = subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(manifest(password, extra)),
                            capture_output=True, text=True)  # fmt: skip
    if result.returncode:
        sys.exit(result.stderr.strip())
    print(result.stdout.strip(), "with keys:", ", ".join(sorted(manifest(password, extra)["data"])))


if __name__ == "__main__":
    main()
