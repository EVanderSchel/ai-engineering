"""Brings the gateway up on EKS (step 11), one stage at a time, so each can be checked before the next costs money.

    uv run python eks/up.py check       # free: the AWS sign-in, the Region, the plan, Kubernetes versions, Docker
    uv run python eks/up.py cluster     # eksctl create cluster: the VPC, the control plane ($0.10 an hour from here)
    uv run python eks/up.py nodes       # the public node pool and the gp3 StorageClass (eks/nodes.yaml)
    uv run python eks/up.py image       # an ECR repository, and the gateway's image built and pushed to it
    uv run python eks/up.py install     # the Secret, then the chart with eks/values.yaml
    uv run python eks/down.py           # when done: everything above removed, and checked

Every AWS call uses the agent-toolkit profile in us-east-2. The AWS account number is read at run time for the ECR
address and never written to a file or printed. kubectl and helm refuse to run unless the current context is this
cluster, so nothing here can land on the kind cluster by mistake.
"""

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

AI_GATEWAY = pathlib.Path(__file__).resolve().parent.parent
EKS = AI_GATEWAY / "eks"
PROFILE, REGION, CLUSTER = "agent-toolkit", "us-east-2", "ai-gateway"
REPOSITORY = "ai-gateway"
TAG = "dev"
LOCAL_BIN = pathlib.Path.home() / ".local" / "bin"  # kind, helm, k6 and eksctl were installed here (step 0, step 11)


def tool(name: str) -> str:
    local = LOCAL_BIN / (f"{name}.exe" if sys.platform == "win32" else name)
    return str(local) if local.exists() else (shutil.which(name) or name)


def run(
    args: list[str], *, capture: bool = False, input: str | None = None, check: bool = True, env: dict | None = None
) -> str:
    """Runs a command; shows its output unless captured. Stops the script on failure, with the command's own error."""
    shown = " ".join(args[:6]) + (" ..." if len(args) > 6 else "")
    print("$", re.sub(r"\d{12}", "<account>", shown), flush=True)  # the account number stays off screen
    result = subprocess.run(args, text=True, input=input, capture_output=capture, env=env)
    if check and result.returncode != 0:
        sys.exit((result.stderr or "").strip() or f"failed: {args[0]} exited {result.returncode}")
    return (result.stdout or "").strip()


def aws(*args: str, region: str = REGION, capture: bool = True, check: bool = True) -> str:
    return run(["aws", *args, "--profile", PROFILE, "--region", region, "--output", "json"],
               capture=capture, check=check)  # fmt: skip


def account() -> str:
    return json.loads(aws("sts", "get-caller-identity"))["Account"]


def ensure_context() -> None:
    """The current kubectl context must be this EKS cluster (eksctl sets it); never the kind cluster."""
    current = run([tool("kubectl"), "config", "current-context"], capture=True, check=False)
    if f"{CLUSTER}.{REGION}.eksctl.io" not in current:
        sys.exit(f"kubectl's current context is {current!r}, not the EKS cluster: run "
                 f"`aws eks update-kubeconfig --name {CLUSTER} --region {REGION} --profile {PROFILE}`")  # fmt: skip


# --- the stages ---


def check() -> None:
    who = json.loads(aws("sts", "get-caller-identity"))
    print("signed in as", who["Arn"].split("/")[-2] if "/" in who["Arn"] else "(unknown role)")  # no account number
    print("plan:", json.loads(aws("freetier", "get-account-plan-state")).get("accountPlanType"))
    versions = json.loads(aws("eks", "describe-cluster-versions", "--default-only", check=False) or "{}")
    for v in versions.get("clusterVersions", []):
        print("EKS default version:", v.get("clusterVersion"), v.get("versionStatus"))
    supported = json.loads(aws("eks", "describe-cluster-versions", "--version-status", "STANDARD_SUPPORT"))
    print("in standard support:", sorted((v["clusterVersion"] for v in supported["clusterVersions"]), reverse=True))
    print("eksctl", run([tool("eksctl"), "version"], capture=True))
    docker = run(["docker", "version", "--format", "{{.Server.Version}}"], capture=True, check=False)
    print("docker", docker or "not running")
    clusters = json.loads(aws("eks", "list-clusters"))["clusters"]
    print("EKS clusters already in", REGION + ":", clusters or "none")


def cluster() -> None:
    run([tool("eksctl"), "create", "cluster", "-f", str(EKS / "cluster.yaml")])


