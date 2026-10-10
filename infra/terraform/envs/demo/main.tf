# SentinelOps demo environment on AWS: VPC, EKS, RDS Postgres, ECR, optional MSK, and
# a monthly cost budget. Written and validated only — see ../../README.md before any
# `terraform apply`; it creates billable resources.
locals {
  tags = {
    Project     = "sentinelops"
    Environment = "demo"
    ManagedBy   = "terraform"
  }
}

module "network" {
  source = "../../modules/network"
  name   = var.name
  tags   = local.tags
}

module "eks" {
  source              = "../../modules/eks"
  name                = var.name
  private_subnet_ids  = module.network.private_subnet_ids
  public_access_cidrs = var.eks_public_access_cidrs
  node_capacity_type  = var.node_capacity_type
  tags                = local.tags
}

module "rds" {
  source                   = "../../modules/rds"
  name                     = var.name
  vpc_id                   = module.network.vpc_id
  private_subnet_ids       = module.network.private_subnet_ids
  client_security_group_id = module.eks.cluster_security_group_id
  tags                     = local.tags
}

module "ecr" {
  source = "../../modules/ecr"
  prefix = "sentinelops"
  tags   = local.tags
}

module "msk" {
  source                   = "../../modules/msk"
  count                    = var.kafka_mode == "msk" ? 1 : 0
  name                     = var.name
  vpc_id                   = module.network.vpc_id
  private_subnet_ids       = module.network.private_subnet_ids
  client_security_group_id = module.eks.cluster_security_group_id
  tags                     = local.tags
}

resource "aws_budgets_budget" "monthly" {
  count        = var.budget_alert_email == "" ? 0 : 1
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$sentinelops"]
  }

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value == 100 ? "FORECASTED" : "ACTUAL"
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }
}
