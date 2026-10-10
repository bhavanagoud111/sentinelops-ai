output "endpoint" {
  value = aws_db_instance.this.endpoint
}

output "master_user_secret_arn" {
  description = "Secrets Manager secret holding the generated master password."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}
