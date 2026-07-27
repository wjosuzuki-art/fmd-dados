#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Consolidação: junta a receita filtrada do FMD com a execução de despesa do
FMD, para responder "quanto entrou vs. quanto saiu", ano a ano.

Não faz nenhuma chamada de rede -- só lê os CSVs que o coletor_sof_fmd.py já
produziu em dados_sof_fmd/ (raw/, filtered/).

CUIDADO COM DUPLA CONTAGEM NA RECEITA:
A árvore de /contasReceita é hierárquica e a API parece reportar o valor já
ACUMULADO em cada nível (o nó pai soma os filhos). Se dois códigos batidos
como "FMD" forem pai e filho da mesma família, somar os dois conta o mesmo
dinheiro 2x. Este script detecta isso comparando a "raiz significativa" de
cada código (o código sem os zeros de preenchimento à direita): se a raiz de
um código é prefixo da raiz de outro código também batido, o mais "raso" é
tratado como pai e É EXCLUÍDO da soma seguro (mantido no CSV detalhado, só
não entra no total).

A soma "segura" (`calcular_total_seguro`) portanto:
  - ignora códigos que têm um "descendente" também batido (evita contar o
    rollup do pai em cima do filho)
  - ignora explicitamente linhas classificadas como "total_subtotal" (linhas
    tipo "TOTAL DEDUÇÕES ...", que já são a soma de outras linhas)
  - soma "bruto_ou_outro" e "multas_e_juros" com sinal positivo
  - soma "deducao" com sinal negativo

