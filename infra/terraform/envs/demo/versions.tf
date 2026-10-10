terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  # State backend is deliberately not configured here. For shared use, add an S3
  # backend with locking (use_lockfile = true) in a backend.tf that is not committed
  # with real bucket names.
}

provider "aws" {
  region = var.region
  default_tags {
    tags = local.tags
  }
}
