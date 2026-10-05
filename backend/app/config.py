from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRACKINFRA_", env_file=".env", extra="ignore")

    # URL pública alcançável pelo celular (usada no QR-code)
    public_base_url: str = "http://localhost:8000"

    # Empresa no FullTrack
    fulltrack_indice: str = "8813"
    fulltrack_emp_slug: str = "8813-impacto-telecomunicacoes"
    fulltrack_base: str = "https://fulltrackapp.com"
    fulltrack_api_base: str = "https://api-fulltrack4.fulltrackapp.com"

    # Tempos de vida (segundos)
    qr_session_ttl: int = 300          # validade do QR-code
    app_token_ttl: int = 0             # sessão da TV: 0 = nunca expira
    # Celular que gerencia a conta (vincula/configura/desconecta TVs): expira
    # depois deste tempo SEM USO. 7 dias: quem cuida das TVs não relogar toda
    # semana, mas celular perdido não fica com acesso eterno. 0 = nunca.
    controller_token_ttl: int = 7 * 24 * 3600

    # --- SGI (origem da tela secundária) ---
    # Sem usuário/senha aqui de propósito: quem habilita é o usuário, na tela de
    # configuração, e a credencial fica só em RAM (ver sgi.py).
    sgi_base_url: str = "https://sgi.impactotelecom.com"
    # ⚠️ SÓ PARA DESENVOLVIMENTO. Em produção ficam VAZIOS: lá quem habilita é o
    # usuário, pela tela, e a credencial vive só em RAM. Aqui existem porque o
    # `--reload` do uvicorn reinicia o processo a cada edição e derrubaria o SGI
    # a cada linha mexida — inviabilizando testar. Preenchidos, o app reconecta
    # sozinho no startup. O `.env.dev` é gitignored.
    sgi_username: str = ""
    sgi_password: str = ""
    # Margem para renovar o token ANTES de ele expirar (segundos). 3600 = renova
    # na última hora de vida, quando o JWT traz `exp`.
    sgi_refresh_margin: int = 3600
    # Intervalo do health check do token (segundos).
    sgi_health_every: int = 1800
    # Filtros da consulta. Ficam aqui (e não no código) porque são política de
    # negócio, não contrato: mudar o escopo do painel não deveria exigir deploy.
    # Na spec do SGI `type` é array — por isso lista, não string.
    sgi_types: list[str] = ["infra"]
    sgi_date_field: str = "scheduled_date"

    # TEMPORÁRIO: grava sessões da TV e credencial do SGI em /data (texto puro,
    # 0600) para sobreviverem a restart/deploy. Ver persist.py.
    persist_sessions: bool = False

    # Persistência do mosaico salvo
    config_path: str = "mosaic_config.json"


    @property
    def emp_login_url(self) -> str:
        return f"{self.fulltrack_base}/emp/{self.fulltrack_emp_slug}"


settings = Settings()
