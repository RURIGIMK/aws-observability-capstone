# Observability-as-a-Service: Short Report

**Workload:** Duka, a three-tier order app (EC2 web, then ECS Fargate API, then RDS MySQL) · **Region:** eu-west-1 · **Author:** Rurigi Kariuki · **Date:** 26 November 2026

## 1. Observability design

Each signal comes from the layer closest to the problem it diagnoses. Every signal lands in CloudWatch, so one dashboard, one alarm system and one query language cover all three tiers.

| Tier | Metrics | Logs | Traces |
|---|---|---|---|
| **Web**: 2x EC2 + Apache behind the ALB | CloudWatch Agent: `mem_used_percent`, `disk_used_percent`, CPU, swap (namespace `CWAgent`). ALB: requests, 5xx, p95 latency | Agent tails `/var/log/httpd/access_log`, now logging response time `%D`, and `error_log` into `/duka/web/httpd-*` | – |
| **App**: ECS Fargate API | AWS/ECS CPU and memory, Container Insights task count | awslogs into `/duka/app`, **one JSON line per request** (`level`, `path`, `status`, `latency_ms`, `trace_id`) | **X-Ray** daemon sidecar. Flask and PyMySQL are auto-instrumented, so every SQL statement is a timed subsegment |
| **Data**: RDS MySQL 8.4 | AWS/RDS connections, CPU, read/write latency. **Enhanced Monitoring** OS metrics every 60 s | **error** and **slowquery** logs exported (`slow_query_log=1`, `long_query_time=1`) | via the app's SQL subsegments |

**Derived metrics** come from metric filters on the logs. `Duka/App ErrorCount` counts lines where `$.level = "ERROR"`. `SlowRequestCount` counts lines where `latency_ms > 1000`. `Duka/Database SlowQueryCount` counts entries in the RDS slow log. These turn log events into numbers you can alarm on.

**Long-term archive.** Five subscription filters (app, httpd access and error, RDS error and slowquery) feed one **Kinesis Data Firehose** stream. It unzips the CloudWatch Logs batches, extracts the events, and writes date-partitioned, gzip-compressed files to S3. A lifecycle rule moves them to Standard-IA after 30 days and Glacier Instant Retrieval after 90, and deletes them after 400. CloudWatch keeps logs for 7 days for fast searching, and S3 holds the long, cheap audit trail. That fits the auditor requirement without paying CloudWatch storage prices for a year.

**Automated response.** Alarm state change goes to an EventBridge rule (`alarmName` prefix `duka-`, state `ALARM`), which runs the `duka-remediation` Lambda, which acts and then notifies the SNS `duka-oncall` topic:

| Alarm | Condition | Automated action |
|---|---|---|
| `duka-ecs-cpu-high` | ECS CPU > 80% for 2 of 2 minutes | Force a new ECS deployment, a rolling replacement of the hot tasks, then page on-call |
| `duka-alb-5xx-high` | (target 5xx + ALB 5xx) / requests > 5% (metric math) | Tag web EC2 instances `investigate=true`, restart the API, page on-call |
| `duka-app-error-spike` | `ErrorCount` > 10 in 5 min | Restart the API, tag the web tier, page on-call |
| `duka-rds-cpu-high` | RDS CPU > 80% for 3 minutes | Page only. A database is never restarted automatically |

A second EventBridge rule pages on-call directly when an ECS task stops unexpectedly (`EssentialContainerExited` or `TaskFailedToStart`), so a crash loop is noticed even if no alarm fires.

**Why these choices:**
- One Lambda with a playbook keyed on the alarm name keeps all response logic in one place. Adding a response means adding one entry.
- Restarts are safe because the ECS service keeps `minimumHealthyPercent: 100` and the circuit breaker is on.
- EC2 instances are tagged rather than terminated, so evidence is kept for the investigation.
- The Lambda's IAM policy only allows `CreateTags` on instances tagged `Tier=web`, so a bug can't tag the wrong resources.

INSIGHTS_SECTION

TEST_SECTION

LESSONS_SECTION
