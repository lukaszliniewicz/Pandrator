#!/bin/sh
set -eu

source_dir="${PANDRATOR_SOURCE_DIR:-/source}"
work_dir="${PANDRATOR_WORK_DIR:-/work/Pandrator}"
output_dir="${PANDRATOR_OUTPUT_DIR:-/output}"

if [ ! -f "$source_dir/pixi.toml" ] || [ ! -f "$source_dir/pixi.lock" ]; then
    echo "The source mount does not look like a Pandrator checkout: $source_dir" >&2
    exit 2
fi

rm -rf "$work_dir"
mkdir -p "$work_dir" "$output_dir"

# Keep host-specific environments and generated/user data out of the Linux build
# workspace. The source checkout is mounted read-only by the host wrapper.
tar \
    --exclude='./.appimage-tools' \
    --exclude='./.env' \
    --exclude='./.env.*' \
    --exclude='./.git' \
    --exclude='./.pixi' \
    --exclude='./.pixi-cache' \
    --exclude='./.pixi-home' \
    --exclude='./.pytest_cache' \
    --exclude='./.release_blocks' \
    --exclude='./.release_sources' \
    --exclude='./.release_staging' \
    --exclude='./.venv' \
    --exclude='./ENV' \
    --exclude='./Outputs' \
    --exclude='./__pycache__' \
    --exclude='*/__pycache__' \
    --exclude='./build' \
    --exclude='./conda' \
    --exclude='./dist' \
    --exclude='./env' \
    --exclude='./logs' \
    --exclude='./migration-web-v1.json' \
    --exclude='./pandrator.sqlite3*' \
    --exclude='./pandrator_settings.json' \
    --exclude='./pandrator_state.sqlite3*' \
    --exclude='./release_packages' \
    --exclude='./rvc_models' \
    --exclude='./venv' \
    --exclude='./web/node_modules' \
    --exclude='./web/playwright-report' \
    --exclude='./web/test-results' \
    --create --file - --directory "$source_dir" . \
    | tar --extract --file - --directory "$work_dir"

cd "$work_dir"
pixi run --locked --environment installer-build \
    python scripts/build_linux_appimage.py --output-dir "$output_dir" "$@"
