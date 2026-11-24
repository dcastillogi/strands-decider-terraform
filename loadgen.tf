# Optional in-region load generator. Benchmarks run from a CPU instance in the decider's
# subnet and call Envoy's private address, so the client's own network does not enter the
# measurements. scripts/benchmark.py drives it through SSM; results go through a private bucket.

data "aws_ssm_parameter" "loadgen_ami" {
  count = var.load_generator ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_s3_bucket" "results" {
  count         = var.load_generator ? 1 : 0
  bucket_prefix = "${var.name}-results-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "results" {
  count                   = var.load_generator ? 1 : 0
  bucket                  = aws_s3_bucket.results[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "results" {
  count  = var.load_generator ? 1 : 0
  bucket = aws_s3_bucket.results[0].id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = 14
    }
  }
}

resource "aws_security_group" "loadgen" {
  count       = var.load_generator ? 1 : 0
  name_prefix = "${var.name}-loadgen-"
  description = "In-region load generator for Strands Decider"
  vpc_id      = var.vpc_id
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "loadgen" {
  count             = var.load_generator ? 1 : 0
  security_group_id = aws_security_group.loadgen[0].id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "envoy_from_loadgen" {
  count                        = var.load_generator ? 1 : 0
  security_group_id            = aws_security_group.envoy.id
  description                  = "Envoy from the in-region load generator"
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
  referenced_security_group_id = aws_security_group.loadgen[0].id
}

resource "aws_iam_role" "loadgen" {
  count       = var.load_generator ? 1 : 0
  name_prefix = "${var.name}-loadgen-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "loadgen_ssm" {
  count      = var.load_generator ? 1 : 0
  role       = aws_iam_role.loadgen[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "loadgen" {
  count = var.load_generator ? 1 : 0
  name  = "benchmark"
  role  = aws_iam_role.loadgen[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.api_key.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.results[0].arn}/*"
      },
    ]
  })
}

# The decider uploads its resource telemetry for the benchmark to correlate.
resource "aws_iam_role_policy" "decider_telemetry" {
  count = var.load_generator ? 1 : 0
  name  = "benchmark-telemetry"
  role  = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:PutObject"
      Resource = "${aws_s3_bucket.results[0].arn}/telemetry/*"
    }]
  })
}

resource "aws_iam_instance_profile" "loadgen" {
  count       = var.load_generator ? 1 : 0
  name_prefix = "${var.name}-loadgen-"
  role        = aws_iam_role.loadgen[0].name
}

resource "aws_instance" "loadgen" {
  count                  = var.load_generator ? 1 : 0
  ami                    = data.aws_ssm_parameter.loadgen_ami[0].insecure_value
  instance_type          = var.load_generator_instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [aws_security_group.loadgen[0].id]
  iam_instance_profile   = aws_iam_instance_profile.loadgen[0].name

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
    encrypted   = true
  }

  user_data                   = <<-EOT
    #!/bin/bash
    set -euo pipefail
    dnf install -y python3.11
    # Many concurrent client connections.
    echo '* - nofile 65536' > /etc/security/limits.d/90-loadgen.conf
    sysctl -w net.ipv4.ip_local_port_range="10240 65535"
  EOT
  user_data_replace_on_change = true

  tags = {
    Name = "${var.name}-loadgen"
  }

  depends_on = [aws_iam_role_policy.loadgen, aws_iam_role_policy_attachment.loadgen_ssm]
}
