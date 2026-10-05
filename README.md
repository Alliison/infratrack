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

### Contas, TVs e celulares

- **Conta** = um usuário do FullTrack. Guarda a única sessão do FullTrack dela;
  todas as TVs da conta usam essa sessão (e a frota é buscada uma vez a cada
  2 s por conta, não uma vez por TV).
- **TV (dispositivo)** = token próprio + nome + configuração própria (carros,
  trava FullTrack/SGI, rodízio, equipe/tipo do SGI). Duas TVs da mesma conta
  podem mostrar carros diferentes, ou uma presa no FullTrack e outra no SGI.
- **Celular (controle)** = quem loga com a senha vira controle da conta (token
  que expira após 7 dias sem uso, `TRACKINFRA_CONTROLLER_TOKEN_TTL`). Com ele:
  - escanear o QR de uma **TV nova** oferece *Liberar sem senha*;
  - `/devices` lista as TVs: no ar ou não, tela atual, página, placas na tela,
    trava de tela com um toque, **Configurar**, **Espelhar** (vê no celular o
    que a TV mostra), **Identificar** (nome em tela cheia), **Recarregar**,
    **Desconectar** só ela, e **Desconectar tudo**.
- O QR **Configurar** da TV dá um controle curto (15 min parado) já apontado
  para aquela TV.
- Desconectar (uma, ou tudo) mantém nome e configuração no registro: a TV
  manda o `device_id` que lembra ao pedir QR novo e volta como era.
- Teste com duas TVs no mesmo navegador: `/?tv=2` usa chaves separadas.

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
| Banco | nenhum. No volume `infratrack_trackinfra_data`: `devices.json` (nome/config de cada TV, sem segredo), `sessions_state.json` (contas e tokens, com `PERSIST_SESSIONS`) e `mosaic_config.json` (config "da casa": molde de TV nova + flag do SGI) |
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
| POST | `/api/auth/session` | cria sessão de QR (TV; corpo opcional `{device_id}`) |
| GET  | `/api/auth/qr/{uuid}.png` | imagem do QR |
| POST | `/api/auth/login` | celular envia credenciais; devolve token de controle |
| POST | `/api/auth/pair` | celular-controle libera TV nova sem senha |
| GET  | `/api/auth/status/{uuid}` | polling da TV (token + `device_id`) |
| GET  | `/api/me` | dono do token (tv/controller, conta, TV) |
| GET  | `/api/fleet` | frota normalizada (Bearer) |
| GET  | `/api/notifications/total` | alertas não lidos (Bearer) |
| GET/POST | `/api/config[?device=]` | config da TV (a própria; o celular passa `device`) |
| GET  | `/api/devices` | TVs e celulares da conta (controle) |
| PATCH/DELETE | `/api/devices/{id}` | renomear / esquecer TV |
| POST | `/api/devices/{id}/disconnect` | derruba só esta TV |
| POST | `/api/devices/{id}/command` | `identify` / `reload` no próximo heartbeat |
| POST | `/api/devices/me/heartbeat` | TV conta o que está exibindo |
| POST | `/api/account/disconnect-all` | derruba todas as TVs e celulares da conta |
| DELETE | `/api/controllers/{id}` | tira o acesso de um celular |

## Segurança e limitações

- Credenciais do FullTrack **nunca** são gravadas em disco. Ficam em memória, na
  sessão do backend, e são usadas para o **re-login automático**: quando o cookie
  do FullTrack cai, o backend refaz o login sozinho e a TV nem percebe.
- O QR expira em 5 min; o token da TV **não expira**. O do celular-controle cai
  após 7 dias sem uso; o do QR "Configurar", após 15 min sem uso.
- A TV volta a exibir o QR quando: é **desconectada pelo celular** (ela mesma ou
  "desconectar tudo"), a **senha é trocada no FullTrack** (o re-login é recusado
  e a CONTA inteira cai — todas as TVs dividem essa senha), ou há **restart do
  backend sem `PERSIST_SESSIONS`**. Nesses casos a API responde 401 com
  `X-Session-Revoked: 1` e a TV vai para o QR na hora. 401 sem o header é
  tolerado algumas vezes; instabilidade do FullTrack devolve 503 e a TV segue
  com o último mosaico na tela.
- Sobreviver a restart exigiria gravar a credencial em disco: **decisão consciente
  de não fazer**, é o trade-off de segurança que mantém a senha só na RAM.
- Sessões em memória: para múltiplas instâncias, migrar `sessions.py` para Redis.
- API do FullTrack é **privada/não documentada** (v3.2.50) — pode mudar sem aviso.
  Verifique o contrato de uso antes de produção.
