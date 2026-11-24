#!/usr/bin/env bash
# 只执行隔离自动检查。不会初始化生产表、执行真实模型、启用排程或发布。
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
python_bin="${RESEARCH_TEST_PYTHON:-python}"
mkdir -p "$repo_root/outputs/acceptance"
report_dir="$(mktemp -d "$repo_root/outputs/acceptance/automated_XXXXXX")"
printf '自动检查输出目录：%s\n' "$report_dir"
"$python_bin" --version > "$report_dir/versions.txt" 2>&1
node --version >> "$report_dir/versions.txt" 2>&1
npm --version >> "$report_dir/versions.txt" 2>&1
git rev-parse HEAD >> "$report_dir/versions.txt"
git status --short > "$report_dir/worktree.txt"
# 显式隔离；这些设置只在此子脚本中生效，不覆盖 .env。
export TEST_DATABASE_URL=''
export DATABASE_URL='sqlite://'
export LLM_PROVIDER='openai'
export LLM_API_KEY='isolated-acceptance-key'
export LLM_MODEL='isolated-acceptance-model'
export LLM_BASE_URL='https://example.invalid/v1'
export ANALYSIS_RUNTIME_PINNING='false'
export RESEARCH_RUNTIME_PINNING='false'
run_step() {
    local step_name="$1"
    shift
    printf '正在检查：%s\n' "$step_name"
    if "$@" > "$report_dir/$step_name.log" 2>&1; then
        printf 'PASS %s\n' "$step_name" | tee -a "$report_dir/summary.txt"
    else
        local result_code=$?
        printf 'FAIL %s (exit=%s)，日志：%s\n' "$step_name" "$result_code" "$report_dir/$step_name.log" | tee -a "$report_dir/summary.txt"
        tail -n 60 "$report_dir/$step_name.log"
        exit "$result_code"
    fi
}
run_step backend "$python_bin" -m pytest -q test -p test.audit_isolation --tb=short --junitxml="$report_dir/backend.junit.xml"
cd "$repo_root/web"
run_step web_build npm run build
run_step web_unit npm test
for script_name in render data questions planning citations claim-review execution-review library-execution coverage reread supplement-search supplement-readings execution-review-links execution-acceptance evaluation-workspace; do
    run_step "web_$script_name" npm run "test:$script_name"
done
printf '隔离自动检查结束。真实PostgreSQL/模型/浏览器/P4-P5管理验收尚需按文档执行。\n' | tee -a "$report_dir/summary.txt"
