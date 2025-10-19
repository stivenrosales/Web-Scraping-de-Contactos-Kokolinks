"""Utilidades para validar y enriquecer contactos con OpenAI."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, Iterable, List, Sequence, Tuple

from ai_client import RetryableError, respond

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = int(os.getenv("OPENAI_ENRICH_MAX_TOKENS", "2400"))
TOKENS_PER_CONTACT = int(os.getenv("OPENAI_ENRICH_TOKENS_PER_CONTACT", "140"))


def enrich_contacts(
    contacts: Sequence[Dict[str, str]],
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Valida y mejora contactos. Devuelve notas si la IA falla."""

    contacts_list = list(contacts)
    if not contacts_list:
        return [], []

    if not os.getenv("OPENAI_API_KEY"):
        note = (
            "OPENAI_API_KEY no está configurada; se devuelven los contactos sin enriquecer."
        )
        logger.warning(note)
        return [dict(contact) for contact in contacts_list], [note]

    notes: List[str] = []
    try:
        chunk_results, metadata = _query_contacts_with_retry(contacts_list)
    except Exception as exc:  # pragma: no cover - depende de servicio externo
        logger.exception(
            "Fallo al enriquecer contactos con IA: %s. Se usará la información original.",
            exc,
        )
        notes.append(
            "No fue posible enriquecer los contactos con OpenAI. "
            "Se muestran los datos originales."
        )
        return [dict(contact) for contact in contacts_list], notes

    usage = metadata.get("usage") if isinstance(metadata, dict) else None
    if isinstance(usage, dict):
        logger.debug(
            "OpenAI tokens usados: input=%s output=%s total=%s",
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("total_tokens"),
        )

    enriched: List[Dict[str, str]] = []
    invalid_count = 0
    flagged_count = 0

    for contact, extra in zip(contacts_list, chunk_results):
        enriched_contact = dict(contact)
        valido = bool(extra.get("valido"))
        enriched_contact["validado"] = valido
        if not valido:
            invalid_count += 1

        descripcion_enriquecida = (extra.get("descripcion") or "").strip()
        if descripcion_enriquecida:
            enriched_contact["descripcion_enriquecida"] = descripcion_enriquecida

        motivo = (extra.get("motivo") or "").strip()
        if motivo:
            flags = set(_as_iterable(enriched_contact.get("flags")))
            flags.add(motivo)
            enriched_contact["flags"] = sorted(flags)
            flagged_count += 1

        enriched.append(enriched_contact)

    if invalid_count:
        notes.append(f"{invalid_count} contacto(s) fueron marcados como no válidos por la IA.")
    if flagged_count and flagged_count != invalid_count:
        notes.append(f"{flagged_count} contacto(s) recibieron observaciones adicionales.")

    return enriched, notes


def sort_contacts(contacts: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    """Ordena contactos priorizando los validados y agrupando por sitio."""

    def key(contact: Dict[str, str]) -> Tuple[bool, str, str, str]:
        valid = bool(contact.get("validado"))
        site = (contact.get("sitio") or "").lower()
        tipo = (contact.get("tipo") or "").lower()
        valor = (contact.get("valor") or "").lower()
        return (not valid, site, tipo, valor)

    return sorted((dict(contact) for contact in contacts), key=key)


def _query_contacts_with_retry(
    chunk: Sequence[Dict[str, str]]
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    """Solicita enriquecimiento IA con un prompt primario y uno estricto si es necesario."""

    if not chunk:
        return [], {}

    prompts = [
        _build_prompt(chunk, strict=False),
        _build_prompt(chunk, strict=True),
    ]
    last_exc: Exception | None = None
    metadata: Dict[str, object] = {}

    estimated_tokens = min(
        MAX_OUTPUT_TOKENS,
        max(400, 200 + len(chunk) * TOKENS_PER_CONTACT),
    )

    for idx, prompt in enumerate(prompts):
        try:
            content, metadata = respond(
                prompt,
                max_tokens=estimated_tokens,
                json_mode=True,
            )
            chunk_results = _parse_contacts_response(content, expected=len(chunk))
            return chunk_results, metadata
        except RetryableError as exc:
            last_exc = exc
            logger.warning(
                "Respuesta IA incompleta (intento %d/%d): %s", idx + 1, len(prompts), exc
            )
            continue
        except Exception as exc:
            last_exc = exc
            logger.exception("Fallo al enriquecer contactos con IA: %s", exc)
            break

    raise last_exc or RuntimeError("No se logró obtener un JSON válido de la IA.")


def _build_prompt(contacts: Sequence[Dict[str, str]], *, strict: bool) -> str:
    ejemplos = []
    for idx, contact in enumerate(contacts, start=1):
        ejemplos.append(
            {
                "indice": idx,
                "tipo": contact.get("tipo", ""),
                "valor": contact.get("valor", ""),
                "descripcion": contact.get("descripcion", ""),
                "pagina": contact.get("url", ""),
            }
        )

    instrucciones = [
        "Evalúa cada contacto y responde únicamente con JSON válido. "
        "Estructura exacta: {\"contacts\":[{\"indice\":num,\"valido\":bool,"
        "\"descripcion\":str,\"motivo\":str}]}. "
        "No presentes razonamientos, explicaciones ni cadenas de pensamiento. "
        "La descripción debe ser corta (≤40 caracteres) y profesional. "
        "Si el dato es inválido, deja descripcion vacía y explica la razón en 'motivo'. "
        "Si es válido pero detectas advertencias, colócalas en 'motivo'."
    ]

    if strict:
        instrucciones.append(
            "Responde exactamente con un objeto JSON válido y nada más. "
            "No incluyas texto adicional, comentarios ni bloques ```."
        )

    payload = {"instrucciones": " ".join(instrucciones), "contactos": ejemplos}
    return json.dumps(payload, ensure_ascii=False)


def _as_iterable(value: Iterable[str] | str | None) -> Iterable[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return value


def _sanitize_response_text(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_contacts_response(text: str, *, expected: int) -> List[Dict[str, object]]:
    """Intenta extraer la clave `contacts` desde un JSON en texto libre."""

    cleaned = _sanitize_response_text(text)
    candidates = [cleaned]

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match and match.group(0) != cleaned:
        candidates.append(match.group(0))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("contacts"), list):
            contacts_list = parsed["contacts"]
            if len(contacts_list) != expected:
                raise ValueError(
                    f"La IA devolvió {len(contacts_list)} elementos, se esperaban {expected}."
                )
            return contacts_list

    snippet = (text or "")[:200]
    raise ValueError(f"No se pudo extraer un JSON válido de la respuesta de la IA: {snippet!r}")
