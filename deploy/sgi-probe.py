#!/usr/bin/env python3
"""Explora o contrato do SGI para desenhar a tela secundaria do TrackInfra.

Nao le credencial de nenhum outro servico e nao grava nada: usuario e senha vem
do ambiente, e nem a senha nem o token sao impressos.

Uso:
    SGI_USER='seu.usuario' SGI_PASS='sua-senha' python3 deploy/sgi-probe.py

Opcional:
    SGI_BASE=https://sgi-dev.impactotelecom.com   (default)
    SGI_DATE=2026-09-28                           (default: hoje)
"""
import json, os, sys, urllib.error, urllib.parse, urllib.request
from datetime import date

BASE = os.environ.get("SGI_BASE", "https://sgi-dev.impactotelecom.com").rstrip("/")
USER = os.environ.get("SGI_USER", "")
PASS = os.environ.get("SGI_PASS", "")
DAY = os.environ.get("SGI_DATE") or date.today().isoformat()

if not (USER and PASS):
    sys.exit("defina SGI_USER e SGI_PASS no ambiente (veja o docstring)")


def req(path, data=None, token=None, params=None):
    url = f"{BASE}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"accept": "application/json"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=body, headers=headers), timeout=30
        ) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def shape(value, depth=0, path="$"):
    """Imprime o ESQUEMA do JSON (chaves e tipos), nao o volume de dados."""
    pad = "  " * depth
    if isinstance(value, dict):
        print(f"{pad}{path}: objeto com {len(value)} chaves")
        for k, v in list(value.items())[:25]:
            if isinstance(v, (dict, list)):
                shape(v, depth + 1, k)
            else:
                sample = str(v)[:60]
                print(f"{pad}  {k}: {type(v).__name__} = {sample}")
    elif isinstance(value, list):
        print(f"{pad}{path}: lista com {len(value)} itens")
        if value:
            shape(value[0], depth + 1, f"{path}[0]")


print(f"base: {BASE}   data: {DAY}   usuario: {USER[:3]}***\n")

st, body = req("api/core/v1/auth/login", data={
    "grant_type": "password", "username": USER, "password": PASS,
    "scope": "admin", "client_id": "", "client_secret": "",
})
print(f"POST /api/core/v1/auth/login -> HTTP {st}")
if st != 200:
    sys.exit(f"  login falhou: {body[:300]}")
payload = json.loads(body)
tok = payload.get("access_token", "")
print(f"  chaves da resposta: {sorted(payload)}")
print(f"  access_token: {len(tok)} chars")

# O plano pede refresh proativo na ultima hora de vida: isso depende do 'exp' do
# JWT. Aqui so conferimos se ele existe e quanto tempo o token dura.
try:
    import base64, time
    seg = tok.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    print(f"  claims do JWT: {sorted(claims)}")
    if "exp" in claims:
        horas = (claims["exp"] - time.time()) / 3600
        print(f"  exp presente: token vive ~{horas:.1f}h  (da' para refresh proativo)")
    else:
        print("  SEM 'exp' no JWT: refresh proativo por expiracao nao e' possivel")
except Exception as e:  # noqa: BLE001
    print(f"  nao foi possivel decodificar o JWT ({e}) — refresh so reativo, via /auth/me")

st, body = req("api/core/v1/auth/me", token=tok)
print(f"\nGET /api/core/v1/auth/me -> HTTP {st}")
if st == 200:
    shape(json.loads(body), path="/me")

st, body = req("api/core/v1/reports/service-orders/summary", token=tok, params={
    "startDate": DAY, "endDate": DAY, "dateField": "scheduled_date", "type": "infra",
})
print(f"\nGET /api/core/v1/reports/service-orders/summary -> HTTP {st}")
if st == 200:
    data = json.loads(body)
    with open("/tmp/sgi-summary.json", "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("  JSON completo salvo em /tmp/sgi-summary.json")
    shape(data, path="summary")
else:
    print(f"  {body[:400]}")

# Caca ao endpoint de equipe: nenhum dos clientes existentes tem um.
print("\n=== procurando o endpoint de EQUIPE ===")
for path in ("api/core/v1/teams/", "api/core/v1/team/", "api/core/v1/sector/",
             "api/core/v1/squads/", "api/core/v1/technicians/",
             "api/core/v1/groups/", "api/core/v1/work-teams/"):
    st, body = req(path, token=tok, params={"size": 5})
    marca = "  <<< existe" if st == 200 else ""
    print(f"  {st}  /{path}{marca}")
    if st == 200:
        try:
            shape(json.loads(body), depth=1, path=path)
        except Exception:  # noqa: BLE001
            print(f"    (resposta nao-JSON: {body[:80]})")
