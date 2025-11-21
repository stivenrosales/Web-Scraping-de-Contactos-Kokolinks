## Extractor de contactos web

Aplicación CLI que rastrea un sitio web y sus subpáginas para identificar correos electrónicos y números de teléfono con contexto. Al finalizar, exporta los resultados a un archivo Excel (`.xlsx`).

### Novedades rápidas

- Webhook por defecto: `https://n8n.truly.cl/webhook/180d63f6-70b9-4844-ae38-8a3ed9a43a36` (puedes sobrescribir con `CONTACTS_WEBHOOK_URL`).
- Envío automático solo de contactos validados por IA al webhook (sin selección manual en el frontend).
- Rastreo optimizado para priorizar páginas de contacto/soporte (20 páginas máx., 25 enlaces por página, 0.35s de pausa).

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

1. Abre `http://127.0.0.1:5000` en tu navegador (si el puerto 5000 está ocupado, arranca con otra `port` en `app.run`).
2. Ingresa una o varias URLs (separadas por saltos de línea, comas o punto y coma) y pulsa “Iniciar análisis”.
3. Espera a que finalice el análisis (verás un indicador de proceso). Al completarse, el backend envía automáticamente al webhook solo los contactos validados por IA como relevantes para el sitio. La tabla muestra todos los hallazgos para referencia.

### Webhook de destino

Define la URL del webhook y el tiempo de espera (opcional) mediante variables de entorno antes de arrancar `webapp.py` (útil para despliegues en Vercel u otros servicios):

```bash
export CONTACTS_WEBHOOK_URL="https://n8n.truly.cl/webhook/180d63f6-70b9-4844-ae38-8a3ed9a43a36"
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

- Número máximo de páginas (`max_pages`, valor por defecto 20)
- Profundidad de rastreo (`max_depth`)
- Número máximo de enlaces explorados por página (`max_links_per_page`, valor por defecto 25 y prioriza rutas de contacto/soporte)
- Tiempo de espera por solicitud (`request_timeout`)
- Pausa entre peticiones (`delay_seconds`, por defecto 0.35s)

La IA descarta correos/teléfonos de terceros o dominios ajenos al sitio; solo valida los que parezcan útiles para contactar al dominio analizado.

Los mismos ajustes aplican al frontend web, ya que reutiliza el mismo motor de rastreo.
