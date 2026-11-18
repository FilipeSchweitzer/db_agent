"""CLI do pipeline. Uso típico, dentro do container:

    docker compose exec app python -m app.main discover example-client
    docker compose exec app python -m app.main map example-client
    # -> revisar e salvar output/example-client/mapeamento_para_revisao.xlsx
    docker compose exec app python -m app.main generate example-client
"""
from __future__ import annotations

import typer
from rich import print

from app import orchestrator

app = typer.Typer(help="Pipeline de migração de dados legados.")


@app.command()
def discover(client: str):
    """Fase 1 — perfila o banco legado e gera discovery_report.json."""
    path = orchestrator.run_discovery(client)
    print(f"[green]Relatório de discovery gerado:[/green] {path}")


@app.command()
def map(client: str):
    """Fase 2 — gera o mapeamento proposto e a planilha de revisão."""
    path = orchestrator.run_mapping(client)
    print(f"[green]Planilha de revisão gerada:[/green] {path}")
    print("[yellow]Revise a coluna 'status' antes de rodar 'generate'.[/yellow]")


@app.command()
def generate(client: str):
    """Fase 3 — gera o script de ETL a partir do mapeamento APROVADO."""
    path = orchestrator.run_etl_generation(client)
    print(f"[green]Script de ETL gerado:[/green] {path}")


if __name__ == "__main__":
    app()
