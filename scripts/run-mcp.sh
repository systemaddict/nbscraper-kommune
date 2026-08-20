#!/bin/sh
set -eu

tls_dir="${NBK_MCP_TLS_DIR:-/tmp/nbk-mcp-tls}"
server_name="${NBK_MCP_TLS_SERVER_NAME:-mcp}"
cert_file="$tls_dir/cert.pem"
key_file="$tls_dir/key.pem"

mkdir -p "$tls_dir"
chmod 700 "$tls_dir"
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 2 \
    -subj "/CN=$server_name" \
    -addext "subjectAltName=DNS:$server_name,DNS:mcp,DNS:localhost" \
    -keyout "$key_file" \
    -out "$cert_file" \
    >/dev/null 2>&1
chmod 600 "$cert_file" "$key_file"

export NBK_MCP_SSL_CERTFILE="$cert_file"
export NBK_MCP_SSL_KEYFILE="$key_file"

exec nbk mcp --http --host 0.0.0.0 --port 8766
