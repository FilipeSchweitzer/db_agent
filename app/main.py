"""CLI do pipeline. Uso típico, dentro do container:

    docker compose exec app python -m app.main discover example-client
    docker compose exec app python -m app.main map example-client
    # -> revisar e salvar output/example-client/mapeamento_para_revisao.xlsx
    docker compose exec app python -m app.main generate example-client
    docker compose exec app python -m app.main generate example-client --dry-run
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
    total_tables = 0

    def progress(position: int, total: int, table: str) -> None:
        nonlocal total_tables
        total_tables = total
        print(f"[dim]({position}/{total})[/dim] mapeando [bold]{table}[/bold]")

    path = orchestrator.run_mapping(client, on_table=progress)
    print(f"[green]Planilha de revisão gerada:[/green] {path}")
    print(f"[dim]{total_tables} tabela(s) de destino mapeada(s).[/dim]")
    print("[yellow]Revise a coluna 'status' antes de rodar 'generate'.[/yellow]")


@app.command()
def generate(
    client: str,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Só valida o mapeamento e mostra o plano; não escreve script executável.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Gera mesmo se o mapping.json não bater com o que foi revisado.",
    ),
):
    """Fase 3 — gera os artefatos de migração a partir do mapeamento APROVADO."""
    path = orchestrator.run_etl_generation(client, dry_run=dry_run, force=force)
    if dry_run:
        print(f"[green]Plano validado:[/green] {path}")
        print("[yellow]Nada foi executado nem gerado além do plano.[/yellow]")
        return
    print(f"[green]Artefatos gerados em[/green] {path.parent}")
    print("[dim]Ordem: etl_schema.sql -> etl_script.sql -> etl_constraints.sql "
          "-> etl_validation.sql[/dim]")


if __name__ == "__main__":
    app()
