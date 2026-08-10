#!/usr/bin/env bash

# Load the one deployment identity setting without sourcing the rest of .env.
# This keeps secrets out of the host shell while giving maintenance scripts the
# same name used by the container runtime.
load_agent_identity() {
    local configured="${AGENT_NAME:-}"
    local line

    if [[ -z "$configured" && -f .env ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            line="${line%$'\r'}"
            line="${line#export }"
            if [[ "$line" == AGENT_NAME=* ]]; then
                configured="${line#AGENT_NAME=}"
            fi
        done < .env
    fi

    if [[ "$configured" == \"*\" && "$configured" == *\" ]]; then
        configured="${configured:1:${#configured}-2}"
    elif [[ "$configured" == \'*\' && "$configured" == *\' ]]; then
        configured="${configured:1:${#configured}-2}"
    fi

    AGENT_NAME="${configured:-Assistant}"
    AGENT_SLUG="$(
        printf '%s' "$AGENT_NAME" \
            | LC_ALL=C tr '[:upper:]' '[:lower:]' \
            | LC_ALL=C tr -cs 'a-z0-9' '-' \
            | sed 's/^-//; s/-$//'
    )"
    AGENT_SLUG="${AGENT_SLUG:-agent}"
    export AGENT_NAME AGENT_SLUG
}
