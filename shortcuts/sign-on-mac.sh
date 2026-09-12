#!/bin/bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    printf '%s\n' 'Run this script on macOS.' >&2
    exit 1
fi

source_dir="$(cd "$(dirname "$0")" && pwd)"
output_dir="${1:-$HOME/Downloads/media-manager-shortcuts}"
mkdir -p "$output_dir"

for name in "Convert Media" "Compress Media"; do
    input="$source_dir/${name}_unsigned.shortcut"
    output="$output_dir/${name}.shortcut"
    [[ -s "$input" ]] || { printf 'Missing %s\n' "$input" >&2; exit 1; }
    plutil -lint "$input"
    rm -f "$output"
    shortcuts sign --mode anyone --input "$input" --output "$output"
    [[ -s "$output" ]] || { printf 'Signing did not create %s\n' "$output" >&2; exit 1; }
done

open "$output_dir"
printf 'Signed shortcuts are in: %s\n' "$output_dir"
