output "endpoint" {
  description = "Decider endpoint (send the x-api-key header)"
  value       = "http://${aws_eip.decider.public_ip}:8080/v1/systemone"
}

output "api_key_secret_arn" {
  description = "Secret holding the API key (JSON field \"key\")"
  value       = aws_secretsmanager_secret.api_key.arn
}

output "instance_id" {
  value = aws_instance.decider.id
}

output "ssm_session_command" {
  value = "aws ssm start-session --region ${data.aws_region.current.region} --target ${aws_instance.decider.id}"
}

output "private_endpoint" {
  description = "Decider endpoint on the instance's private address (reachable from the load generator)"
  value       = "http://${aws_instance.decider.private_ip}:8080/v1/systemone"
}

output "load_generator_instance_id" {
  value = var.load_generator ? aws_instance.loadgen[0].id : null
}

output "results_bucket" {
  value = var.load_generator ? aws_s3_bucket.results[0].bucket : null
}