Isso é uma ESTIMATIVA, não um número oficial. O CSV detalhado por ano fica
disponível para você conferir cada linha manualmente antes de confiar no
total.
"""

import csv
from pathlib import Path

from fmd_sof import salvar_csv  # reaproveita a função de escrita

OUTPUT_DIR = Path("dados_sof_fmd")
RAW_DIR = OUTPUT_DIR / "raw"
FILTERED_DIR = OUTPUT_DIR / "filtered"
CONSOLIDADO_DIR = OUTPUT_DIR / "consolidado"
CONSOLIDADO_DIR.mkdir(parents=True, exist_ok=True)


def ler_csv(caminho: Path) -> list:
    if not caminho.exists():
        return []
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def raiz_significativa(codigo: str) -> str:
    """
    Remove zeros à direita. Convenção observada nos dados reais: níveis mais
    profundos da árvore de receita acrescentam dígitos significativos à
    direita, mantendo o prefixo do nível anterior (ex: '1000...' -> '1100...'
    -> '1110...' -> '1111...'). Isso deixa a raiz significativa do pai como
    prefixo exato da raiz significativa do filho.
    """
    codigo = (codigo or "").rstrip("0")
    return codigo or "0"


def marcar_descendentes(linhas_receita: list) -> list:
    """Marca cada linha com tem_descendente_no_conjunto = True/False."""
    raizes = [(linha, raiz_significativa(linha.get("codReceita", "")))
              for linha in linhas_receita]
    for linha, raiz in raizes:
        linha["tem_descendente_no_conjunto"] = any(
            outra_raiz != raiz
            and outra_raiz.startswith(raiz)
            and len(outra_raiz) > len(raiz)
            for _, outra_raiz in raizes
        )
    return linhas_receita


def carregar_tipos_receita() -> dict:
    """codReceita -> tipo_no_receita (bruto_ou_outro / multas_e_juros / deducao / total_subtotal)."""
    registros = ler_csv(FILTERED_DIR / "fmd_receita_codigos.csv")
    return {r["codReceita"]: r.get("tipo_no_receita", "bruto_ou_outro") for r in registros}


def consolidar_receita_ano(ano: int, tipos_por_codigo: dict) -> list:
    registros = ler_csv(FILTERED_DIR / f"fmd_movimentosReceita_{ano}.csv")
    if not registros:
        return []

    # mesAteMovimento é ACUMULADO (confirmado no /movimentosReceita real) ->
    # o valor de dezembro já é o total do ano inteiro. Usar só dezembro evita
    # somar o mesmo código 12 vezes (uma por mês).
    por_codigo_dezembro = {}
    for r in registros:
        if str(r.get("mesAteMovimento_consulta")) == "12":
            por_codigo_dezembro[r.get("codMovimentoReceita")] = r

    linhas = []
    for cod, registro in por_codigo_dezembro.items():
        linha = dict(registro)
        linha["codReceita"] = cod
        linha["tipo_no_receita"] = tipos_por_codigo.get(cod, "bruto_ou_outro")
        linhas.append(linha)

    return marcar_descendentes(linhas)


def calcular_total_seguro(linhas_receita: list) -> float:
    total = 0.0
    for linha in linhas_receita:
        if linha.get("tem_descendente_no_conjunto"):
            continue  # pai com filho também batido -> pula, o filho já conta
        if linha.get("tipo_no_receita") == "total_subtotal":
            continue  # linha de subtotal explícito, redundante com as partes
        try:
            valor = float(linha.get("valRealizado", 0) or 0)
        except (TypeError, ValueError):
            valor = 0.0
        sinal = -1 if linha.get("tipo_no_receita") == "deducao" else 1
        total += sinal * valor
    return total


def carregar_despesa_resumo_ano(ano: int) -> dict:
    registros = ler_csv(RAW_DIR / f"despesas_fmd_{ano}.csv")
    if not registros:
        return {}
    # /despesas já vem agregado pela própria API (1 linha por consulta), sem
    # risco de dupla contagem hierárquica como na receita. Se houver mais de
    # uma linha salva (várias consultas mensais no mesmo arquivo), fica com a
    # última, que reflete o acumulado mais recente do ano.
    return registros[-1]


def descobrir_anos_disponiveis() -> list:
    anos = set()
    for p in RAW_DIR.glob("despesas_fmd_*.csv"):
        anos.add(int(p.stem.rsplit("_", 1)[-1]))
    for p in FILTERED_DIR.glob("fmd_movimentosReceita_*.csv"):
        anos.add(int(p.stem.rsplit("_", 1)[-1]))
    return sorted(anos)


def main():
    tipos_por_codigo = carregar_tipos_receita()
    anos = descobrir_anos_disponiveis()
    if not anos:
        print("Nenhum dado encontrado em dados_sof_fmd/raw ou dados_sof_fmd/filtered. "
              "Rode o coletor_sof_fmd.py primeiro.")
        return

    resumo = []
    for ano in anos:
        linhas_receita = consolidar_receita_ano(ano, tipos_por_codigo)
        if linhas_receita:
            salvar_csv(linhas_receita, CONSOLIDADO_DIR / f"receita_fmd_detalhada_{ano}.csv")
        total_receita = calcular_total_seguro(linhas_receita)

        despesa = carregar_despesa_resumo_ano(ano)

        resumo.append({
            "ano": ano,
            "qtd_codigos_receita_batidos": len(linhas_receita),
            "receita_fmd_estimada_liquida": round(total_receita, 2),
            "despesa_orcado_inicial": despesa.get("valOrcadoInicial", ""),
            "despesa_orcado_atualizado": despesa.get("valOrcadoAtualizado", ""),
            "despesa_empenhado_liquido": despesa.get("valEmpenhadoLiquido", ""),
            "despesa_liquidado": despesa.get("valLiquidado", ""),
            "despesa_pago_exercicio": despesa.get("valPagoExercicio", ""),
            "despesa_disponivel": despesa.get("valDisponivel", ""),
        })

    salvar_csv(resumo, CONSOLIDADO_DIR / "resumo_fmd_por_ano.csv")

    print("\nResumo (estimativa -- confira o CSV detalhado por ano antes de citar):")
    print(f"{'ano':<6}{'receita (líq. estim.)':>24}{'empenhado líq.':>20}{'pago no exercício':>20}")
    for r in resumo:
        print(f"{r['ano']:<6}{r['receita_fmd_estimada_liquida']:>24,.2f}"
              f"{(r['despesa_empenhado_liquido'] or 0):>20}"
              f"{(r['despesa_pago_exercicio'] or 0):>20}")


if __name__ == "__main__":
    main()