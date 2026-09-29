#!/usr/bin/env bash
set -e

CONFIG_PATH="/data/options.json"

if [ -f "$CONFIG_PATH" ]; then
    export ASI1_API_KEY=$(jq -r '.asi1_api_key // ""' $CONFIG_PATH)
    export ASI1_MODEL=$(jq -r '.asi1_model // "asi1-mini"' $CONFIG_PATH)
    export ASI1_MODEL_STRONG=$(jq -r '.asi1_model_strong // ""' $CONFIG_PATH)
    export ASI1_STRONG_DAILY_CAP=$(jq -r '.asi1_strong_daily_cap // 20' $CONFIG_PATH)
    # Not `// true`: jq's // replaces false as well as null, which would make
    # the analyst's kill switch impossible to turn off.
    export ANALYST_ENABLED=$(jq -r 'if .analyst_enabled == false then "false" else "true" end' $CONFIG_PATH)
    export DIGEST_DAY=$(jq -r '.digest_day // "saturday"' $CONFIG_PATH)
    export AGENT_NAME=$(jq -r '.agent_name // "HAAutomationAgent"' $CONFIG_PATH)
    export AGENT_DISPLAY_NAME=$(jq -r '.agent_display_name // "HA Automation Agent"' $CONFIG_PATH)
    export AGENTVERSE_API_KEY=$(jq -r '.agentverse_api_key // ""' $CONFIG_PATH)
    export NOTIFY_SERVICE=$(jq -r '.notify_service // ""' $CONFIG_PATH)
    # The behavioural record's size ceiling. 0 means no cap — the record is
    # then bounded only in time, which on a home with fast-reporting sensors
    # is not a bound at all.
    export RECORD_MAX_MB=$(jq -r '.record_max_mb // 256' $CONFIG_PATH)
fi

# Fixed internal values
export AGENT_PORT=8099

# Seed: generate once, persist forever
SEED_FILE=/data/seed.txt
if [ ! -f "$SEED_FILE" ]; then
    python3 -c "import secrets; print('ha_agent_seed_' + secrets.token_hex(32))" > "$SEED_FILE"
    echo "Generated new agent seed."
fi
export AGENT_SEED=$(cat "$SEED_FILE")

echo "Starting Fetch.ai Automation Agent: ${AGENT_NAME} on port ${AGENT_PORT}"

# Start ingress proxy in background
python3 /proxy.py &

# Start agent (blocking)
cd /app
exec python3 main.py
