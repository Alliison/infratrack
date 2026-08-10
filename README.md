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

- Credenciais do FullTrack **nunca** são gravadas em disco (só ficam em memória,
  na sessão do backend). Reiniciar o backend exige novo QR.
- O QR expira em 5 min; o token da TV, em 7 dias (configurável).
- Quando a sessão do FullTrack expira de vez, a TV volta a exibir o QR
  (rescan rápido). Para operação 100% desatendida seria preciso guardar a senha
  (criptografada) — **não implementado por padrão** por ser um trade-off de segurança.
- Sessões em memória: para múltiplas instâncias, migrar `sessions.py` para Redis.
- API do FullTrack é **privada/não documentada** (v3.2.50) — pode mudar sem aviso.
  Verifique o contrato de uso antes de produção.
