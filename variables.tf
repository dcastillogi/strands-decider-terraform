variable "name" {
  type        = string
  default     = "strands-decider"
  description = "Name prefix for all resources."
}

variable "instance_type" {
  type        = string
  default     = "g6.xlarge"
  description = "GPU instance type."

  validation {
    condition     = contains(["g6.xlarge", "g6.2xlarge", "g5.xlarge", "g5.2xlarge", "g6e.xlarge"], var.instance_type)
    error_message = "instance_type must be one of g6.xlarge, g6.2xlarge, g5.xlarge, g5.2xlarge, g6e.xlarge."
  }
}

variable "allowed_cidr" {
  type        = string
  description = "CIDR allowed to reach Envoy on port 8080. No default on purpose."

  validation {
    condition     = can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/[0-9]{1,2}$", var.allowed_cidr))
    error_message = "Must be an IPv4 CIDR such as 203.0.113.10/32."
  }
}

variable "vpc_id" {
  type        = string
  description = "VPC for the instance."
}

variable "subnet_id" {
  type        = string
  description = "Public subnet in the VPC above (an Elastic IP is attached)."
}

variable "model_id" {
  type        = string
  default     = "StrandsAgents/strands-decider-E2B-gemma4-v2-2611"
  description = "Hugging Face repo id of a Strands Decider v2 model checkpoint (decision_config.json)."
}

variable "hf_token_secret_arn" {
  type        = string
  default     = ""
  description = "Optional Secrets Manager ARN holding a plain-text Hugging Face token."
}

variable "root_volume_size" {
  type        = number
  default     = 100
  description = "Root volume size in GiB."

  validation {
    condition     = var.root_volume_size >= 60 && var.root_volume_size <= 1000
    error_message = "root_volume_size must be between 60 and 1000 GiB."
  }
}

variable "image_ssm_parameter" {
  type        = string
  default     = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id"
  description = "SSM parameter resolving to the Deep Learning Base AMI (NVIDIA driver, CUDA, Docker)."
}

variable "envoy_image" {
  type        = string
  default     = "envoyproxy/envoy:v1.31.2"
  description = "Envoy container image (pin an exact tag)."
}

variable "wait_for_ready" {
  type        = bool
  default     = true
  description = <<-EOT
    Block `terraform apply` until the model answers through Envoy (up to ~45 min).
    Requires the machine running Terraform to be inside allowed_cidr and to have
    the aws CLI, curl and python3.
  EOT
}

variable "inference_workers" {
  type        = number
  default     = 1
  description = "Inference workers when batching is disabled. One avoids shared token-offset races."
  validation {
    condition     = var.inference_workers == 1
    error_message = "Use one model executor; concurrent access races on the upstream engine's temporary token offsets."
  }
}

variable "inference_queue_size" {
  type        = number
  default     = 256
  description = "Admission buffer beyond the maximum batch per process; full capacity returns HTTP 503 with Retry-After."
  validation {
    condition     = var.inference_queue_size == floor(var.inference_queue_size) && var.inference_queue_size >= 0 && var.inference_queue_size <= 256
    error_message = "inference_queue_size must be an integer between 0 and 256."
  }
}

variable "inference_batching" {
  type        = bool
  default     = true
  description = "Group distinct HTTP requests into one GPU batch using a single model executor."
}

variable "model_processes" {
  type        = number
  default     = 1
  description = "Independent model processes sharing the GPU; each owns its engine and bounded batch queue."
  validation {
    condition     = contains([1, 2], var.model_processes)
    error_message = "model_processes must be one or two on this single-GPU deployment."
  }
}

variable "inference_max_batch" {
  type    = number
  default = 8
  validation {
    condition     = contains([1, 2, 4, 8, 16, 32, 64, 128], var.inference_max_batch)
    error_message = "inference_max_batch must be a power of two from 1 to 128."
  }
}

variable "inference_batch_wait_ms" {
  type    = number
  default = 2
  validation {
    condition     = var.inference_batch_wait_ms >= 0 && var.inference_batch_wait_ms <= 20
    error_message = "inference_batch_wait_ms must be between 0 and 20."
  }
}

variable "inference_max_batch_tokens" {
  type    = number
  default = 8192
  validation {
    condition     = var.inference_max_batch_tokens == floor(var.inference_max_batch_tokens) && var.inference_max_batch_tokens >= 4096 && var.inference_max_batch_tokens <= 32768
    error_message = "inference_max_batch_tokens must be an integer from 4096 to 32768."
  }
}

variable "torch_threads" {
  type        = number
  default     = 1
  description = "CPU threads per PyTorch operation, also used for OMP and MKL."
  validation {
    condition     = var.torch_threads == floor(var.torch_threads) && var.torch_threads >= 1 && var.torch_threads <= 4
    error_message = "torch_threads must be an integer between 1 and 4."
  }
}

variable "load_generator" {
  type        = bool
  default     = false
  description = "Create an in-region CPU instance and results bucket that scripts/benchmark.py uses to measure the endpoint over its private address."
}

variable "load_generator_instance_type" {
  type        = string
  default     = "c7i.2xlarge"
  description = "Instance type of the optional load generator."
}
