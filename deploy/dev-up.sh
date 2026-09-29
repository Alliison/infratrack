#!/usr/bin/env bash
# Sobe a stack de DEVELOP do TrackInfra resolvendo o IP do tailnet em tempo de
# execucao. Existe para que nenhum endereco fique fixo em arquivo: o IP muda se o
# node for recriado no tailnet, e um literal quebraria a stack silenciosamente.
#
# Uso:  deploy/dev-up.sh [argumentos extras do docker compose]
#       deploy/dev-up.sh --build        # reconstroi a imagem
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v tailscale >/dev/null 2>&1; then
    echo "tailscale nao encontrado: defina TAILSCALE_IP a mao e rode o compose." >&2
    exit 1
fi

TAILSCALE_IP="${TAILSCALE_IP:-$(tailscale ip -4 2>/dev/null | head -1)}"
if [[ -z "$TAILSCALE_IP" ]]; then
    echo "nao foi possivel descobrir o IP do tailnet (tailscale esta conectado?)." >&2
    exit 1
fi
export TAILSCALE_IP

echo "tailnet: $TAILSCALE_IP  ->  http://$TAILSCALE_IP:8301"
exec docker compose -f docker-compose.yml -f docker-compose.dev.yml \
     -p trackinfra-dev up -d "$@"
