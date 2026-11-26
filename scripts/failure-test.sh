#!/usr/bin/env bash
# Mock failure test. Generates baseline traffic, then injects three incidents so every alarm and
# remediation path fires:
#   1. error storm   - /api/fault/error  -> 5xx > 5% on the ALB and ErrorCount > 10 in 5 min
#   2. slow queries  - /api/report?slow  -> DB slow query log + slow X-Ray subsegments
#   3. CPU burn      - /api/fault/cpu    -> ECS CPU > 80%
# Usage: ./scripts/failure-test.sh baseline|errors|slow|cpu|all
set -uo pipefail
P=${PROJECT:-duka}
ALB=$(aws cloudformation describe-stacks --stack-name "$P-foundation" \
      --query "Stacks[0].Outputs[?OutputKey=='LoadBalancerDNS'].OutputValue" --output text)
U="http://$ALB"
stamp() { date -u +%H:%M:%S; }

baseline() {  # healthy mixed traffic for N seconds
  local end=$(( $(date +%s) + ${1:-120} ))
  while [ "$(date +%s)" -lt $end ]; do
    curl -s -o /dev/null "$U/"; curl -s -o /dev/null "$U/api/orders"; curl -s -o /dev/null "$U/api/report"
    curl -s -o /dev/null -X POST -H 'content-type: application/json' -d '{"county":"Nairobi","amount_kes":1500}' "$U/api/orders"
    sleep 0.5
  done
}
errors() {
  echo "$(stamp) INCIDENT 1: error storm (payment gateway timeouts)"
  for i in $(seq 1 150); do curl -s -o /dev/null "$U/api/fault/error" & [ $((i % 15)) -eq 0 ] && wait; done; wait
  echo "$(stamp) error storm sent"
}
slow() {
  echo "$(stamp) INCIDENT 2: slow database queries"
  for i in $(seq 1 12); do curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" "$U/api/report?slow=3" & done; wait
  echo "$(stamp) slow queries sent"
}
cpu() {
  echo "$(stamp) INCIDENT 3: CPU burn on the API tasks (~4 min)"
  for round in 1 2 3 4; do
    for i in 1 2 3 4; do curl -s -o /dev/null --max-time 90 "$U/api/fault/cpu?seconds=55" & done; wait
    echo "$(stamp) cpu round $round done"
  done
}

case "${1:-all}" in
  baseline) baseline "${2:-120}" ;;
  errors) errors ;;
  slow) slow ;;
  cpu) cpu ;;
  all) baseline 120; errors; slow; baseline 60; cpu; baseline 60 ;;
esac
