"""Aplicación web para rastrear sitios y extraer contactos con progreso en vivo."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional
from uuid import uuid4

from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)

from scraper import ContactScraper, CrawlSettings, export_contacts_to_excel
from enrichment import enrich_contacts, sort_contacts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webapp")

EXPORT_DIR = Path("exports")
EXPORT_DIR.mkdir(exist_ok=True)


@dataclass
class JobData:
    job_id: str
    url: str
    total_steps: int
    status: str = "PENDING"
    progress: float = 0.0
    current_step: int = 0
    label: str = ""
    visited_pages: int = 0
    explored_links: int = 0
    contacts: List[Dict[str, str]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
    file_path: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    finished: bool = False
    urls: List[str] = field(default_factory=list)
    current_url: Optional[str] = None
    current_url_index: int = 0
    site_results: List[Dict[str, object]] = field(default_factory=list)
    notices: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        progress_percent = max(0, min(100, int(round(self.progress * 100))))
        return {
            "job_id": self.job_id,
            "url": self.url,
            "urls": self.urls,
            "status": self.status,
            "finished": self.finished,
            "progress": self.progress,
            "progress_percent": progress_percent,
            "label": self.label,
            "visited_pages": self.visited_pages,
            "explored_links": self.explored_links,
            "errors": self.errors,
            "error_message": self.error_message,
            "contacts": self.contacts if self.finished and self.status != "ERROR" else [],
            "contacts_count": len(self.contacts),
            "current_url": self.current_url,
            "current_url_index": self.current_url_index,
            "total_urls": len(self.urls),
            "site_results": self.site_results,
            "notices": self.notices,
            "download_url": f"/download/{self.job_id}" if self.file_path else None,
        }


app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

executor = ThreadPoolExecutor(max_workers=4)
jobs_lock = Lock()
jobs: Dict[str, JobData] = {}


def store_job(job: JobData) -> None:
    with jobs_lock:
        jobs[job.job_id] = job


def get_job(job_id: str) -> Optional[JobData]:
    with jobs_lock:
        return jobs.get(job_id)


def update_job(job_id: str, **kwargs) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)


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
    job_id = uuid4().hex
    total_steps = max(1, len(urls) * settings.max_pages)
    job = JobData(
        job_id=job_id,
        url=urls[0],
        urls=urls,
        total_steps=total_steps,
    )
    store_job(job)
    executor.submit(run_scraper_job, job_id, urls, settings)
    logger.info("Trabajo %s iniciado para %d urls", job_id, len(urls))
    return jsonify({"job_id": job_id})


@app.get("/status/<job_id>")
def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Trabajo no encontrado"}), 404
    return jsonify(job.to_dict())


@app.get("/download/<job_id>")
def download(job_id: str):
    job = get_job(job_id)
    if not job or not job.file_path:
        abort(404)
    file_path = Path(job.file_path)
    if not file_path.exists():
        abort(404)
    return send_file(file_path, as_attachment=True, download_name=file_path.name)


def run_scraper_job(job_id: str, urls: List[str], base_settings: CrawlSettings) -> None:
    job = get_job(job_id)
    if not job:
        return

    total_urls = len(urls)
    max_pages = base_settings.max_pages
    total_steps = max(1, total_urls * max_pages)
    update_job(job_id, total_steps=total_steps)

    all_contacts: Dict[tuple, Dict[str, str]] = {}
    aggregated_errors: List[str] = []
    site_results: List[Dict[str, object]] = []
    enrichment_notes: List[str] = []
    total_visited = 0
    total_links = 0
    completed_steps = 0
    restricted_sites = 0
    completed_sites = 0
    found_contacts = False

    try:
        for index, url in enumerate(urls):
            current_settings = CrawlSettings(
                base_url=url,
                max_pages=base_settings.max_pages,
                max_depth=base_settings.max_depth,
                request_timeout=base_settings.request_timeout,
                delay_seconds=base_settings.delay_seconds,
                user_agent=base_settings.user_agent,
            )
            scraper = ContactScraper(current_settings)
            base_completed_steps = index * max_pages
            base_visited_pages = total_visited
            pages_for_url = 0
            start_progress = base_completed_steps / total_steps if total_steps else 0.0
            update_job(
                job_id,
                status="RUNNING",
                current_step=base_completed_steps,
                progress=start_progress,
                label=f"Iniciando sitio {index + 1}/{total_urls}",
                visited_pages=total_visited,
                explored_links=total_links,
                current_url=url,
                current_url_index=index + 1,
            )

            def progress_callback(label: str) -> None:
                nonlocal pages_for_url, completed_steps
                pages_for_url = min(pages_for_url + 1, max_pages)
                completed_steps = base_completed_steps + pages_for_url
                progress_value = completed_steps / total_steps
                update_job(
                    job_id,
                    status="RUNNING",
                    current_step=completed_steps,
                    progress=progress_value,
                    label=f"[{index + 1}/{total_urls}] {label}",
                    visited_pages=base_visited_pages + pages_for_url,
                    explored_links=total_links,
                    current_url=url,
                    current_url_index=index + 1,
                )

            result = scraper.run(progress=progress_callback)
            total_visited += result.visited_pages
            total_links += result.explored_links
            completed_sites += 1

            if result.status == "RESTRINGIDO":
                restricted_sites += 1

            if result.errors:
                aggregated_errors.extend(f"[{url}] {err}" for err in result.errors)

            if result.contacts:
                found_contacts = True
                for contact in result.contacts:
                    key = (
                        contact.get("tipo", "").lower(),
                        contact.get("valor", "").lower(),
                        contact.get("url", "").lower(),
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
            completed_steps = min((index + 1) * max_pages, total_steps)
            progress_after_site = completed_steps / total_steps if total_steps else 1.0
            next_url = urls[index + 1] if index + 1 < total_urls else None
            update_job(
                job_id,
                current_step=completed_steps,
                progress=progress_after_site,
                visited_pages=total_visited,
                explored_links=total_links,
                label=f"Sitio {index + 1}/{total_urls} completado",
                current_url=next_url,
                current_url_index=index + 1,
            )

        if found_contacts:
            overall_status = "OK"
        elif restricted_sites == total_urls:
            overall_status = "RESTRINGIDO"
        elif restricted_sites > 0:
            overall_status = "RESTRINGIDO"
        else:
            overall_status = "NO_ENCONTRADO"

        file_path: Optional[str] = None
        contacts_list = list(all_contacts.values())
        if contacts_list:
            try:
                contacts_list, enrichment_notes = enrich_contacts(contacts_list)
                contacts_list = sort_contacts(contacts_list)
            except RuntimeError as exc:
                logger.exception("Error al enriquecer contactos con IA: %s", exc)
                update_job(
                    job_id,
                    status="ERROR",
                    error_message=str(exc),
                    progress=1.0,
                    current_step=total_steps,
                    visited_pages=total_visited,
                    explored_links=total_links,
                    contacts=[],
                    errors=aggregated_errors,
                    completed_at=time.time(),
                    finished=True,
                    label="Error durante el enriquecimiento IA",
                    site_results=site_results,
                    notices=[],
                )
                return
            export_name = EXPORT_DIR / f"contactos_{job_id}.xlsx"
            file_path = export_contacts_to_excel(contacts_list, export_name)

        update_job(
            job_id,
            status=overall_status,
            progress=1.0,
            current_step=total_steps,
            visited_pages=total_visited,
            explored_links=total_links,
            contacts=contacts_list,
            errors=aggregated_errors,
            file_path=file_path,
            completed_at=time.time(),
            finished=True,
            label="Rastreo finalizado",
            site_results=site_results,
            current_url=None,
            current_url_index=completed_sites,
            notices=enrichment_notes,
        )
        logger.info(
            "Trabajo %s finalizado con estado %s (%d contactos)",
            job_id,
            overall_status,
            len(contacts_list),
        )
    except Exception as exc:  # pragma: no cover - logging de fallos inesperados
        logger.exception("Fallo en trabajo %s: %s", job_id, exc)
        update_job(
            job_id,
            status="ERROR",
            error_message=str(exc),
            progress=1.0,
            current_step=total_steps,
            completed_at=time.time(),
            finished=True,
            label="Error durante el rastreo",
        )


if __name__ == "__main__":
    app.run(debug=True)
