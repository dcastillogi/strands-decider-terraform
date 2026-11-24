data "aws_region" "current" {}

data "aws_ssm_parameter" "ami" {
  name = var.image_ssm_parameter
}

locals {
  hf_secret_arns = compact([aws_secretsmanager_secret.api_key.arn, var.hf_token_secret_arn])
}

# --- API key -----------------------------------------------------------------

resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "api_key" {
  name_prefix = "${var.name}-api-key-"
  description = "API key for the ${var.name} decider endpoint (x-api-key header)"
}

resource "aws_secretsmanager_secret_version" "api_key" {
  secret_id     = aws_secretsmanager_secret.api_key.id
  secret_string = jsonencode({ key = random_password.api_key.result })
}

# --- IAM ---------------------------------------------------------------------

resource "aws_iam_role" "instance" {
  name_prefix = "${var.name}-"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "cloudwatch_agent" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_iam_role_policy" "read_secrets" {
  name = "read-secrets"
  role = aws_iam_role.instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = local.hf_secret_arns
    }]
  })
}

resource "aws_iam_instance_profile" "instance" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.instance.name
}

# --- Network -----------------------------------------------------------------

resource "aws_security_group" "envoy" {
  name_prefix = "${var.name}-"
  description = "Envoy listener for Strands Decider"
  vpc_id      = var.vpc_id

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "envoy" {
  security_group_id = aws_security_group.envoy.id
  description       = "Envoy"
  ip_protocol       = "tcp"
  from_port         = 8080
  to_port           = 8080
  cidr_ipv4         = var.allowed_cidr
}

# Package, model and container downloads.
resource "aws_vpc_security_group_egress_rule" "envoy" {
  security_group_id = aws_security_group.envoy.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# --- Instance ----------------------------------------------------------------

resource "aws_instance" "decider" {
  ami                    = data.aws_ssm_parameter.ami.insecure_value
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [aws_security_group.envoy.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region         = data.aws_region.current.region
    model_id       = var.model_id
    envoy_image    = var.envoy_image
    api_key_secret = aws_secretsmanager_secret.api_key.arn
    hf_secret_arn  = var.hf_token_secret_arn

  })
  user_data_replace_on_change = true

  tags = {
    Name = "${var.name}-decider"
  }

  # The boot script reads the secret and uses the role on first start.
  depends_on = [
    aws_secretsmanager_secret_version.api_key,
    aws_iam_role_policy.read_secrets,
    aws_iam_role_policy_attachment.ssm,
    aws_iam_role_policy_attachment.cloudwatch_agent,
  ]
}

resource "aws_eip" "decider" {
  domain = "vpc"
}

resource "aws_eip_association" "decider" {
  allocation_id = aws_eip.decider.id
  instance_id   = aws_instance.decider.id
}

# --- Optional readiness gate ---------------------------------------------------

resource "terraform_data" "wait_ready" {
  count = var.wait_for_ready ? 1 : 0

  triggers_replace = [aws_instance.decider.id, aws_ssm_document.runtime.latest_version, filesha256("${path.module}/scripts/wait-ready.py")]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "python3 \"${path.module}/scripts/wait-ready.py\""

    environment = {
      ASSOCIATION      = aws_ssm_association.runtime.association_id
      DOCUMENT_VERSION = aws_ssm_document.runtime.latest_version
      INSTANCE         = aws_instance.decider.id
      ENDPOINT         = "http://${aws_eip.decider.public_ip}:8080/v1/systemone"
      SECRET           = aws_secretsmanager_secret.api_key.arn
      REGION           = data.aws_region.current.region
    }
  }

  depends_on = [aws_ssm_association.runtime]
}
