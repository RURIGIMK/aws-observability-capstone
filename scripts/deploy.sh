#!/usr/bin/env bash
# Deploy / test / tear down the Duka observability stack. Run from AWS CloudShell in the repo root.
#   ./scripts/deploy.sh up <oncall-email>   foundation -> (image build || RDS) -> web -> app -> observability
#   ./scripts/deploy.sh down                delete everything in reverse order, then verify
set -euo pipefail
P=${PROJECT:-duka}
REPO_URL=${REPO_URL:-https://github.com/RURIGIMK/aws-observability-capstone.git}
cd "$(dirname "$0")/.."

deploy() { aws cloudformation deploy --no-fail-on-empty-changeset --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --stack-name "$1" --template-file "$2" "${@:3}"; echo "OK $1"; }
out() { aws cloudformation describe-stacks --stack-name "$1" --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text; }

build_image() {
  local id; id=$(aws codebuild start-build --project-name "$P-api-build" --query build.id --output text)
  echo "image build $id"
  while true; do
    s=$(aws codebuild batch-get-builds --ids "$id" --query 'builds[0].buildStatus' --output text)
    [ "$s" = IN_PROGRESS ] || break; sleep 10
  done
  echo "image build: $s"; [ "$s" = SUCCEEDED ]
}

case "${1:-}" in
  up|base)
    [ "$1" = up ] && EMAIL=${2:?usage: up <oncall-email>}
    deploy "$P-foundation" infrastructure/01-foundation.yaml --parameter-overrides Project="$P" RepositoryUrl="$REPO_URL"
    deploy "$P-data" infrastructure/02-data.yaml --parameter-overrides Project="$P" & DATA=$!
    build_image & BUILD=$!
    wait $BUILD
    deploy "$P-web" infrastructure/03-web.yaml --parameter-overrides Project="$P" & WEB=$!
    wait $DATA
    deploy "$P-app" infrastructure/04-app.yaml --parameter-overrides Project="$P"
    wait $WEB
    [ "$1" = base ] && { echo "base tiers ready; run: $0 observability <email>"; exit 0; }
    "$0" observability "$EMAIL" ;;
  observability)
    EMAIL=${2:?usage: observability <oncall-email>}
    python3 scripts/render.py
    deploy "$P-observability" infrastructure/05-observability.yaml --parameter-overrides Project="$P" OnCallEmail="$EMAIL"
    echo "App: http://$(out "$P-foundation" LoadBalancerDNS)/"
    out "$P-observability" DashboardUrl ;;
  down)
    BUCKET=$(out "$P-foundation" LogArchiveBucketName 2>/dev/null || true)
    for s in observability app web data; do
      aws cloudformation delete-stack --stack-name "$P-$s"
      aws cloudformation wait stack-delete-complete --stack-name "$P-$s"; echo "deleted $P-$s"
    done
    if [ -n "$BUCKET" ] && [ "$BUCKET" != None ]; then aws s3 rm "s3://$BUCKET" --recursive --quiet || true; fi
    aws cloudformation delete-stack --stack-name "$P-foundation"
    aws cloudformation wait stack-delete-complete --stack-name "$P-foundation"; echo "deleted $P-foundation"
    # Log groups that AWS services create on their own (not owned by any stack)
    for g in $(aws logs describe-log-groups --query "logGroups[].logGroupName" --output text | tr '\t' '\n' \
               | grep -E "^/aws/(ecs/containerinsights/$P|rds/instance/$P|codebuild/$P|lambda/$P)|^RDSOSMetrics$|^/codebuild/$P" ); do
      aws logs delete-log-group --log-group-name "$g" && echo "deleted log group $g"
    done
    ./scripts/deploy.sh verify ;;
  verify)
    echo "--- anything left? (all should be empty)"
    aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE ROLLBACK_COMPLETE DELETE_FAILED \
      --query "StackSummaries[?starts_with(StackName,'$P-')].[StackName,StackStatus]" --output text
    aws ec2 describe-instances --filters Name=tag:Tier,Values=web Name=instance-state-name,Values=pending,running,stopping,stopped --query 'Reservations[].Instances[].InstanceId' --output text
    aws rds describe-db-instances --query "DBInstances[?starts_with(DBInstanceIdentifier,'$P')].DBInstanceIdentifier" --output text
    aws rds describe-db-snapshots --query "DBSnapshots[?starts_with(DBInstanceIdentifier,'$P')].DBSnapshotIdentifier" --output text
    aws elbv2 describe-load-balancers --query "LoadBalancers[?starts_with(LoadBalancerName,'$P')].LoadBalancerName" --output text
    aws ecs list-clusters --query "clusterArns[?contains(@,'$P')]" --output text
    aws ecr describe-repositories --query "repositories[?starts_with(repositoryName,'$P')].repositoryName" --output text 2>/dev/null
    aws firehose list-delivery-streams --query "DeliveryStreamNames[?starts_with(@,'$P')]" --output text
    aws s3api list-buckets --query "Buckets[?starts_with(Name,'$P')].Name" --output text
    aws cloudwatch describe-alarms --alarm-name-prefix "$P-" --query 'MetricAlarms[].AlarmName' --output text
    aws ec2 describe-addresses --query 'Addresses[].PublicIp' --output text
    echo "--- end" ;;
  *) echo "usage: $0 up <email> | base | observability <email> | down | verify"; exit 1 ;;
esac
