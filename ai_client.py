"""Cliente centralizado de OpenAI con reintentos y configuración para serverless."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, Tuple, TypeVar

from openai import OpenAI

logger = logging.getLogger(__name__)

_client: OpenAI | None = None
MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")


class RetryableError(Exception):
    """Error usado para indicar que se agotaron los reintentos."""


T = TypeVar("T")


def _retry(fn: Callable[[], T], *, attempts: int = 3, backoff: float = 0.8) -> T:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # pragma: no cover - dependiente de API externa
            if isinstance(exc, RetryableError):
                last = exc
                sleep = backoff * (2**attempt)
                logger.warning("OpenAI retryable error: %s. Retry in %.2fs", exc, sleep)
                time.sleep(sleep)
                continue
            msg = str(exc).lower()
            if any(
                token in msg
                for token in ("rate", "timeout", "overloaded", "temporarily", "quota")
            ):
                last = exc
                sleep = backoff * (2**attempt)
                logger.warning("OpenAI temporal error: %s. Retry in %.2fs", exc, sleep)
                time.sleep(sleep)
                continue
            raise
    if last:
        raise last
    raise RetryableError("Reintentos agotados")


def respond(
    prompt: str,
    *,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """Envía un prompt y devuelve el texto plano junto con metadatos."""

    def _get_client() -> OpenAI:
        """Obtiene una instancia reutilizable del cliente OpenAI."""

        global _client
        if _client is None:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY no está configurada; no se puede contactar OpenAI."
                )
            _client = OpenAI(api_key=api_key)
        return _client

    def call() -> Tuple[str, Dict[str, Any]]:
        kwargs: Dict[str, Any] = {
            "model": MODEL,
            "input": prompt,
        }
        if max_tokens:
            kwargs["max_output_tokens"] = max_tokens

        response = _get_client().responses.create(**kwargs)
        try:
            dumped = response.model_dump()
        except AttributeError:  # pragma: no cover - versiones antiguas
            dumped = response
        logger.debug("OpenAI response raw: %s", dumped)
        texts: list[str] = []
        outputs = dumped.get("output") if isinstance(dumped, dict) else None
        if isinstance(outputs, list):
            for item in outputs:
                status = item.get("status") if isinstance(item, dict) else None
                if status and status != "completed":
                    logger.warning(
                        "Subrespuesta incompleta detectada: status=%s item=%s", status, item
                    )
                    incomplete_text = item.get("content") if isinstance(item, dict) else None
                    if incomplete_text:
                        logger.warning("Contenido parcial recibido: %s", incomplete_text)
                    continue
                contents = item.get("content") if isinstance(item, dict) else None
                if not isinstance(contents, list):
                    continue
                for content in contents:
                    if not isinstance(content, dict):
                        continue
                    text = content.get("text")
                    if isinstance(text, str):
                        texts.append(text)
                        continue
                    if isinstance(text, dict):
                        for key in ("value", "text"):
                            value = text.get(key)
                            if isinstance(value, str):
                                texts.append(value)
                                break
                            if isinstance(value, dict):
                                nested = value.get("value") or value.get("text")
                                if isinstance(nested, str):
                                    texts.append(nested)
                                    break
                    if content.get("type") == "output_text":
                        value = content.get("value") or content.get("text")
                        if isinstance(value, str):
                            texts.append(value)
                            continue
                    for key in ("value", "data"):
                        candidate = content.get(key)
                        if isinstance(candidate, str):
                            texts.append(candidate)
                        elif isinstance(candidate, dict):
                            nested = candidate.get("value") or candidate.get("text")
                            if isinstance(nested, str):
                                texts.append(nested)

        if not texts:
            # Fallback a objetos Python en respuesta (por compatibilidad)
            for item in getattr(response, "output", []) or []:
                for content in getattr(item, "content", []) or []:
                    text_attr = getattr(content, "text", None)
                    if isinstance(text_attr, str):
                        texts.append(text_attr)
                    elif hasattr(text_attr, "value"):
                        value = getattr(text_attr, "value", None)
                        if isinstance(value, str):
                            texts.append(value)
                    value_attr = getattr(content, "value", None)
                    if isinstance(value_attr, str):
                        texts.append(value_attr)

        combined = "".join(texts).strip()
        if not combined:
            fallback = getattr(response, "output_text", "") or ""
            combined = fallback.strip()
        if not combined and isinstance(dumped, dict):
            text_conf = dumped.get("text")
            if isinstance(text_conf, dict):
                conf_val = text_conf.get("value") or text_conf.get("text")
                if isinstance(conf_val, str):
                    combined = conf_val.strip()
        if not combined and isinstance(dumped, dict):
            messages = dumped.get("output")
            if isinstance(messages, list):
                for item in messages:
                    if isinstance(item, dict):
                        for content in item.get("content") or []:
                            if isinstance(content, dict):
                                text_val = content.get("text")
                                if isinstance(text_val, str) and text_val.strip():
                                    combined = text_val.strip()
                                    break
                        if combined:
                            break

        logger.debug("OpenAI texto combinado final: %r", combined)

        metadata: Dict[str, Any] = {
            "status": dumped.get("status") if isinstance(dumped, dict) else None,
            "incomplete_details": dumped.get("incomplete_details")
            if isinstance(dumped, dict)
            else None,
            "usage": dumped.get("usage") if isinstance(dumped, dict) else None,
        }

        status = metadata.get("status")
        incomplete = metadata.get("incomplete_details") or {}
        reason = None
        if isinstance(incomplete, dict):
            reason = incomplete.get("reason")

        if status and status != "completed":
            logger.warning(
                "Respuesta marcada como '%s'. Contenido combinado: %r", status, combined[:400]
            )
            if not combined:
                raise RetryableError(f"Respuesta incompleta (status={status})")
            if not combined.strip().endswith("}"):
                raise RetryableError(f"Respuesta incompleta (status={status})")
        if reason:
            logger.warning(
                "OpenAI reporta motivo de incompletitud '%s'. Contenido combinado: %r",
                reason,
                combined[:400],
            )
            if not combined:
                raise RetryableError(f"Respuesta incompleta (motivo={reason})")
            if not combined.strip().endswith("}"):
                raise RetryableError(f"Respuesta incompleta (motivo={reason})")
        if not combined:
            raise RetryableError("Respuesta vacía de OpenAI")

        return combined, metadata

    return _retry(call)
