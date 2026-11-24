#!/usr/bin/env bash
# Usage: scripts/loadgen.sh up|down|status
#
# A temporary CPU instance in the decider's subnet, for benchmarks that should not include the
# client's own network (see scripts/benchmark.py). It is created with the AWS CLI, outside the
# Terraform state, and `down` deletes everything `up` created. Credentials and the region come
# from the standard AWS chain and the Terraform outputs.
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=${LOADGEN_NAME:-strands-decider-loadgen}
TYPE=${LOADGEN_INSTANCE_TYPE:-c7i.2xlarge}
SECRET=$(terraform output -raw api_key_secret_arn)
DECIDER=$(terraform output -raw instance_id)
export AWS_REGION=$(cut -d: -f4 <<<"$SECRET")

decider_sg() {
  aws ec2 describe-instances --instance-ids "$DECIDER" \
    --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text
}
loadgen_sg() {
  aws ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" \
    --query 'SecurityGroups[0].GroupId' --output text
}
instance() {
  aws ec2 describe-instances --filters "Name=tag:Name,Values=$NAME" \
    "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text
}

up() {
  if [ -n "$(instance)" ]; then echo "already running: $(instance)"; return; fi
  read -r SUBNET VPC < <(aws ec2 describe-instances --instance-ids "$DECIDER" \
    --query 'Reservations[0].Instances[0].[SubnetId,VpcId]' --output text)

  if ! aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
    aws iam create-role --role-name "$NAME" --assume-role-policy-document \
      '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
    aws iam attach-role-policy --role-name "$NAME" \
      --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
    aws iam create-instance-profile --instance-profile-name "$NAME" >/dev/null
    aws iam add-role-to-instance-profile --instance-profile-name "$NAME" --role-name "$NAME"
  fi
  aws iam put-role-policy --role-name "$NAME" --policy-name read-api-key --policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":\"$SECRET\"}]}"

  SG=$(loadgen_sg)
  if [ "$SG" = "None" ]; then
    SG=$(aws ec2 create-security-group --group-name "$NAME" --vpc-id "$VPC" \
      --description "Temporary Strands Decider load generator" --query GroupId --output text)
  fi
  aws ec2 authorize-security-group-ingress --group-id "$(decider_sg)" --ip-permissions \
    "IpProtocol=tcp,FromPort=8080,ToPort=8080,UserIdGroupPairs=[{GroupId=$SG,Description=$NAME}]" \
    >/dev/null 2>&1 || true

  AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
    --query Parameter.Value --output text)
  USER_DATA=$(printf '%s\n' '#!/bin/bash' 'dnf install -y python3.11' \
    'echo "* - nofile 65536" > /etc/security/limits.d/90-loadgen.conf' \
    'sysctl -w net.ipv4.ip_local_port_range="10240 65535"')
  # The new instance profile can take a few seconds to become usable by EC2.
  for attempt in 1 2 3 4 5 6; do
    ID=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" --subnet-id "$SUBNET" \
      --security-group-ids "$SG" --iam-instance-profile "Name=$NAME" --user-data "$USER_DATA" \
      --metadata-options HttpTokens=required,HttpEndpoint=enabled \
      --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3,Encrypted=true}' \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
      --query 'Instances[0].InstanceId' --output text 2>/dev/null) && break
    sleep 10
  done
  [ -n "${ID:-}" ] || { echo 'run-instances failed' >&2; exit 1; }
  echo "launched $ID ($TYPE in $SUBNET); waiting for SSM"
  for attempt in $(seq 60); do
    if [ "$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$ID" \
        --query 'InstanceInformationList[0].PingStatus' --output text)" = Online ]; then
      echo "ready: $ID"; return
    fi
    sleep 10
  done
  echo "SSM agent did not come online" >&2; exit 1
}

down() {
  ID=$(instance)
  if [ -n "$ID" ]; then
    aws ec2 terminate-instances --instance-ids $ID >/dev/null
    aws ec2 wait instance-terminated --instance-ids $ID
    echo "terminated $ID"
  fi
  SG=$(loadgen_sg)
  if [ "$SG" != "None" ]; then
    aws ec2 revoke-security-group-ingress --group-id "$(decider_sg)" --ip-permissions \
      "IpProtocol=tcp,FromPort=8080,ToPort=8080,UserIdGroupPairs=[{GroupId=$SG}]" >/dev/null 2>&1 || true
    aws ec2 delete-security-group --group-id "$SG"
    echo "deleted $SG"
  fi
  if aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
    aws iam remove-role-from-instance-profile --instance-profile-name "$NAME" --role-name "$NAME" || true
    aws iam delete-instance-profile --instance-profile-name "$NAME" || true
    aws iam delete-role-policy --role-name "$NAME" --policy-name read-api-key || true
    aws iam detach-role-policy --role-name "$NAME" \
      --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore || true
    aws iam delete-role --role-name "$NAME"
    echo "deleted role $NAME"
  fi
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status) instance ;;
  *) echo "usage: $0 up|down|status" >&2; exit 2 ;;
esac
