# SentinelOps on AWS (optional, Phase 7)

**Status: written and validated only — never applied.** The core demo runs entirely
locally (Compose + kind); AWS is not required for anything in this repository.

```
envs/demo        root module: wires the modules, default tags, monthly budget alarm
modules/network  VPC, 2 public + 2 private subnets, 1 NAT gateway, default SG locked down
modules/eks      EKS (private endpoint, KMS-encrypted secrets, audit logs), managed node
                 group in private subnets, VPC CNI with NetworkPolicy enforcement
modules/rds      Postgres 17, private, SG from EKS only, encrypted, force SSL,
                 master password generated into Secrets Manager (not in state)
modules/ecr      immutable tags, scan on push, lifecycle (keep 20)
modules/msk      optional provisioned MSK (kafka_mode = "msk"); default is in-cluster Redpanda
```

## Validate (no AWS account needed)
```bash
make terraform-check   # terraform fmt -check, init -backend=false, validate
```

## Estimated monthly cost — `envs/demo` defaults
**These are estimates, not measured spend.** Computed as 730 h/month × AWS us-east-1
on-demand list prices as known when this was written; prices change, so check the
[AWS Pricing Calculator](https://calculator.aws/) before applying. Data transfer, NAT
data processing ($/GB), CloudWatch log ingestion and taxes are extra and usage-dependent.

| Item | Basis | ≈ USD/month |
|---|---|---|
| EKS control plane | $0.10/h | 73.00 |
| NAT gateway | $0.045/h (+ $/GB processed) | 32.85 |
| Public IPv4 (NAT EIP) | $0.005/h | 3.65 |
| 2 × t3.medium nodes, on-demand | $0.0416/h each | 60.74 |
| Node volumes 2 × 20 GB gp3 | $0.08/GB-month | 3.20 |
| RDS db.t4g.micro single-AZ | $0.016/h | 11.68 |
| RDS storage 20 GB gp3 | $0.115/GB-month | 2.30 |
| KMS key (EKS secrets) | $1/key-month | 1.00 |
| **Baseline** | | **≈ 188** |
| + ALB if the chart's ingress is enabled | $0.0225/h + LCU | + ≈ 16 |
| + MSK (`kafka_mode = "msk"`, 2 × kafka.t3.small, 2 × 20 GB) | $0.0456/h each + $0.10/GB-month | + ≈ 71 |

Levers: `node_capacity_type = "SPOT"` (nodes are the largest variable line), destroy
when not demoing (EKS + NAT bill hourly even when idle), keep `kafka_mode = "in_cluster"`.
`budget_alert_email` creates an AWS Budget (alerts at 50 % and 80 % actual, 100 %
forecast; default limit $250) filtered on the `Project=sentinelops` cost-allocation tag —
activate that tag in the Billing console or the filter matches nothing.

## Apply (only after deciding to spend)
```bash
cd infra/terraform/envs/demo
cp terraform.tfvars.example terraform.tfvars    # set your IP, email
terraform init && terraform plan -out tf.plan   # review every resource
terraform apply tf.plan
$(terraform output -raw kubeconfig_command)
```
Then push images to the ECR repositories, create the `sentinelops` Secret (database URL
built from the RDS endpoint + Secrets Manager password; see
`infra/helm/sentinelops/README.md`), and `helm install` with your ECR image values.
Before production use, apply the threat-model backlog (`docs/threat-model.md`) —
separate DB roles first.

## Teardown
```bash
helm uninstall sentinelops -n sentinelops-system   # removes any ALB created by ingress
cd infra/terraform/envs/demo && terraform destroy
```
`deletion_protection = false` and `skip_final_snapshot = true` (RDS) and
`force_delete = true` (ECR) are the demo defaults so destroy leaves nothing billable.
Afterwards check in the console: no EKS cluster, NAT gateway, Elastic IP, load balancer,
RDS instance/snapshot or MSK cluster remains in the region, and CloudWatch log group
`/aws/eks/sentinelops-demo/cluster` is deleted (log groups outlive clusters).
