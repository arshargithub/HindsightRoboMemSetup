#!/usr/bin/env python3
"""CDK app entry point for Hindsight infrastructure."""

import aws_cdk as cdk
from hindsight_stack import HindsightStack
from config import ENVIRONMENTS

app = cdk.App()

# Create dev stack
dev_stack = HindsightStack(
    app,
    "HindsightDevStack",
    env=cdk.Environment(
        account=ENVIRONMENTS["dev"]["account"],
        region=ENVIRONMENTS["dev"]["region"],
    ),
    environment="dev",
    description="Hindsight memory service - Development environment",
)

# Create prod stack
prod_stack = HindsightStack(
    app,
    "HindsightProdStack",
    env=cdk.Environment(
        account=ENVIRONMENTS["prod"]["account"],
        region=ENVIRONMENTS["prod"]["region"],
    ),
    environment="prod",
    description="Hindsight memory service - Production environment",
)

app.synth()
