"""Aplicación web para rastrear sitios y extraer contactos."""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Dict, List

import requests
from flask import Flask, jsonify, render_template, request

from scraper import CrawlSettings, ContactScraper
from enrichment import enrich_contacts, sort_contacts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webapp")

CONTACTS_WEBHOOK_URL = os.getenv(
    "CONTACTS_WEBHOOK_URL",
    "https://n8n.truly.cl/webhook/1743df36-76f8-4dc9-b5c3-05d7fcf6ea5e",
)
try:
    CONTACTS_WEBHOOK_TIMEOUT = float(os.getenv("CONTACTS_WEBHOOK_TIMEOUT", "15"))
except ValueError:  # pragma: no cover - valores no numéricos en entorno
    CONTACTS_WEBHOOK_TIMEOUT = 15.0

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.post("/start")
def start_job() -> tuple:
    data = request.get_json(silent=True) or request.form
    raw_urls = (data.get("urls") or data.get("url") or "").strip()
    if not raw_urls:
        return jsonify({"error": "Debes proporcionar al menos una URL."}), 400

    separators = r"[\n,;]"
    urls = [u.strip() for u in re.split(separators, raw_urls) if u.strip()]
    if not urls:
        return jsonify({"error": "No se encontraron URLs válidas."}), 400

    settings = CrawlSettings(base_url=urls[0])
    start_time = time.time()
    try:
        result = process_urls(urls, settings)
    except Exception as exc:  # pragma: no cover - logging de fallos inesperados
        logger.exception("Fallo al procesar las URLs %s: %s", urls, exc)
        return (
            jsonify(
                {
                    "error": "Ocurrió un error inesperado durante el rastreo.",
                    "details": str(exc),
                }
            ),
            500,
        )

    duration = time.time() - start_time
    result["duration_seconds"] = round(duration, 2)
    result["urls"] = urls
    logger.info(
        "Trabajo completado para %d urls en %.2fs (estado %s, %d contactos)",
        len(urls),
        duration,
        result.get("status"),
        result.get("contacts_count", 0),
    )
    return jsonify(result)


@app.post("/send")
def send_contacts():
    data = request.get_json(silent=True) or {}
    contacts = data.get("contacts")
    if not isinstance(contacts, list) or not contacts:
        return jsonify({"error": "Selecciona al menos un contacto para enviar."}), 400

    if not CONTACTS_WEBHOOK_URL:
        return (
            jsonify(
                {
                    "error": "El webhook de contactos no está configurado en el servidor.",
                }
            ),
            500,
        )

    payload: Dict[str, object] = {
        "job_id": data.get("job_id"),
        "total_contacts": len(contacts),
        "contacts": contacts,
    }
    metadata = data.get("metadata")
    if metadata:
        payload["metadata"] = metadata

    try:
        response = requests.post(
            CONTACTS_WEBHOOK_URL,
            json=payload,
            timeout=CONTACTS_WEBHOOK_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:  # pragma: no cover - depende de red externa
        logger.exception("No se pudo entregar contactos al webhook: %s", exc)
        status_code = getattr(getattr(exc, "response", None), "status_code", 502)
        detail = ""
        if getattr(exc, "response", None) is not None:
            try:
                body = exc.response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("error"):
                detail = str(body.get("error"))
            elif isinstance(body, dict) and body.get("message"):
                detail = str(body.get("message"))
            else:
                detail = exc.response.text or ""
        if not detail:
            detail = str(exc)
        return (
            jsonify(
                {
                    "error": "No se pudo enviar los contactos al webhook.",
                    "details": detail,
                    "status": status_code,
                }
            ),
            502,
        )

    return jsonify({"ok": True})


def process_urls(urls: List[str], base_settings: CrawlSettings) -> Dict[str, object]:
    total_urls = len(urls)
    all_contacts: Dict[tuple, Dict[str, str]] = {}
    aggregated_errors: List[str] = []
    site_results: List[Dict[str, object]] = []
    enrichment_notes: List[str] = []
    total_visited = 0
    total_links = 0
    restricted_sites = 0
    found_contacts = False

    for url in urls:
        current_settings = CrawlSettings(
            base_url=url,
            max_pages=base_settings.max_pages,
            max_depth=base_settings.max_depth,
            request_timeout=base_settings.request_timeout,
            delay_seconds=base_settings.delay_seconds,
            user_agent=base_settings.user_agent,
        )
        scraper = ContactScraper(current_settings)
        result = scraper.run()
        total_visited += result.visited_pages
        total_links += result.explored_links

        if result.status == "RESTRINGIDO":
            restricted_sites += 1

        if result.errors:
            aggregated_errors.extend(f"[{url}] {err}" for err in result.errors)

        if result.contacts:
            found_contacts = True
            for contact in result.contacts:
                key = (
                    (contact.get("tipo") or "").lower(),
                    (contact.get("valor") or "").lower(),
                    (contact.get("url") or "").lower(),
                )
                if key not in all_contacts:
                    enriched = dict(contact)
                    enriched.setdefault("sitio", url)
                    all_contacts[key] = enriched

        site_results.append(
            {
                "url": url,
                "status": result.status,
                "contacts": len(result.contacts),
                "errors": result.errors,
            }
        )

    if found_contacts:
        overall_status = "OK"
    elif restricted_sites == total_urls and total_urls > 0:
        overall_status = "RESTRINGIDO"
    elif restricted_sites > 0:
        overall_status = "RESTRINGIDO"
    else:
        overall_status = "NO_ENCONTRADO"

    contacts_list = list(all_contacts.values())

    if contacts_list:
        try:
            contacts_list, enrichment_notes = enrich_contacts(contacts_list)
            contacts_list = sort_contacts(contacts_list)
        except Exception as exc:  # pragma: no cover - seguridad adicional
            logger.exception("Error inesperado al enriquecer contactos: %s", exc)
            enrichment_notes.append(
                "Ocurrió un error inesperado durante el enriquecimiento IA. "
                "Se muestran los datos originales."
            )
    return {
        "status": overall_status,
        "contacts": contacts_list,
        "contacts_count": len(contacts_list),
        "visited_pages": total_visited,
        "explored_links": total_links,
        "errors": aggregated_errors,
        "site_results": site_results,
        "notices": enrichment_notes,
    }


if __name__ == "__main__":
    app.run(debug=True)
