#!/usr/bin/env bash
# Prints the evidence used in the report. Run after failure-test.sh.
#   insights   run the saved error-spike, error-rate and slow-query Logs Insights queries (last 40 min)
#   alarms     alarm state history (ALARM / OK transitions)
#   lambda     remediation Lambda decisions
#   tags       investigate=true tags written to the web EC2 instances
#   archive    objects delivered by Firehose to the S3 log archive
#   xray       slowest recent X-Ray traces and their SQL subsegments
set -uo pipefail
P=${PROJECT:-duka}
SINCE=$(( $(date +%s) - ${MINUTES:-40} * 60 ))
NOW=$(date +%s)

run_query() {  # $1 log group, $2 query
  local id; id=$(aws logs start-query --log-group-name "$1" --start-time "$SINCE" --end-time "$NOW" \
                 --query-string "$2" --query queryId --output text)
  for _ in $(seq 1 30); do
    s=$(aws logs get-query-results --query-id "$id" --query status --output text)
    [ "$s" = Complete ] && break; sleep 2
  done
  aws logs get-query-results --query-id "$id" --output json | python3 -c '
import json,sys
rows=json.load(sys.stdin)["results"]
if not rows: print("(no rows)"); sys.exit()
cols=[f["field"] for f in rows[0] if not f["field"].startswith("@ptr")]
print("  ".join(f"{c:<24}" for c in cols))
for r in rows[:15]:
    d={f["field"]:f["value"] for f in r}
    print("  ".join(f"{d.get(c,"")[:24]:<24}" for c in cols))'
}

case "${1:-}" in
  insights)
    for name in error-spikes error-rate-and-latency-per-minute db-slow-queries; do
      read -r group q < <(aws logs describe-query-definitions --query-definition-name-prefix "$P/$name" \
        --query 'queryDefinitions[0].[logGroupNames[0],queryString]' --output text | head -1)
      q=$(aws logs describe-query-definitions --query-definition-name-prefix "$P/$name" --query 'queryDefinitions[0].queryString' --output text)
      echo "=== saved query: $P/$name  ($group)"; run_query "$group" "$q"; echo
    done ;;
  alarms)
    aws cloudwatch describe-alarm-history --alarm-name-prefix "$P-" --history-item-type StateUpdate \
      --start-date "$(date -u -d "@$SINCE" +%FT%TZ)" --query 'AlarmHistoryItems[].[Timestamp,AlarmName,HistorySummary]' \
      --output text | sort | cut -c1-140 ;;
  lambda)
    aws logs filter-log-events --log-group-name "/aws/lambda/$P-remediation" --start-time $((SINCE*1000)) \
      --filter-pattern '"remediation complete"' --query 'events[].message' --output text | tr '\t' '\n' \
      | python3 -c '
import json,sys,re
for line in sys.stdin:
    m=re.search(r"(\{.*\})",line)
    if not m: continue
    d=json.loads(m.group(1))
    print(d["time"], d["alarm"]); [print("    ->",a) for a in d["automated_actions"]]; print("    -> notified SNS duka-oncall")' ;;
  tags)
    aws ec2 describe-instances --filters Name=tag:Tier,Values=web Name=instance-state-name,Values=running \
      --query 'Reservations[].Instances[].[InstanceId, Tags[?starts_with(Key,`investigate`)]|[].join(`=`,[Key,Value])]' --output text ;;
  archive)
    B=$(aws cloudformation describe-stacks --stack-name "$P-foundation" --query "Stacks[0].Outputs[?OutputKey=='LogArchiveBucketName'].OutputValue" --output text)
    aws s3 ls "s3://$B/logs/" --recursive --human-readable --summarize | tail -12
    K=$(aws s3 ls "s3://$B/logs/" --recursive | sort | tail -1 | awk '{print $4}')
    echo "--- sample from $K"; aws s3 cp "s3://$B/$K" - | gunzip | head -4 | cut -c1-200 ;;
  xray)
    aws xray get-trace-summaries --start-time "$SINCE" --end-time "$NOW" --filter-expression 'service("duka-api") AND responsetime > 2' \
      --query 'TraceSummaries[:5].[Id,ResponseTime,Http.HttpURL,Http.HttpStatus]' --output text ;;
  *) echo "usage: $0 insights|alarms|lambda|tags|archive|xray"; exit 1 ;;
esac
