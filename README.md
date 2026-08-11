# TrackInfra

Video-wall (mosaico) de rastreamento próprio da Impacto, construído sobre a API do
**FullTrack**. Resolve a dor do mosaico oficial: aqui **cada mapa recentraliza no
carro** a cada atualização, então o veículo nunca "sai" da tela.

- **Grid automático** pelo nº de carros ligados: 3 → 2×2, 5 → 2×3, 7 → 3×3…
  (`cols = ⌈√n⌉`, `rows = ⌈n/cols⌉`).
- **Login por QR-code** — ideal para TV: ninguém digita senha na tela.
- **Mosaico salvo** — seleção de veículos e preferências persistidas.
- A TV guarda apenas um **token opaco**; credenciais e token do FullTrack ficam só no backend.

## Arquitetura

```
   TV (navegador)                Backend (FastAPI)              FullTrack
 ┌───────────────┐   /api/*    ┌──────────────────┐   login/getDados   ┌──────────┐
 │ index.html    │◀──────────▶ │  proxy + sessões │◀─────────────────▶ │ fulltrack│
 │ mosaic.js     │             │  QR / token      │   Bearer / cookie  │  api-... │
 └───────▲───────┘             └────────▲─────────┘                    └──────────┘
         │ QR (uuid)                     │ /api/auth/login (user,senha)
         │                        ┌──────┴───────┐
         └── escaneado ─────────▶ │ Celular      │ (login.html)
                                  └──────────────┘
```

### Fluxo de login por QR
1. A TV abre `/` sem token → chama `POST /api/auth/session` e exibe o QR.
2. O QR aponta para `PUBLIC_BASE_URL/login?s=<uuid>`, aberto no celular.
3. O celular envia usuário/senha do FullTrack → `POST /api/auth/login`.
4. O backend loga no FullTrack (form POST + troca por token ftk4), cria uma
   sessão e vincula ao `uuid`.
5. A TV, em polling no `GET /api/auth/status/<uuid>`, recebe o token opaco e
   começa a renderizar o mosaico.

### Como os dados chegam
- Frota: `POST fulltrackapp.com/mapaGeral_v2/getDados` (cookie de sessão) →
  normalizado em `/api/fleet`.
- Notificações: `GET api-fulltrack4.../plataform/notification/total`
  (Bearer, com refresh automático).

## Rodando

```bash
cd trackinfra/backend
python -m venv .venv && . .venv/Scripts/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # ajuste TRACKINFRA_PUBLIC_BASE_URL !
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Abra a TV em `http://<ip-do-servidor>:8000/`.

> ⚠️ **`TRACKINFRA_PUBLIC_BASE_URL` é essencial**: precisa ser o endereço que o
> **celular** consegue acessar (IP na rede local ou domínio público). Se ficar
> `localhost`, o QR-code não vai funcionar no telefone.

## Deploy na infra (VM 100)

Roda em `/opt/infratrack` na **VM 100** (`172.21.1.10`), junto dos outros
containers. Topologia e procedimento completos em `/opt/infra/README.md`.

| Item | Valor |
|---|---|
| URL | `https://infra.it.vistotrack.com:8443` |
| Porta interna | `172.21.1.10:8300` (só a interface interna — o Traefik chega por ela) |
| Rota do Traefik | `/etc/traefik/dynamic/trackinfra.yml` no LXC 102 — cópia em `deploy/traefik/` |
| Banco | nenhum. Sessões em memória; mosaico salvo no volume `infratrack_trackinfra_data` |
| Healthcheck | `/healthz`, no container e no Traefik |

```bash
docker compose up -d --build
docker compose logs -f api
```

Publicar a rota. O `pct push` roda **no `nuc1`** e lê um arquivo local dele, e a
VM 100 não tem chave para o LXC 102 — por isso o `scp` no meio do caminho:

```bash
scp deploy/traefik/trackinfra.yml nuc1:/tmp/trackinfra.yml
ssh nuc1 'pct push 102 /tmp/trackinfra.yml /etc/traefik/dynamic/trackinfra.yml && rm /tmp/trackinfra.yml'
# o pct push cria como root:root; as outras rotas sao traefik:traefik
ssh nuc1 'pct exec 102 -- chown traefik:traefik /etc/traefik/dynamic/trackinfra.yml'
ssh nuc1 'pct exec 102 -- journalctl -u traefik -n 30 --no-pager'   # erro de config aparece aqui
```

> O arquivo se chama `trackinfra.yml` (nome do serviço) mas serve o host
> `infra.it.vistotrack.com`. Mesmo descasamento do `notification.yml`, que
> serve `notificador.it.vistotrack.com`.

> ⚠️ **`TRACKINFRA_PUBLIC_BASE_URL` com `:8443`.** É o que entra no QR-code.
> Pela LAN o Traefik também escuta na 8443 (de propósito — seção 9 do README da
> infra), então o mesmo endereço serve para celular no Wi-Fi e no 4G, com o
> mesmo certificado Let's Encrypt.

> ⚠️ **Validar o acesso externo só pelo 4G.** Toda conexão de saída na 8443 de
> dentro da rede é interceptada e entregue ao Traefik local — `curl` da LAN
> devolve 200 mesmo com o port forward desligado. Falso positivo garantido.

> A TV precisa de **internet aberta**: o Leaflet vem do `unpkg.com` e os tiles
> do `tile.openstreetmap.org`. O backend só fala com o FullTrack, saindo
> NATeado pelo `nuc1`.

## Endpoints

| Método | Rota | Descrição |
|--------|------|-----------|
| POST | `/api/auth/session` | cria sessão de QR (TV) |
| GET  | `/api/auth/qr/{uuid}.png` | imagem do QR |
| POST | `/api/auth/login` | celular envia credenciais |
| GET  | `/api/auth/status/{uuid}` | polling da TV |
| GET  | `/api/fleet` | frota normalizada (Bearer) |
| GET  | `/api/notifications/total` | alertas não lidos (Bearer) |
| GET/POST | `/api/config` | mosaico salvo |

## Segurança e limitações

- Credenciais do FullTrack **nunca** são gravadas em disco. Ficam em memória, na
  sessão do backend, e são usadas para o **re-login automático**: quando o cookie
  do FullTrack cai, o backend refaz o login sozinho e a TV nem percebe.
- O QR expira em 5 min; o token da TV **não expira** (`TRACKINFRA_APP_TOKEN_TTL=0`).
  Só o token curto do handoff de configuração (celular) continua caindo em 15 min.
- A TV só volta a exibir o QR em dois casos: **restart do backend** (as sessões são
  em memória) ou **senha trocada no FullTrack** (o re-login passa a ser recusado e
  o `/api/fleet` devolve 401). Instabilidade do FullTrack devolve 503 e a TV segue
  com o último mosaico na tela, tentando de novo — não pisca para o QR.
- Sobreviver a restart exigiria gravar a credencial em disco: **decisão consciente
  de não fazer**, é o trade-off de segurança que mantém a senha só na RAM.
- Sessões em memória: para múltiplas instâncias, migrar `sessions.py` para Redis.
- API do FullTrack é **privada/não documentada** (v3.2.50) — pode mudar sem aviso.
  Verifique o contrato de uso antes de produção.
