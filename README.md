# Observability-as-a-Service: Duka on AWS

This capstone is centralized monitoring, logging and automated response for a three-tier application: EC2 web, then ECS Fargate API, then RDS MySQL.

**Scenario.** *Duka* is a Kenyan online shop with three tiers. Apache on two EC2 instances serves the storefront. An order API runs on ECS Fargate. Data lives in RDS for MySQL. The CTO wants to see across all three tiers, catch problems early, and have the platform react on its own when something breaks. This repo builds that layer in CloudFormation, deploys it, runs a mock failure test, and documents what happened.

| | |
|---|---|
| **Report (1–2 pages)** | [REPORT.md](REPORT.md) |
| **Region** | eu-west-1 (Ireland). Cape Town (af-south-1) is the nearest region to Kenya but is opt-in and was not enabled on this account. See the report. |
| **Status** | Deployed, tested and **torn down** after the evidence was captured. Everything here can be redeployed with one script. |

## Architecture

```mermaid
flowchart LR
  users([Users in Kenya]) -->|HTTP| alb["Application Load Balancer<br/>eu-west-1, 2 AZs"]

  subgraph tiers["Three-tier application"]
    alb -->|"/ (default)"| web["Web tier<br/>2x EC2 t3.micro, Apache<br/>CloudWatch Agent"]
    alb -->|"/api/*"| api["App tier<br/>ECS Fargate: Duka API<br/>+ X-Ray daemon sidecar"]
    api -->|"SQL (PyMySQL, traced)"| rds[("Data tier<br/>RDS MySQL 8.4<br/>Enhanced Monitoring<br/>slow query log")]
  end

  subgraph collect["Collection"]
    web -->|"mem / disk / cpu"| cwm["CloudWatch Metrics<br/>CWAgent, AWS/*, Duka/*"]
    web -->|"httpd access + error"| cwl["CloudWatch Logs"]
    api -->|"awslogs JSON"| cwl
    api -->|"CPU / mem (Container Insights)"| cwm
    api -->|"segments + SQL subsegments"| xray["AWS X-Ray<br/>service map, traces"]
    rds -->|"error + slowquery logs"| cwl
    rds -->|"OS metrics (Enhanced Monitoring)"| cwl
    rds -->|"RDS metrics"| cwm
    alb -->|"requests, 5xx, latency"| cwm
  end

  subgraph analyse["Analysis and archive"]
    cwl -->|"metric filters: ErrorCount, SlowRequestCount, SlowQueryCount"| cwm
    cwl -->|"Logs Insights saved queries"| insights["error-spikes<br/>error-rate-and-latency<br/>db-slow-queries"]
    cwl -->|"subscription filters (5 log groups)"| fh["Kinesis Data Firehose<br/>gunzip + extract"]
    fh -->|"date-partitioned, gzip"| s3[("S3 log archive<br/>IA at 30 d, Glacier IR at 90 d")]
  end

  subgraph respond["Detect and respond"]
    cwm --> alarms{{"CloudWatch alarms<br/>ecs-cpu-high (&gt;80%)<br/>alb-5xx-high (&gt;5%)<br/>app-error-spike (&gt;10 / 5 min)<br/>rds-cpu-high"}}
    alarms -->|"Alarm State Change"| eb["EventBridge rule"]
    eb --> lambda["Lambda: duka-remediation"]
    lambda -->|"force new deployment"| api
    lambda -->|"tag investigate=true"| web
    lambda --> sns["SNS: duka-oncall"] --> oncall([On-call email])
    ecsev["ECS task stopped events"] --> eb2["EventBridge rule"] --> sns
  end

  cwm --> dash["CloudWatch dashboard<br/>duka-observability"]
  alarms --> dash
  insights --> dash
```

## Evidence

| What | Screenshot |
|---|---|
| Centralized log groups (web, app, DB, Lambda, Firehose, X-Ray) | ![](docs/img/01-log-groups.jpg) |
| X-Ray trace map: client to API to MySQL | ![](docs/img/02-xray-trace-map-healthy.jpg) |
| Mock failure test injection | ![](docs/img/03-failure-test-injection.png) |
| ErrorCount alarm in ALARM 33 s after the error storm | ![](docs/img/04-alarm-error-spike.jpg) |
| ALB 5xx alarm + ErrorCount alarm in ALARM | ![](docs/img/05-alarms-5xx-and-errors.jpg) |
| Saved Logs Insights queries | ![](docs/img/06-saved-insights-queries.jpg) |
| **Remediation Lambda decisions + EC2 `investigate` tags** | ![](docs/img/07-lambda-remediation-and-tags.png) |
| Saved queries' output: error spike, 5xx % per minute, DB slow queries | ![](docs/img/08-insights-query-results.png) |
| ECS CPU at 100%, Firehose archive in S3 (30 objects, plain JSON lines) | ![](docs/img/09-ecs-cpu-and-firehose-archive.png) |
| ECS CPU alarm and its automated restart | ![](docs/img/10-ecs-cpu-alarm-remediation.png) |
| Dashboard: header and alarm overview | ![](docs/img/11-dashboard-top.jpg) |
| Dashboard: ALB 5xx %, latency, ECS CPU, ErrorCount during the incident window | ![](docs/img/12-dashboard-alb-ecs-incident.jpg) |
| Dashboard: RDS CPU, latency, Logs Insights widget | ![](docs/img/13-dashboard-rds-insights.jpg) |
| Unplanned real incident caught and auto-remediated | ![](docs/img/00-real-incident-caught.png) |
| Teardown: all stacks deleted, verify finds nothing left | ![](docs/img/14-teardown-verified.png) |


## Repository layout

```
app/                         Duka API (Flask). JSON logs, X-Ray (Flask + PyMySQL auto-instrumented), fault-injection endpoints
lambda/remediation/          Remediation Lambda: restart ECS service, tag EC2 investigate=true, page on-call
infrastructure/
  01-foundation.yaml         VPC (2 AZs), security groups, ALB (/ -> EC2, /api/* -> ECS), ECR + CodeBuild image build, S3 log archive
  02-data.yaml               RDS MySQL 8.4, Enhanced Monitoring, error + slow query log export, slow_query_log=1 (long_query_time=1 s)
  03-web.yaml                2x EC2 (Apache) in an ASG, CloudWatch Agent config in SSM (mem, disk, CPU, httpd logs)
  04-app.yaml                ECS Fargate service + X-Ray daemon sidecar, Container Insights, awslogs
  05-observability.tmpl.yaml Firehose -> S3, subscription filters, metric filters, saved queries, alarms,
                             SNS, EventBridge -> Lambda, ECS task-stopped rule, dashboard
scripts/
  render.py                  inlines the Lambda source into 05-observability.yaml
  deploy.sh                  up | base | observability <email> | down | verify
  failure-test.sh            baseline | errors | slow | cpu | all
docs/                        architecture diagram and screenshots
```

## Run it yourself

From AWS CloudShell:

```bash
git clone https://github.com/RURIGIMK/aws-observability-capstone.git && cd aws-observability-capstone
./scripts/deploy.sh up you@example.com    # about 15 min. Confirm the SNS email afterwards
./scripts/failure-test.sh all             # about 10 min of baseline traffic plus 3 injected incidents
./scripts/deploy.sh down                  # deletes every stack, the S3 archive, ECR images and stray log groups, then verifies
```
