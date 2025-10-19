"""Interfaz de línea de comandos para el extractor de contactos."""

from __future__ import annotations

from scraper import ContactScraper, CrawlSettings, export_contacts_to_excel
from enrichment import enrich_contacts, sort_contacts
from progress import TextProgressBar


def main() -> None:
    url = input("Ingresa la URL del sitio a analizar: ").strip()
    if not url:
        print("No se proporcionó una URL. Saliendo.")
        return

    settings = CrawlSettings(base_url=url)
    scraper = ContactScraper(settings)
    progress = TextProgressBar(total=settings.max_pages)

    try:
        result = scraper.run(progress=progress.step)
    finally:
        progress.close("Rastreo finalizado")

    print(f"Páginas visitadas: {result.visited_pages}")
    if result.explored_links:
        print(f"Enlaces evaluados: {result.explored_links}")

    if result.status == "RESTRINGIDO":
        print("El sitio parece tener restricciones de acceso para el scraping.")
        if result.errors:
            print("Detalles:")
            for error in result.errors:
                print(f" - {error}")
        return

    if not result.contacts:
        print("No se encontraron datos de contacto.")
        if result.errors:
            print("Detalles:")
            for error in result.errors:
                print(f" - {error}")
        return

    try:
        enriched_contacts, notes = enrich_contacts(result.contacts)
    except RuntimeError as exc:
        print(f"Error durante el enriquecimiento IA: {exc}")
        return

    enriched_contacts = sort_contacts(enriched_contacts)
    if notes:
        print("Notas del enriquecimiento IA:")
        for note in notes:
            print(f" - {note}")

    try:
        output_path = export_contacts_to_excel(enriched_contacts, "contactos.xlsx")
    except RuntimeError as exc:
        print(str(exc))
        return

    print(f"Se encontraron {len(enriched_contacts)} contactos.")
    print(f"Archivo generado: {output_path}")


if __name__ == "__main__":
    main()
