## Extractor de contactos web

Aplicación CLI que rastrea un sitio web y sus subpáginas para identificar correos electrónicos y números de teléfono con contexto. Al finalizar, exporta los resultados a un archivo Excel (`.xlsx`).

### Requisitos

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Uso

```bash
python main.py
```

1. La herramienta solicitará la URL inicial.
2. Mostrará una barra de progreso basada en el número máximo de páginas que intentará visitar.
3. Al finalizar:
   - Si encuentra contactos, generará el archivo `contactos.xlsx`.
   - Si no encuentra datos, informará `No se encontraron datos de contacto`.
   - Si detecta bloqueos (códigos 401, 403, 429, etc.), reportará el estado `restringido`.

### Aplicación web con frontend

```bash
python webapp.py
```

1. Abre `http://127.0.0.1:5000` en tu navegador.
2. Ingresa una o varias URLs (separadas por saltos de línea, comas o punto y coma) y pulsa “Iniciar análisis”.
3. Espera a que finalice el análisis (verás un indicador de proceso). Al completarse, elige qué contactos enviar; todos vienen marcados por defecto y el backend reenviará la selección al webhook configurado.

### Webhook de destino

Define la URL del webhook y el tiempo de espera (opcional) mediante variables de entorno antes de arrancar `webapp.py` (útil para despliegues en Vercel u otros servicios):

```bash
export CONTACTS_WEBHOOK_URL="https://n8n.truly.cl/webhook/1743df36-76f8-4dc9-b5c3-05d7fcf6ea5e"
# opcional: segundos que esperará la petición al webhook
export CONTACTS_WEBHOOK_TIMEOUT=15
```

Si la variable `CONTACTS_WEBHOOK_URL` no está configurada, se usará la URL anterior como predeterminada.

### Validación con IA

El sistema valida y describe cada contacto usando OpenAI (por defecto GPT-5 mini mediante la Responses API). Antes de ejecutar el CLI o la aplicación web debes definir `OPENAI_API_KEY`. Opcionalmente puedes indicar otro modelo con `OPENAI_MODEL`:

```bash
export OPENAI_API_KEY="tu_token"
# Opcional:
export OPENAI_MODEL="gpt-5-mini"
```

⚠️ Mantén tus credenciales fuera del repositorio (usa variables de entorno o un archivo `.env` que no subas al control de versiones). Si la clave no está configurada, el sistema omitirá el enriquecimiento automático y avisará en la interfaz.

### Personalización rápida

Modifica los parámetros en `CrawlSettings` dentro de `main.py` para cambiar:

- Número máximo de páginas (`max_pages`)
- Profundidad de rastreo (`max_depth`)
- Tiempo de espera por solicitud (`request_timeout`)
- Pausa entre peticiones (`delay_seconds`)

Los mismos ajustes aplican al frontend web, ya que reutiliza el mismo motor de rastreo.
