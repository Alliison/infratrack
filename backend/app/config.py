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

    # Persistência do mosaico salvo
    config_path: str = "mosaic_config.json"

    @property
    def emp_login_url(self) -> str:
        return f"{self.fulltrack_base}/emp/{self.fulltrack_emp_slug}"


settings = Settings()
