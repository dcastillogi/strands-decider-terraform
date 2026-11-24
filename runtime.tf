# Runtime updates are Terraform-managed SSM documents and associations. No SSH
# or manual commands are needed; source/config changes update the document and
# re-run its association, without replacing the instance.
locals {
  runtime_files = ["decider_server.py", "inference_pool.py", "batch_engine.py", "batch_pool.py", "requirements.txt", "install.sh"]
  runtime_deploy = templatefile("${path.module}/runtime/deploy.sh.tftpl", {
    payloads         = { for name in local.runtime_files : name => base64gzip(file("${path.module}/runtime/${name}")) }
    model_id         = var.model_id
    workers          = var.inference_workers
    queue_size       = var.inference_queue_size
    torch_threads    = var.torch_threads
    batching         = tostring(var.inference_batching)
    max_batch        = var.inference_max_batch
    batch_wait_ms    = var.inference_batch_wait_ms
    max_batch_tokens = var.inference_max_batch_tokens
    model_processes  = var.model_processes
  })
}

resource "aws_ssm_document" "runtime" {
  name            = "${var.name}-runtime"
  document_type   = "Command"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Install the Terraform-managed Strands Decider CUDA runtime"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "deployRuntime"
      inputs = {
        timeoutSeconds = "3600"
        runCommand     = [local.runtime_deploy]
      }
    }]
  })
}

resource "aws_ssm_association" "runtime" {
  name                             = aws_ssm_document.runtime.name
  document_version                 = aws_ssm_document.runtime.latest_version
  association_name                 = "${var.name}-runtime"
  wait_for_success_timeout_seconds = 3600
  targets {
    key    = "InstanceIds"
    values = [aws_instance.decider.id]
  }
  depends_on = [aws_eip_association.decider]
}
