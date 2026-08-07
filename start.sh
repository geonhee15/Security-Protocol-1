#!/bin/zsh
cd "$(dirname "$0")"
exec ./venv/bin/python security_protocol.py "$@"
