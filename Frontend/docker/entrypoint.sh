#!/bin/sh
set -eu

# 与应用侧 ECHOMIND_CHAT_TIMEOUT_S 保持同一基准：客户端预算 = 应用预算 + 15s。
CHAT_TIMEOUT_S="${CHAT_TIMEOUT_S:-90}"
CHAT_TIMEOUT_MS=$(( (CHAT_TIMEOUT_S + 15) * 1000 ))

cat >/usr/share/nginx/html/runtime-config.js <<EOF
window.__ECHOMIND_CONFIG__ = {
  pythonApiUrl: "${PYTHON_API_URL:-/api/python}",
  javaApiUrl: "${JAVA_API_URL:-/api/java}",
  chatTimeoutMs: ${CHAT_TIMEOUT_MS}
};
EOF

exec nginx -g "daemon off;"
