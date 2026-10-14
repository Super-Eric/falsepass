#!/bin/bash
# Typed-out session for the README recording.
falsepass() { uvx --quiet --from git+https://github.com/Super-Eric/falsepass falsepass "$@"; }
say() { printf '\033[1;32m$\033[0m '; for ((i=0; i<${#1}; i++)); do printf '%s' "${1:i:1}"; sleep 0.035; done; sleep 0.4; echo; }
rm -rf /tmp/fp && mkdir -p /tmp/fp && cd /tmp/fp
clear
say "falsepass demo | head -13"
falsepass demo 2>/dev/null | head -13
sleep 3
say "falsepass results falsepass-toy-env --json | jq '.static_hints[0] | {file, line, evidence, verdict}'"
falsepass results falsepass-toy-env --json | jq '.static_hints[0] | {file, line, evidence, verdict}'
sleep 5
