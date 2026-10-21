#!/bin/sh
# keepalived health check for the k3s API (managed by Ansible, role svc_k3s). Needs no credentials: the API answers
# /livez with 200 (anonymous-auth on) or 401/403 (off, k3s default here); any of these means kube-apiserver serves.
# No answer (connection refused, timeout) or 5xx = fault.
code=$(/usr/bin/curl -ks --max-time 2 -o /dev/null -w '%{http_code}' https://127.0.0.1:6443/livez) || exit 1
case "$code" in
  200|401|403) exit 0 ;;
  *) exit 1 ;;
esac
