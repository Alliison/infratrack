FROM python:3.12-slim

WORKDIR /app

# Sem gcc/libpq: este servico nao fala com o Postgres da VM 101, e todas as
# dependencias (Pillow do qrcode, uvloop/httptools do uvicorn) tem wheel
# pronta para cp312 — nada compila aqui.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# ⚠️ A arvore precisa espelhar a do repositorio. O main.py acha o front por
# caminho relativo ao proprio arquivo:
#   parents[2] de /app/backend/app/main.py  ->  /app  ->  /app/frontend
# Achatar backend/ na raiz quebraria o FileResponse e o mount de /assets.
COPY backend/ backend/
COPY frontend/ frontend/

EXPOSE 8300

# CMD de fallback: o compose repete o comando com --proxy-headers.
WORKDIR /app/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300"]
