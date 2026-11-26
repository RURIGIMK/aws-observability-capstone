"""Automated incident response.

Triggered by an EventBridge rule on "CloudWatch Alarm State Change" events (state = ALARM)
for the capstone's alarms. Decides what to do from the alarm name, does it, records every
action as a structured log line, and pages on-call through SNS with a summary.

  ecs-cpu-high      -> force a new ECS deployment (replaces the hot tasks) + notify
  app-error-spike   -> restart the ECS service + tag web EC2 instances investigate=true + notify
  alb-5xx-high      -> tag web EC2 instances investigate=true + restart ECS service + notify
  anything else     -> notify only
"""
import json
import logging
import os
from datetime import datetime, timezone

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

ecs = boto3.client("ecs")
ec2 = boto3.client("ec2")
sns = boto3.client("sns")

CLUSTER = os.environ["ECS_CLUSTER"]
SERVICE = os.environ["ECS_SERVICE"]
TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
WEB_TAG_KEY = os.environ.get("WEB_TIER_TAG_KEY", "Tier")
WEB_TAG_VALUE = os.environ.get("WEB_TIER_TAG_VALUE", "web")


def restart_ecs_service():
    """Rolling replacement of every task. ECS keeps minimum healthy capacity during it."""
    resp = ecs.update_service(cluster=CLUSTER, service=SERVICE, forceNewDeployment=True)
    dep = resp["service"]["deployments"][0]["id"]
    return f"Forced new deployment {dep} on {CLUSTER}/{SERVICE}"


def tag_web_instances(alarm_name):
    reservations = ec2.describe_instances(
        Filters=[
            {"Name": f"tag:{WEB_TAG_KEY}", "Values": [WEB_TAG_VALUE]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )["Reservations"]
    ids = [i["InstanceId"] for r in reservations for i in r["Instances"]]
    if not ids:
        return "No running web instances to tag"
    ec2.create_tags(
        Resources=ids,
        Tags=[
            {"Key": "investigate", "Value": "true"},
            {"Key": "investigate-reason", "Value": alarm_name[:250]},
            {"Key": "investigate-at", "Value": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
        ],
    )
    return f"Tagged {len(ids)} web instance(s) investigate=true: {', '.join(ids)}"


PLAYBOOK = {
    "ecs-cpu-high": [restart_ecs_service],
    "app-error-spike": [restart_ecs_service, "tag"],
    "alb-5xx-high": ["tag", restart_ecs_service],
}


def handler(event, context):
    detail = event.get("detail", {})
    alarm = detail.get("alarmName", "unknown")
    state = detail.get("state", {})
    reason = state.get("reason", "")
    log.info(json.dumps({"msg": "alarm received", "alarm": alarm, "state": state.get("value")}))

    if state.get("value") != "ALARM":
        return {"skipped": True}

    actions = []
    for key, steps in PLAYBOOK.items():
        if alarm.endswith(key):
            for step in steps:
                try:
                    actions.append(tag_web_instances(alarm) if step == "tag" else step())
                except Exception as exc:  # keep going: a failed step must not block paging
                    actions.append(f"FAILED {getattr(step, '__name__', step)}: {exc}")
            break

    summary = {
        "alarm": alarm,
        "state": "ALARM",
        "time": event.get("time"),
        "reason": reason,
        "automated_actions": actions or ["none (notify only)"],
        "runbook": "Check the duka-observability dashboard; Logs Insights query 'duka/error-spikes'.",
    }
    sns.publish(
        TopicArn=TOPIC_ARN,
        Subject=f"[ALARM] {alarm}"[:100],
        Message=json.dumps(summary, indent=2),
    )
    summary["notified"] = TOPIC_ARN
    log.info(json.dumps({"msg": "remediation complete", **summary}))
    return summary
