#!/usr/bin/env bash
# Deploy the EVAL toolbox: same image, same SA, same context set as prod,
# but pointed at the weatherbot_eval database (seeded by
# 07-seed-eval-db.sh) and named weatherbot-toolbox-eval. The eval harness
# (weatherbot-app/evals) targets this service so eval runs never touch
# prod data — record_event writes land in the eval DB.
#
# Everything is delegated to the parameterized 05-deploy-toolbox.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export TOOLBOX_SERVICE_NAME="weatherbot-toolbox-eval"
export TOOLBOX_DB_NAME="${EVAL_DB:-weatherbot_eval}"
# Avoid clashing with a concurrently-running prod deploy's proxy.
export PROXY_PORT="${PROXY_PORT:-15436}"

exec bash "${SCRIPT_DIR}/05-deploy-toolbox.sh"
