#!/bin/sh
set -eu

exec nbk mcp --http --host 0.0.0.0 --port 8766
