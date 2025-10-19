"""Utilidades para validar y enriquecer contactos con OpenAI."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, Iterable, List, Sequence, Tuple

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - dependencia opcional
    OpenAI = None  # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
MAX_BATCH_SIZE = int(os.getenv("OPENAI_ENRICH_BATCH", "5"))


def enrich_contacts(
    contacts: Sequence[Dict[str, str]],
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Valida y mejora contactos. Lanza RuntimeError si no puede usar la IA."""

    if not contacts:
        return [], []

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY no está configurada. Define la variable de entorno con tu token."
        )

    if OpenAI is None:
        raise RuntimeError(
            "La librería 'openai' no está instalada. Ejecuta `pip install openai`."
        )

    try:
        client = OpenAI(api_key=api_key)
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"No fue posible inicializar el cliente de OpenAI: {exc}") from exc

    batch_size = max(1, min(MAX_BATCH_SIZE, 10))
    enriched: List[Dict[str, str]] = []
    invalid_count = 0
    flagged_count = 0

    for start in range(0, len(contacts), batch_size):
        chunk = contacts[start : start + batch_size]
        prompt = _build_prompt(chunk)
        try:
            response = client.responses.create(
                model=model,
                input=prompt,
                temperature=temperature,
                max_output_tokens=800,
            )
            content = response.output[0].content[0].text  # type: ignore[index]
            content = _sanitize_response_text(content)
        except Exception as exc:  # pragma: no cover - dependiente del servicio externo
            raise RuntimeError(f"Error al solicitar enriquecimiento a OpenAI: {exc}") from exc

        try:
            parsed = json.loads(content)
            chunk_results = parsed.get("contacts", [])
            if len(chunk_results) != len(chunk):
                raise ValueError("La respuesta no coincide con el número de contactos enviados.")
        except Exception as exc:  # pragma: no cover - validación robusta
            raise RuntimeError(f"La respuesta de OpenAI no es válida: {exc}") from exc

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

    notes: List[str] = []
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


def _build_prompt(contacts: Sequence[Dict[str, str]]) -> str:
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

    payload = {
        "instrucciones": (
            "Evalúa si cada dato de contacto parece válido y redacta una descripción breve y profesional. "
            "Si detectas que el dato es inválido o sospechoso, márcalo como no válido e indica la razón en 'motivo'. "
            "Responde en JSON con la estructura {\"contacts\": [{\"indice\": n, \"valido\": bool, \"descripcion\": str, \"motivo\": str}]}."
        ),
        "contactos": ejemplos,
    }
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
