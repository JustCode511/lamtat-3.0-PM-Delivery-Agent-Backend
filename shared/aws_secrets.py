"""
Load backend secrets from SSM Parameter Store into the environment.

On AWS the Lambda receives only non-secret config as env vars, plus
SSM_PARAM_PREFIX. At cold start we fetch every SecureString under that prefix
and inject it into os.environ (JWT_SECRET, JIRA_*, SLACK_*), so the rest of the
code keeps reading os.getenv(...) unchanged and no secret sits in the Lambda's
visible env config.

No-op locally (SSM_PARAM_PREFIX unset → secrets come from .env as before).
"""
from __future__ import annotations
import logging
import os

log = logging.getLogger(__name__)


def load_secrets_from_ssm() -> None:
    prefix = os.getenv("SSM_PARAM_PREFIX")
    if not prefix:
        return  # local dev / not configured — nothing to do

    import boto3  # imported lazily so local dev never needs it

    ssm = boto3.client("ssm")  # region comes from AWS_REGION on Lambda
    paginator = ssm.get_paginator("get_parameters_by_path")

    count = 0
    for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=True):
        for p in page.get("Parameters", []):
            key = p["Name"].rsplit("/", 1)[-1]  # /pm-agent/demo/JWT_SECRET -> JWT_SECRET
            # Don't override a value explicitly set in the Lambda env.
            os.environ.setdefault(key, p["Value"])
            count += 1

    log.info("Loaded %d secret(s) from SSM prefix %s", count, prefix)
