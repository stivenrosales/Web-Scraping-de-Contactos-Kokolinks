"""
Herramientas principales para rastrear un sitio web y extraer datos de contacto.

Nota: Este módulo depende de bibliotecas externas comunes (`requests`, `beautifulsoup4`)
que deberán instalarse manualmente si aún no están disponibles en el entorno.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

import logging
import re
import time
from collections import deque
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


Contact = Dict[str, str]
ProgressCallback = Callable[[str], None]

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(
    r"""
    (?<!\d)                              # evita continuar cadenas numéricas
    (?:\+\d{1,3}\s*)?                    # prefijo internacional opcional
    (?:\(?\d{2,4}\)?[\s.-]*)?            # código de área opcional
    \d{3,4}[\s.-]*\d{3,4}                # número base
    (?:[\s.-]*\d{2,4})?                  # extensión corta opcional
    (?!\d)                               # evita continuar cadenas numéricas
    """,
    re.VERBOSE,
)

RESTRICTED_STATUS = {401, 403, 429, 503}


@dataclass
class CrawlSettings:
    base_url: str
    max_pages: int = 20
    max_depth: int = 2
    max_links_per_page: int = 25
    request_timeout: int = 10
    delay_seconds: float = 0.35
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_3) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0 Safari/537.36"
    )


@dataclass
class CrawlResult:
    contacts: List[Contact] = field(default_factory=list)
    status: str = "OK"  # OK | NO_ENCONTRADO | RESTRINGIDO
    visited_pages: int = 0
    explored_links: int = 0
    errors: List[str] = field(default_factory=list)


class ContactScraper:
    """Rastrea un sitio web en busca de correos y teléfonos con contexto."""

    def __init__(self, settings: CrawlSettings) -> None:
        self.settings = settings
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": settings.user_agent})
        self._logger = logging.getLogger(self.__class__.__name__)

    def run(self, progress: Optional[ProgressCallback] = None) -> CrawlResult:
        result = CrawlResult()
        base_url = self._ensure_scheme(self.settings.base_url)
        normalized_base = self._normalize_url(base_url)
        queue: deque[Tuple[str, int]] = deque([(normalized_base, 0)])
        visited: Set[str] = set()
        queued: Set[str] = {normalized_base}
        contacts_map: Dict[Tuple[str, str], Contact] = {}
        restricted_detected = False

        while queue and len(visited) < self.settings.max_pages:
            current_url, depth = queue.popleft()
            normalized_url = self._normalize_url(current_url)
            if normalized_url in visited:
                continue
            visited.add(normalized_url)
            if progress:
                progress(self._short_label(normalized_url))

            try:
                response = self._session.get(
                    normalized_url, timeout=self.settings.request_timeout
                )
            except requests.RequestException as exc:
                result.errors.append(f"{normalized_url}: {exc}")
                continue

            result.visited_pages += 1

            if response.status_code in RESTRICTED_STATUS:
                restricted_detected = True
                result.errors.append(
                    f"{normalized_url}: acceso restringido ({response.status_code})"
                )
                continue

            if "text/html" not in response.headers.get("Content-Type", ""):
                self._logger.debug("Contenido no HTML ignorado en %s", normalized_url)
                continue

            page_contacts = self._extract_contacts_from_html(response.text, normalized_url)
            for contact in page_contacts:
                key = (contact["tipo"], contact["valor"])
                if key not in contacts_map:
                    contacts_map[key] = contact

            if depth < self.settings.max_depth:
                links = self._collect_links(response.text, normalized_url, base_url)
                for link in links:
                    if link not in visited and link not in queued:
                        queue.append((link, depth + 1))
                        queued.add(link)
                result.explored_links += len(links)

            time.sleep(self.settings.delay_seconds)

        result.contacts = list(contacts_map.values())
        if restricted_detected and not result.contacts:
            result.status = "RESTRINGIDO"
        elif not result.contacts:
            result.status = "NO_ENCONTRADO"
        else:
            result.status = "OK"

        return result

    def _ensure_scheme(self, url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme:
            return f"https://{url}"
        return url

    def _normalize_url(self, url: str) -> str:
        parsed = urlparse(url)
        cleaned = parsed._replace(fragment="")
        return cleaned.geturl()

    def _short_label(self, url: str, max_len: int = 45) -> str:
        if len(url) <= max_len:
            return url
        return f"...{url[-max_len:]}"

    def _extract_contacts_from_html(self, html: str, page_url: str) -> List[Contact]:
        soup = BeautifulSoup(html, "html.parser")
        contacts: Dict[Tuple[str, str], Contact] = {}

        # Extracción desde enlaces explícitos mailto/tel
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if href.startswith("mailto:"):
                email = href.split(":", 1)[1].split("?")[0]
                email = email.strip()
                if EMAIL_RE.fullmatch(email):
                    desc = self._anchor_description(anchor, email)
                    contacts.setdefault(
                        ("correo", email.lower()),
                        {
                            "tipo": "correo",
                            "valor": email,
                            "descripcion": desc,
                            "url": page_url,
                        },
                    )
            elif href.startswith("tel:"):
                phone = href.split(":", 1)[1]
                phone = phone.strip()
                digits = re.sub(r"\D", "", phone)
                if self._valid_phone_digits(digits):
                    desc = self._anchor_description(anchor, phone)
                    normalized = self._normalize_phone(phone)
                    contacts.setdefault(
                        ("telefono", normalized),
                        {
                            "tipo": "telefono",
                            "valor": normalized,
                            "descripcion": desc,
                            "url": page_url,
                        },
                    )

        text_content = soup.get_text(" ", strip=True)

        for match in EMAIL_RE.finditer(text_content):
            email = match.group()
            key = ("correo", email.lower())
            if key not in contacts:
                desc = self._text_context(text_content, match.start(), match.end(), email)
                contacts[key] = {
                    "tipo": "correo",
                    "valor": email,
                    "descripcion": desc,
                    "url": page_url,
                }

        for match in PHONE_RE.finditer(text_content):
            phone = match.group()
            digits = re.sub(r"\D", "", phone)
            if not self._valid_phone_digits(digits):
                continue
            normalized = self._normalize_phone(phone)
            key = ("telefono", normalized)
            if key not in contacts:
                desc = self._text_context(text_content, match.start(), match.end(), phone)
                contacts[key] = {
                    "tipo": "telefono",
                    "valor": normalized,
                    "descripcion": desc,
                    "url": page_url,
                }

        return list(contacts.values())

    def _anchor_description(self, anchor, value: str) -> str:
        text = anchor.get_text(" ", strip=True)
        title = anchor.get("title", "").strip()
        if text and text.lower() != value.lower():
            return text
        if title:
            return title
        parent = anchor.find_parent(["p", "li", "div", "section"])
        if parent:
            snippet = parent.get_text(" ", strip=True)
            if snippet:
                return self._trim_snippet(snippet, value)
        return f"Encontrado en el enlace de {value}"

    def _text_context(self, text: str, start: int, end: int, value: str) -> str:
        window = 90
        snippet_start = max(0, start - window)
        snippet_end = min(len(text), end + window)
        snippet = text[snippet_start:snippet_end].strip()
        return self._trim_snippet(snippet, value)

    def _trim_snippet(self, snippet: str, value: str, max_len: int = 140) -> str:
        cleaned = re.sub(r"\s+", " ", snippet)
        if value.lower() in cleaned.lower() and len(cleaned) <= max_len:
            return cleaned
        if len(cleaned) > max_len:
            return cleaned[: max_len - 3].rstrip() + "..."
        return cleaned or f"Encontrado en {value}"

    def _normalize_phone(self, phone: str) -> str:
        digits = re.sub(r"\D", "", phone)
        if phone.strip().startswith("+"):
            return f"+{digits}"
        return digits

    def _valid_phone_digits(self, digits: str) -> bool:
        return 7 <= len(digits) <= 15

    def _collect_links(self, html: str, current_url: str, base_url: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: List[str] = []
        base_domain = urlparse(base_url).netloc
        base_clean = base_domain.split(":", 1)[0].lstrip("www.")
        seen: Set[str] = set()
        candidates: List[Tuple[int, str]] = []
        skip_ext = {
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".svg",
            ".ico",
            ".css",
            ".js",
            ".json",
            ".xml",
            ".mp4",
            ".mov",
            ".avi",
            ".zip",
            ".rar",
            ".gz",
            ".tar",
            ".woff",
            ".woff2",
            ".ttf",
        }
        priority_keywords = (
            "contact",
            "contacto",
            "about",
            "nosotros",
            "soporte",
            "support",
            "equipo",
            "team",
            "help",
            "ayuda",
            "ventas",
            "sales",
            "service",
            "servicio",
            "press",
            "prensa",
        )
        penalty_keywords = (
            "blog",
            "news",
            "posts",
            "articulo",
            "article",
            "categoria",
            "category",
            "tag",
            "legal",
            "term",
            "privacy",
        )

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                continue
            absolute = urljoin(current_url, href)
            parsed = urlparse(absolute)
            if parsed.scheme not in ("http", "https"):
                continue
            path_lower = parsed.path.lower()
            if any(path_lower.endswith(ext) for ext in skip_ext):
                continue
            target_domain = parsed.netloc.split(":", 1)[0].lstrip("www.")
            if target_domain and not target_domain.endswith(base_clean):
                continue
            normalized = self._normalize_url(absolute)
            if normalized in seen:
                continue
            score = 0
            if parsed.query:
                score -= 1
            if len(parsed.path.split("/")) > 5:
                score -= 1
            for keyword in priority_keywords:
                if keyword in path_lower:
                    score += 6
            for keyword in penalty_keywords:
                if keyword in path_lower:
                    score -= 2
            score += max(0, 12 - len(path_lower))  # prioriza rutas cortas
            candidates.append((score, normalized))
            seen.add(normalized)

        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, link in candidates[: self.settings.max_links_per_page]:
            links.append(link)
        return links

def export_contacts_to_excel(contacts: Iterable[Contact], filename: str) -> str:
    from pathlib import Path

    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise RuntimeError(
            "openpyxl es requerido para generar archivos Excel. "
            "Instala las dependencias con `pip install -r requirements.txt`."
        ) from exc

    output_path = Path(filename).resolve()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Contactos"

    contacts = list(contacts)
    include_site = any("sitio" in contact for contact in contacts)
    include_enriched = any("descripcion_enriquecida" in contact for contact in contacts)
    include_validated = any("validado" in contact for contact in contacts)
    include_flags = any("flags" in contact for contact in contacts)

    headers = []
    if include_site:
        headers.append("Sitio")
    headers.extend(["Tipo", "Valor", "Descripción"])
    if include_enriched:
        headers.append("Descripción IA")
    headers.append("Página")
    if include_validated:
        headers.append("Validado IA")
    if include_flags:
        headers.append("Notas")
    sheet.append(headers)

    for contact in contacts:
        row = []
        if include_site:
            row.append(contact.get("sitio", ""))
        row.extend(
            [
                contact.get("tipo", ""),
                contact.get("valor", ""),
                contact.get("descripcion", ""),
            ]
        )
        if include_enriched:
            row.append(contact.get("descripcion_enriquecida", ""))
        row.append(contact.get("url", ""))
        if include_validated:
            valid_state = contact.get("validado")
            if valid_state is True:
                row.append("Sí")
            elif valid_state is False:
                row.append("No")
            else:
                row.append("")
        if include_flags:
            flags = contact.get("flags")
            if isinstance(flags, (list, tuple, set)):
                row.append(", ".join(str(flag) for flag in flags))
            else:
                row.append(flags or "")
        sheet.append(row)

    # Ajuste simple de ancho de columnas basado en el contenido
    for column in sheet.columns:
        max_length = max(len(str(cell.value)) if cell.value else 0 for cell in column)
        adjusted_width = min(max_length + 2, 80)
        sheet.column_dimensions[column[0].column_letter].width = adjusted_width

    workbook.save(output_path)
    return str(output_path)
