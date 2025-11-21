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
    "https://n8n.truly.cl/webhook/180d63f6-70b9-4844-ae38-8a3ed9a43a36",
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
    job_id = f"job-{int(time.time() * 1000)}"
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
    result["job_id"] = job_id

    validated_contacts = [
        contact for contact in result.get("contacts", []) if contact.get("validado") is True
    ]
    result["validated_contacts_count"] = len(validated_contacts)

    result["delivery"] = {"sent": False, "status": None, "error": None}
    if validated_contacts:
        delivery_meta = {
            "origin": request.host_url.rstrip("/"),
            "urls": urls,
        }
        delivery = deliver_contacts(
            validated_contacts,
            metadata=delivery_meta,
            job_id=job_id,
        )
        result["delivery"] = delivery
        if delivery.get("error"):
            logger.warning("Envio automático falló: %s", delivery["error"])
        else:
            logger.info("Envío automático exitoso: %d contacto(s)", len(validated_contacts))

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
        return jsonify({"error": "Debes enviar al menos un contacto válido."}), 400
    delivery = deliver_contacts(
        contacts,
        metadata=data.get("metadata"),
        job_id=data.get("job_id"),
    )
    status_code = 200 if delivery.get("sent") else delivery.get("status", 502)
    return jsonify(delivery), status_code


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
            max_links_per_page=base_settings.max_links_per_page,
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


def deliver_contacts(
    contacts: List[Dict[str, object]],
    *,
    metadata: Dict[str, object] | None = None,
    job_id: str | None = None,
) -> Dict[str, object]:
    if not CONTACTS_WEBHOOK_URL:
        return {
            "sent": False,
            "error": "El webhook de contactos no está configurado en el servidor.",
            "status": 500,
        }
    if not contacts:
        return {
            "sent": False,
            "error": "No hay contactos para enviar al webhook.",
            "status": 400,
        }

    payload: Dict[str, object] = {
        "job_id": job_id,
        "total_contacts": len(contacts),
        "contacts": contacts,
    }
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
        return {
            "sent": False,
            "error": "No se pudo enviar los contactos al webhook.",
            "details": detail,
            "status": status_code,
        }

    return {"sent": True, "status": response.status_code}


if __name__ == "__main__":
    app.run(debug=True)
