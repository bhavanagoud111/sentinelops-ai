output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.this.endpoint
}

output "cluster_security_group_id" {
  description = "Security group EKS attaches to nodes and pods (used to allow DB/Kafka access)."
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}
