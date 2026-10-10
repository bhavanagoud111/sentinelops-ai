variable "name" {
  type = string
}

variable "kubernetes_version" {
  type    = string
  default = "1.34"
}

variable "private_subnet_ids" {
  type = list(string)
}

# Empty = private endpoint only (reach it via VPN/bastion). Never 0.0.0.0/0.
variable "public_access_cidrs" {
  type    = list(string)
  default = []
  validation {
    condition     = !contains(var.public_access_cidrs, "0.0.0.0/0")
    error_message = "Do not expose the EKS API to the whole internet."
  }
}

variable "node_instance_types" {
  type    = list(string)
  default = ["t3.medium"]
}

# SPOT roughly halves node cost for a demo; ON_DEMAND for anything that must stay up.
variable "node_capacity_type" {
  type    = string
  default = "ON_DEMAND"
}

variable "node_count" {
  type    = number
  default = 2
}

variable "tags" {
  type    = map(string)
  default = {}
}
