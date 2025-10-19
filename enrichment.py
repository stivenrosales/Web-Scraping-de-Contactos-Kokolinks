"""Utilidades para validar y enriquecer contactos con OpenAI."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, Iterable, List, Sequence, Tuple

from ai_client import RetryableError, respond

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = int(os.getenv("OPENAI_ENRICH_MAX_TOKENS", "0"))
TOKENS_PER_CONTACT = int(os.getenv("OPENAI_ENRICH_TOKENS_PER_CONTACT", "0"))
MAX_CONTACTS_PER_REQUEST = int(os.getenv("OPENAI_ENRICH_MAX_CONTACTS", "0"))


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
    enriched: List[Dict[str, str]] = []
    invalid_count = 0
    flagged_count = 0
    aggregated_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    step = len(contacts_list) if MAX_CONTACTS_PER_REQUEST <= 0 else max(1, MAX_CONTACTS_PER_REQUEST)

    for start in range(0, len(contacts_list), step):
        chunk = contacts_list[start : start + step]
        try:
            chunk_results, metadata = _query_contacts_with_retry(chunk)
        except Exception as exc:  # pragma: no cover - depende de servicio externo
            logger.exception(
                "Fallo al enriquecer contactos con IA: %s. Se usarán los datos originales para este bloque.",
                exc,
            )
            notes.append(
                "La IA no pudo validar algunos contactos; se mantienen los datos detectados."
            )
            enriched.extend(dict(contact) for contact in chunk)
            continue

        usage = metadata.get("usage") if isinstance(metadata, dict) else None
        if isinstance(usage, dict):
            for key in aggregated_usage:
                aggregated_usage[key] += usage.get(key, 0)

        for contact, extra in zip(chunk, chunk_results):
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

    if any(aggregated_usage.values()):
        logger.debug(
            "OpenAI tokens usados (total): input=%s output=%s total=%s",
            aggregated_usage["input_tokens"],
            aggregated_usage["output_tokens"],
            aggregated_usage["total_tokens"],
        )

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
    """Solicita enriquecimiento IA en una sola llamada; si falla, divide la lista."""

    if not chunk:
        return [], {}

    prompts = [
        _build_prompt(chunk, strict=False),
        _build_prompt(chunk, strict=True),
    ]
    last_exc: Exception | None = None
    metadata: Dict[str, object] = {}

    if MAX_OUTPUT_TOKENS > 0:
        estimated_tokens = min(
            MAX_OUTPUT_TOKENS,
            max(400, 200 + len(chunk) * TOKENS_PER_CONTACT),
        )
    else:
        estimated_tokens = 0

    for idx, prompt in enumerate(prompts):
        try:
            content, metadata = respond(
                prompt,
                max_tokens=estimated_tokens or None,
                json_mode=False,
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

    if last_exc and len(chunk) > 1:
        logger.info(
            "Dividiendo lote de %d contactos para reintentar debido a: %s",
            len(chunk),
            last_exc,
        )
        mid = len(chunk) // 2
        left, right = chunk[:mid], chunk[mid:]
        left_results, left_meta = _query_contacts_with_retry(left)
        right_results, right_meta = _query_contacts_with_retry(right)
        # combinar metadatos de uso si existen
        usage_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for meta in (left_meta, right_meta):
            if isinstance(meta, dict):
                usage_meta = meta.get("usage")
                if isinstance(usage_meta, dict):
                    for key in usage_totals:
                        usage_totals[key] += usage_meta.get(key, 0)
        combined_meta: Dict[str, object] = (
            {"usage": usage_totals}
            if any(usage_totals.values())
            else {}
        )
        return left_results + right_results, combined_meta

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
        "Devuelve solo JSON válido con formato {\"contacts\":[{\"indice\":num,\"valido\":bool,"
        "\"descripcion\":str,\"motivo\":str}]}. ",
        "No añadas texto extra ni bloques ```.",
        "descripcion debe ser un título breve (≤25 caracteres) que indique el rol del contacto.",
        "descripcion no puede repetir el dato literal (correo/teléfono). Usa frases como 'Email prensa', 'Teléfono soporte', 'Email redacción'.",
        "Si no es posible inferir el rol, usa 'Contacto general'.",
        "motivo debe quedar en blanco cuando el dato sea válido; si no lo es, explica en ≤40 caracteres por qué."
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
