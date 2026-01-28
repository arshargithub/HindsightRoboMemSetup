"""Configuration for Hindsight CDK stacks."""

from typing import Dict, Any

# AWS Region
REGION = "ca-central-1"

# Environment configurations
ENVIRONMENTS: Dict[str, Dict[str, Any]] = {
    "dev": {
        "account": None,  # Will use default AWS account
        "region": REGION,
        "rds_instance_class": "db.t3.micro",
        "ecs_task_cpu": 512,
        "ecs_task_memory": 1024,
        "ecs_desired_count": 1,
        "reflect_schedule": "rate(1 hour)",  # Dev: reflect every hour
    },
    "prod": {
        "account": None,  # Will use default AWS account
        "region": REGION,
        "rds_instance_class": "db.t3.small",  # Prod: slightly larger
        "ecs_task_cpu": 1024,
        "ecs_task_memory": 2048,
        "ecs_desired_count": 2,  # Prod: 2 tasks for availability
        "reflect_schedule": "rate(15 minutes)",  # Prod: reflect every 15 min
    },
}

# Hindsight configuration
HINDSIGHT_IMAGE = "ghcr.io/vectorize-io/hindsight:latest"
HINDSIGHT_API_PORT = 8888
HINDSIGHT_CONTROL_PLANE_PORT = 9999

# LLM provider and model (Hindsight uses these for retain/reflect)
# Provider: openai | anthropic | groq | gemini | ollama | lmstudio
# Model examples: gpt-4o, gpt-4o-mini, claude-sonnet-4-20250514, openai/gpt-oss-20b, etc.
# See https://hindsight.vectorize.io/developer/configuration
LLM_PROVIDER = "openai"
LLM_MODEL = "gpt-4o-mini"  # Or gpt-4o, etc.

# Database configuration
DB_NAME = "hindsight"
DB_USERNAME = "hindsight_admin"

# ALB configuration
# Health check path: /health checks DB connectivity (good for ensuring service is ready)
# If health checks fail, investigate DB performance/connectivity rather than changing the check
ALB_HEALTH_CHECK_PATH = "/health"
# ALB idle timeout (default 60s). Hindsight /docs and first requests can be slow; avoid 502.
ALB_IDLE_TIMEOUT_SECONDS = 120
