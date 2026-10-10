output "cluster_name" {
  value = module.eks.cluster_name
}

output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "postgres_endpoint" {
  value = module.rds.endpoint
}

output "postgres_master_secret_arn" {
  value = module.rds.master_user_secret_arn
}

output "ecr_repositories" {
  value = module.ecr.repository_urls
}

output "kafka_bootstrap" {
  value = var.kafka_mode == "msk" ? module.msk[0].bootstrap_brokers : "redpanda.sentinelops-system.svc:9092 (in-cluster)"
}