def nodes() -> None:
    ensure_context()
    default = json.loads(run([tool("kubectl"), "get", "nodeclass", "default", "-o", "json"], capture=True))["spec"]
    subnets = json.loads(aws("ec2", "describe-subnets", "--filters",
                             f"Name=tag:alpha.eksctl.io/cluster-name,Values={CLUSTER}",
                             "Name=map-public-ip-on-launch,Values=true"))["Subnets"]  # fmt: skip
    if not subnets:
        sys.exit("no public subnets tagged for the cluster: check the VPC eksctl made")
    filled = (
        (EKS / "nodes.yaml").read_text(encoding="utf-8")
        .replace("${NODE_ROLE}", default["role"])
        .replace("${SUBNETS}", json.dumps([{"id": s["SubnetId"]} for s in subnets]))
        .replace("${SECURITY_GROUPS}", json.dumps(default["securityGroupSelectorTerms"]))
    )  # fmt: skip
    print(f"public subnets: {len(subnets)} ({', '.join(sorted(s['AvailabilityZone'] for s in subnets))})")
    run([tool("kubectl"), "apply", "-f", "-"], input=filled)
    # metrics-server for the autoscaler, only now that its pods will land on the public pool (eks/cluster.yaml)
    if not aws("eks", "describe-addon", "--cluster-name", CLUSTER, "--addon-name", "metrics-server", check=False):
        aws("eks", "create-addon", "--cluster-name", CLUSTER, "--addon-name", "metrics-server")
        print("metrics-server add-on created")


def image() -> None:
    registry = f"{account()}.dkr.ecr.{REGION}.amazonaws.com"
    exists = aws("ecr", "describe-repositories", "--repository-names", REPOSITORY, check=False)
    if not exists:
        aws("ecr", "create-repository", "--repository-name", REPOSITORY, "--image-scanning-configuration",
            "scanOnPush=true", "--tags", "Key=project,Value=ai-gateway", "Key=step,Value=11")  # fmt: skip
    run(["docker", "build", "--platform", "linux/amd64", "-t", f"ai-gateway:{TAG}", str(AI_GATEWAY)])
    password = aws("ecr", "get-login-password", capture=True)
    # Docker's own credential store on Windows refuses ECR's long tokens ("The stub received bad data"), and docker
    # picks it whenever its helper is on the PATH. So these three commands get a throwaway config folder and a PATH
    # without the helpers: the token sits only in that folder, deleted after the push. The password goes to docker on
    # standard input, never on the command line.
    docker = shutil.which("docker") or "docker"
    path = os.pathsep.join(
        d for d in os.environ.get("PATH", "").split(os.pathsep)
        if not any(pathlib.Path(d, f"docker-credential-{h}.exe").exists() for h in ("wincred", "desktop"))
    )  # fmt: skip
    with tempfile.TemporaryDirectory() as config:
        pathlib.Path(config, "config.json").write_text("{}", encoding="utf-8")
        env = os.environ | {"DOCKER_CONFIG": config, "PATH": path}
        run([docker, "login", "--username", "AWS", "--password-stdin", registry], input=password, capture=True, env=env)
        run([docker, "tag", f"ai-gateway:{TAG}", f"{registry}/{REPOSITORY}:{TAG}"], env=env)
        run([docker, "push", f"{registry}/{REPOSITORY}:{TAG}"], capture=True, env=env)
    print("pushed", f"<account>.dkr.ecr.{REGION}.amazonaws.com/{REPOSITORY}:{TAG}")


def install() -> None:
    ensure_context()
    run([sys.executable, str(AI_GATEWAY / "k8s" / "make_secret.py")])
    registry = f"{account()}.dkr.ecr.{REGION}.amazonaws.com"
    run([tool("helm"), "upgrade", "--install", "ai-gateway", str(AI_GATEWAY / "chart" / "ai-gateway"),
         "-f", str(EKS / "values.yaml"), "--set", f"image.repository={registry}/{REPOSITORY}",
         "--set-file", "routes=config/routes.yaml",
         "--set-file", "observability.alerts=observability/alerts.yml",
         "--set-file", "observability.drift=observability/drift.yml",
         "--set-file", "observability.dashboard=observability/grafana/dashboards/gateway.json",
         "--wait", "--timeout", "15m"])  # fmt: skip
    run([tool("kubectl"), "get", "pods,nodes", "-o", "wide"])


STAGES = {"check": check, "cluster": cluster, "nodes": nodes, "image": image, "install": install}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("stage", choices=STAGES)
    os.chdir(AI_GATEWAY)  # the chart's --set-file paths are relative to ai-gateway/
    os.environ["AWS_PROFILE"] = PROFILE  # for eksctl, kubectl and helm (eksctl --dry-run refuses --profile)
    STAGES[parser.parse_args().stage]()


if __name__ == "__main__":
    main()
