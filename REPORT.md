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

## 2. Example Logs Insights query

This is saved as `duka/error-spikes`. Every API request writes one JSON log line, so Logs Insights can filter on real fields instead of regex-matching free text:

```
fields @timestamp, level, path, status, message
| filter level = "ERROR"
| stats count(*) as errors by bin(1m) as minute, path
| sort minute desc
```

**What it finds:** which minute errors spiked and on which endpoint. During the test it returned 150 errors at 14:59 UTC, all on `/api/fault/error`, the simulated M-Pesa payment-gateway timeout. The companion query `duka/error-rate-and-latency-per-minute` showed **33% 5xx** in that minute. `duka/db-slow-queries` showed **12 slow `SELECT county, COUNT(*)` statements (worst 3.6 s) at 14:59–15:00**.

**Correlating 5xx errors and latency with slow database queries:**
- The app-side p95 latency (`latency_ms`), the RDS slow query log, and X-Ray SQL subsegments all line up on the same minute.
- X-Ray shows the time is spent in the `Database SQL` node, not in Python.

Performance Insights would normally give the database-side view. It is **not available on the db.t3.micro class** used to keep this lab under $1, so the template enables it automatically on db.t3.medium and larger. With the micro instance, the slow query log and X-Ray SQL subsegments provide the same correlation.

## 3. Alarms and automated actions tested

The mock failure test is `scripts/failure-test.sh all`. It ran 2 minutes of normal traffic, then injected three incidents:

| Incident (UTC) | Injected by | Alarm (time in ALARM) | Automated response (from Lambda logs) | Result |
|---|---|---|---|---|
| Error storm, 14:59:54 | 150x `/api/fault/error` (HTTP 500 + ERROR logs) | `duka-app-error-spike` at 15:00:27, **33 s** later | Forced ECS deployment `ecs-svc/0570…`. Tagged both web instances `investigate=true`. SNS email | OK again at 15:05:27 |
| Same storm, as ALB 5xx | (same) | `duka-alb-5xx-high` at 15:02:59. ALB metrics arrive about 2–3 min late | Tagged web tier, restarted API, SNS email | OK at 15:03:59 |
| Slow queries, 14:59:55 | 12x `/api/report?slow=3` (`SLEEP()` in SQL) | No alarm by design. Shown on the dashboard and in the slow-query metric and query | n/a | 12 statements in the slow log |
| CPU burn, 15:06:38–15:10:19 | 4 parallel 55 s CPU loops | `duka-ecs-cpu-high` at **15:12:16** (CPU 100% at 15:08 and 15:09) | Forced ECS deployment `ecs-svc/4666…`. SNS email | OK at 15:13:16 |

All four remediations sent an SNS email to the on-call address, which was confirmed. The EC2 instances carried `investigate=true`, `investigate-reason=duka-alb-5xx-high` and `investigate-at=…` tags.

**An unplanned incident was caught first.** Before the planned test, the first API build had two real bugs: a missing MySQL auth library, then a deadlock from forking with a background thread. Requests hung and the ALB returned 504s. `duka-alb-5xx-high` fired twice (62.5% and 50% 5xx at 14:43 and 14:50), and the Lambda restarted the service both times without anyone stepping in. The dashboard shows both incident windows. The root cause was fixed in code (commit `a812852`).

## 4. Lessons learned and next improvements

1. **Remediation can hide its own signal.** On the first CPU run, the alarm did not fire. The error-spike remediation had just started a rolling deployment, so for a few minutes the service ran two tasks: one at 100% CPU and one idle. The *average* was about 50%. *Next time:* alarm on `Maximum` per task, using Container Insights `TaskCpuUtilization`, as well as the service average.
2. **Automatic restarts need a circuit breaker of their own.** One burst of errors set off two alarms, each of which restarted the service. *Next time:* add a cooldown (a DynamoDB timestamp, or an EventBridge Pipes filter) so the same service is restarted at most once every 10 minutes. Better still, use a composite alarm so on-call gets one email per incident instead of one per symptom.
3. **Liveness checks must not depend on the database.** `/api/health` stays shallow, so a slow database degrades requests instead of making the ALB kill every task.
4. **ALB metrics arrive later than log metrics.** The log-based ErrorCount alarm fired 33 s after the incident. The ALB 5xx alarm took about 3 minutes. Alerting on log-derived metrics is the fastest early warning.
5. **Next improvements:**
   - db.t3.medium with Performance Insights / Database Insights.
   - Container Insights enhanced observability for per-task metrics.
   - CloudWatch anomaly detection on p95 latency instead of fixed thresholds.
   - Athena tables over the S3 archive so auditors can query it.
   - Deploying to **af-south-1 (Cape Town)** for lower latency from Kenya. That region is opt-in and was not enabled on this account.

**Cost and clean-up:** the full lab ran for about 2 hours. The estimated cost is $0.30–0.50 (ALB, 2x t3.micro, db.t3.micro, one 0.25-vCPU Fargate task, a few CodeBuild minutes). Everything was deleted afterwards with `scripts/deploy.sh down`, which removes the 5 stacks, the emptied S3 archive, the ECR images and the log groups that AWS creates on its own. `deploy.sh verify` then confirmed that no stacks, instances, databases, snapshots, load balancers, clusters, repositories, Firehose streams, buckets, alarms or Elastic IPs were left ([screenshot](docs/img/14-teardown-verified.png)).

6. **Teardown needs the same rigor as deployment.** A deploy command was still waiting in another CloudShell tab. When the first teardown deleted the app stack, that command's wait finished and it recreated the app and observability stacks. The fix: run teardown as a single owner of the account state (one shell, `nohup` and a log file), then verify from a clean list. In a team this would be one pipeline job with a lock, not ad-hoc terminals.
