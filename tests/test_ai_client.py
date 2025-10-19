import os

from ai_client import respond


def test_smoke():
    if not os.getenv("OPENAI_API_KEY"):
        return
    out, _ = respond("Responde con la palabra OK.")
    assert "OK" in out.upper()
