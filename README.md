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
3. Visualiza el progreso en tiempo real por sitio y, cuando el proceso termine, descarga el Excel con los resultados desde la misma página (se guarda también en `exports/`).

### Validación con IA

El sistema valida y describe cada contacto usando OpenAI. Debes definir la variable de entorno `OPENAI_API_KEY` antes de ejecutar el CLI o la aplicación web. También puedes ajustar modelo y temperatura:

```bash
export OPENAI_API_KEY="tu_token"
# Opcional:
export OPENAI_MODEL="gpt-4o-mini"
export OPENAI_TEMPERATURE="0.2"
```

⚠️ Mantén tus credenciales fuera del repositorio (usa variables de entorno o un archivo `.env` que no subas al control de versiones). Si la clave no está configurada, el proceso se detendrá marcando un error.

### Personalización rápida

Modifica los parámetros en `CrawlSettings` dentro de `main.py` para cambiar:

- Número máximo de páginas (`max_pages`)
- Profundidad de rastreo (`max_depth`)
- Tiempo de espera por solicitud (`request_timeout`)
- Pausa entre peticiones (`delay_seconds`)

Los mismos ajustes aplican al frontend web, ya que reutiliza el mismo motor de rastreo.
