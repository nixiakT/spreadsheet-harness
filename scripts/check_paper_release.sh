#!/usr/bin/env bash
set -euo pipefail

allow_pending=false
if [[ "${1:-}" == "--allow-pending" ]]; then
  allow_pending=true
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--allow-pending]" >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
paper_dir="$repo_root/paper"
cd -- "$paper_dir"

sha256sum --check official_style.sha256

if rg -n '\\pending\{' --glob '*.tex' .; then
  if [[ "$allow_pending" != true ]]; then
    echo "release paper contains unresolved evidence gates" >&2
    exit 1
  fi
  echo "warning: unresolved evidence gates allowed for this draft build" >&2
fi

if rg -n '\| (pending|pending-review|running|blocked) \|' \
  "$repo_root/research/claims_ledger.md"; then
  if [[ "$allow_pending" != true ]]; then
    echo "claims ledger contains unresolved evidence states" >&2
    exit 1
  fi
  echo "warning: unresolved claim-ledger states allowed for this draft build" >&2
fi

if command -v latexmk >/dev/null 2>&1; then
  latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
elif [[ -n "${TECTONIC_BIN:-}" && -x "$TECTONIC_BIN" ]]; then
  "$TECTONIC_BIN" --keep-logs --keep-intermediates main.tex
else
  echo "latexmk is required (or set TECTONIC_BIN for a draft-equivalent build)" >&2
  exit 1
fi

if rg -n 'Overfull \\hbox|Undefined control sequence|undefined references|Citation.*undefined' main.log; then
  echo "paper build log contains a release-blocking warning" >&2
  exit 1
fi

main_text_page=$(
  sed -n 's/.*newlabel{maintext:end}{{[^}]*}{\([0-9][0-9]*\)}.*/\1/p' main.aux |
    tail -n 1
)
if [[ ! "$main_text_page" =~ ^[0-9]+$ ]]; then
  echo "could not determine the final main-text page" >&2
  exit 1
fi
if (( main_text_page > 9 )); then
  echo "main text ends on page $main_text_page (limit: 9)" >&2
  exit 1
fi

if command -v pdfinfo >/dev/null 2>&1; then
  page_size=$(pdfinfo main.pdf | sed -n 's/^Page size:[[:space:]]*//p')
  if [[ "$page_size" != 612\ x\ 792\ pts* ]]; then
    echo "paper is not US letter size: $page_size" >&2
    exit 1
  fi
fi

echo "paper checks passed: main text ends on page $main_text_page"
