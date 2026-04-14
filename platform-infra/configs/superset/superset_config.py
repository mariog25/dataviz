# 1. Clave de sesión (Asegúrate de que coincide con el .env si lo usas)
SECRET_KEY = 'superset_secret_key'

# 2. Configuración de Cookies (CRÍTICO para acceso por IP)
SESSION_COOKIE_SAMESITE = None  # Permite enviar la cookie entre diferentes IPs/dominios
SESSION_COOKIE_SECURE = False   # Permite que viaje por HTTP (sin S)
SESSION_COOKIE_HTTPONLY = True  # Protege la cookie de accesos JS

# 3. Desactivar seguridad estricta de cabeceras (Talisman)
# Si no pones esto, Superset intentará forzar HTTPS y rechazará el login por IP
TALISMAN_ENABLED = False 

# 4. Desactivar CSRF para el Login (Para evitar el refresco por token inválido)
WTF_CSRF_ENABLED = False

# 5. Configuración de Proxy (A veces el otro PC es visto como un proxy)
ENABLE_PROXY_FIX = True

FEATURE_FLAGS = {
    "GENERIC_CHART_AXES": True,
    # Permite procesar plantillas y configuraciones extra
    "ENABLE_TEMPLATE_PROCESSING": True,
    # Desbloquea controles avanzados en ECharts
    "PRODUCTION_ONLY_CONFIG_FOR_ECHARTS_EXT_OPTIONS": False,
    # Muy importante para ver la caja de metadatos en gráficos nuevos
    "RAW_QUERY_CONTROL": True,
}

ENABLE_UI_THEME_ADMINISTRATION = True
ENABLE_TEMPLATE_PROCESSING = True
FEATURE_FLAGS = {
    "DISPLAY_MARKDOWN_HTML": True,
}

ESCAPE_MARKDOWN_HTML = False

