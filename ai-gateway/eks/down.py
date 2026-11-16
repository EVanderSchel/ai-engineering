"""Removes everything eks/up.py made (step 11), then checks that nothing billable is left.

    uv run python eks/down.py            # about 10-15 minutes
    uv run python eks/down.py --check    # only the check: what's still there

The order matters. The chart and the database's volume claim go first, so the EBS volume is deleted by the cluster
that made it. Then metrics-server (its pods would otherwise pull nodes into the private subnets), then the public node
pool, so Auto Mode stops its nodes while it still can. Then eksctl deletes the
cluster and its VPC (their CloudFormation stacks), and the ECR repository goes last.

The check looks for each kind of thing that costs money and could outlive a cluster: the cluster itself, EC2
instances, EBS volumes, load balancers, network interfaces, Elastic IPs, NAT gateways, CloudFormation stacks, the ECR
repository, and CloudWatch log groups (in us-east-2, and in us-east-1 where global services log).
"""

import argparse
import json
import os
import sys

from up import CLUSTER, EKS, PROFILE, REGION, REPOSITORY, aws, ensure_context, run, tool


def teardown() -> None:
    clusters = json.loads(aws("eks", "list-clusters"))["clusters"]
    if CLUSTER in clusters:
        # a failed `eksctl delete cluster` removes the cluster from kubectl's config before it fails: put it back
        run([tool("eksctl"), "utils", "write-kubeconfig", "--cluster", CLUSTER, "--region", REGION])
        ensure_context()
        kubectl = tool("kubectl")
        run([tool("helm"), "uninstall", "ai-gateway", "--wait", "--ignore-not-found"])
        # helm leaves a StatefulSet's volume claims behind; deleting them deletes the EBS volume (reclaimPolicy Delete)
        run([kubectl, "delete", "pvc", "--all", "--wait=true", "--timeout=5m"])
        # metrics-server before the node pool. Left running, its evicted pods made Auto Mode's built-in pool start
        # nodes in the private subnets (which never join), and its PodDisruptionBudget then kept the last public node
        # from draining: the first teardown stuck there for 10 minutes (README, step 11 stage 4).
        if aws("eks", "describe-addon", "--cluster-name", CLUSTER, "--addon-name", "metrics-server", check=False):
            aws("eks", "delete-addon", "--cluster-name", CLUSTER, "--addon-name", "metrics-server")
            aws("eks", "wait", "addon-deleted", "--cluster-name", CLUSTER, "--addon-name", "metrics-server")
        run([kubectl, "delete", "nodepool", "public", "--ignore-not-found", "--wait=true", "--timeout=10m"])
        run([kubectl, "delete", "nodeclass", "public", "--ignore-not-found", "--wait=true", "--timeout=10m"])
        run([tool("eksctl"), "delete", "cluster", "-f", str(EKS / "cluster.yaml"), "--wait"])
    else:
        print("no cluster named", CLUSTER)
    if aws("ecr", "describe-repositories", "--repository-names", REPOSITORY, check=False):
        aws("ecr", "delete-repository", "--repository-name", REPOSITORY, "--force")
        print("deleted the ECR repository")


def leftovers() -> list[str]:
    """Everything billable still tagged for, or named after, the cluster. Empty means nothing is left."""
    found = []
    tag = f"Name=tag:eks:eks-cluster-name,Values={CLUSTER}"

    def query(*args: str, key: str, region: str = REGION) -> list:
        out = aws(*args, region=region, check=False)
        return json.loads(out).get(key, []) if out else []

    if CLUSTER in query("eks", "list-clusters", key="clusters"):
        found.append(f"EKS cluster {CLUSTER}")
    for r in query("ec2", "describe-instances", "--filters", tag,
                   "Name=instance-state-name,Values=pending,running,stopping,stopped", key="Reservations"):  # fmt: skip
        found += [f"EC2 instance {i['InstanceId']}" for i in r["Instances"]]
    volumes = query("ec2", "describe-volumes", "--filters", f"Name=tag:KubernetesCluster,Values={CLUSTER}",
                    key="Volumes") + query("ec2", "describe-volumes", "--filters", tag, key="Volumes")  # fmt: skip
    found += sorted({f"EBS volume {v['VolumeId']}" for v in volumes})
    eksctl_tag = f"Name=tag:alpha.eksctl.io/cluster-name,Values={CLUSTER}"
    vpcs = [v["VpcId"] for v in query("ec2", "describe-vpcs", "--filters", eksctl_tag, key="Vpcs")]
    found += [f"VPC {v}" for v in vpcs]
    for vpc in vpcs:
        found += [
            f"network interface {n['NetworkInterfaceId']}"
            for n in query(
                "ec2", "describe-network-interfaces", "--filters", f"Name=vpc-id,Values={vpc}", key="NetworkInterfaces"
            )
        ]
        found += [f"NAT gateway {n['NatGatewayId']}" for n in query(
            "ec2", "describe-nat-gateways", "--filter", f"Name=vpc-id,Values={vpc}", key="NatGateways")
            if n["State"] not in ("deleted", "deleting")]  # fmt: skip
    addresses = query("ec2", "describe-addresses", "--filters", eksctl_tag, key="Addresses")
    found += [f"Elastic IP {a['PublicIp']}" for a in addresses]
    for lb in query("elbv2", "describe-load-balancers", key="LoadBalancers"):
        if lb.get("VpcId") in vpcs:
            found.append(f"load balancer {lb['LoadBalancerName']}")
    found += [f"CloudFormation stack {s['StackName']}" for s in query(
        "cloudformation", "list-stacks", "--stack-status-filter", "CREATE_COMPLETE", "CREATE_IN_PROGRESS",
        "UPDATE_COMPLETE", "DELETE_IN_PROGRESS", "DELETE_FAILED", "ROLLBACK_COMPLETE", key="StackSummaries")
        if s["StackName"].startswith(f"eksctl-{CLUSTER}-")]  # fmt: skip
    if aws("ecr", "describe-repositories", "--repository-names", REPOSITORY, check=False):
        found.append(f"ECR repository {REPOSITORY}")
    for region in (REGION, "us-east-1"):
        found += [f"log group {g['logGroupName']} ({region})" for g in query(
            "logs", "describe-log-groups", "--log-group-name-prefix", f"/aws/eks/{CLUSTER}", key="logGroups",
            region=region)]  # fmt: skip
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="only list what's left")
    os.environ["AWS_PROFILE"] = PROFILE  # for eksctl and kubectl's token helper
    if not parser.parse_args().check:
        teardown()
    left = leftovers()
    print("\n".join(["still there:", *(f"- {item}" for item in left)]) if left else "nothing left: all removed")
    sys.exit(1 if left else 0)


if __name__ == "__main__":
    main()
