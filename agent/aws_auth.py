"""AWS credential availability without Bedrock transport or model metadata."""
from __future__ import annotations
import logging
import os
from typing import Dict, Optional
logger = logging.getLogger(__name__)

def resolve_aws_auth_env_var(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Return the name of the AWS auth source that is active, or None.

    Checks environment variables first, then falls back to boto3's credential
    chain for implicit sources (EC2 IMDS, ECS task role, etc.).

    This detects credential availability without authenticating a request.
    """
    env = env if env is not None else os.environ
    # Bearer token takes highest priority
    if env.get("AWS_BEARER_TOKEN_BEDROCK", "").strip():
        return "AWS_BEARER_TOKEN_BEDROCK"
    # Explicit access key pair
    if (env.get("AWS_ACCESS_KEY_ID", "").strip()
            and env.get("AWS_SECRET_ACCESS_KEY", "").strip()):
        return "AWS_ACCESS_KEY_ID"
    # Named profile (SSO, assume-role, etc.)
    if env.get("AWS_PROFILE", "").strip():
        return "AWS_PROFILE"
    # Container credentials (ECS, CodeBuild)
    if env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "").strip():
        return "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"
    # Web identity (EKS IRSA)
    if env.get("AWS_WEB_IDENTITY_TOKEN_FILE", "").strip():
        return "AWS_WEB_IDENTITY_TOKEN_FILE"
    # No env vars — check if boto3 can resolve credentials via IMDS or other
    # implicit sources (EC2 instance role, ECS task role, Lambda, etc.)
    try:
        import botocore.session
        session = botocore.session.get_session()
        credentials = session.get_credentials()
        if credentials is not None:
            resolved = credentials.get_frozen_credentials()
            if resolved and resolved.access_key:
                return "iam-role"
    except Exception:
        logger.debug("Optional AWS credential chain unavailable", exc_info=True)
    return None


def has_aws_credentials(env: Optional[Dict[str, str]] = None) -> bool:
    """Return True if any AWS credential source is detected.

    Checks environment variables first (fast, no I/O), then falls back to
    boto3's credential chain which covers EC2 instance roles, ECS task roles,
    Lambda execution roles, and other IMDS-based sources that don't set
    environment variables.

    Cloud deployments need both environment and SDK credential sources:
    cloud environments (EC2, ECS, Lambda) provide credentials via instance
    metadata, not environment variables. The env-var check is a fast path
    for local development; the boto3 fallback covers all cloud deployments.
    """
    if resolve_aws_auth_env_var(env) is not None:
        return True
    # Fall back to boto3's credential resolver — this covers EC2 instance
    # metadata (IMDS), ECS container credentials, and other implicit sources
    # that don't set environment variables.
    try:
        import botocore.session
        session = botocore.session.get_session()
        credentials = session.get_credentials()
        if credentials is not None:
            resolved = credentials.get_frozen_credentials()
            if resolved and resolved.access_key:
                return True
    except Exception:
        logger.debug("Optional AWS credential chain unavailable", exc_info=True)
    return False
