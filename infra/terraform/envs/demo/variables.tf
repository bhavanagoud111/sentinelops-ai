variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name" {
  type    = string
  default = "sentinelops-demo"
}

# CIDRs allowed to reach the EKS API publicly (e.g. your IP/32). Empty = private only.
variable "eks_public_access_cidrs" {
  type    = list(string)
  default = []
}

variable "node_capacity_type" {
  type    = string
  default = "ON_DEMAND"
}

# in_cluster: Redpanda runs inside EKS (cheapest). msk: provisioned Amazon MSK.
variable "kafka_mode" {
  type    = string
  default = "in_cluster"
  validation {
    condition     = contains(["in_cluster", "msk"], var.kafka_mode)
    error_message = "kafka_mode must be in_cluster or msk."
  }
}

variable "monthly_budget_usd" {
  type    = number
  default = 250
}

# Budget alerts go here. Empty disables the budget resource.
variable "budget_alert_email" {
  type    = string
  default = ""
}
