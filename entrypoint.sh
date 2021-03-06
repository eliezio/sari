#!/bin/bash
# shellcheck disable=SC2086

set -eux

MODEL_JSON=model.json
trap "rm -f $MODEL_JSON" EXIT

PULUMI_ACTION="$@"

if [ -n "${PULUMI_BACKEND_URL:-}" ]; then
    pulumi --non-interactive login --cloud-url $PULUMI_BACKEND_URL
else
    pulumi --non-interactive login --local
fi

pulumi --non-interactive stack select $PULUMI_STACK_NAME --create

./build-model.py --model=$MODEL_JSON --purge-pulumi-stack

pulumi --non-interactive --logtostderr -v=${PULUMI_LOG_LEVEL:-2} ${PULUMI_ACTION:-preview}
