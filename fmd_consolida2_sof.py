#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Resumo por categorias -- quebra a despesa e a receita do FMD em várias
dimensões, ano a ano, pra responder perguntas tipo "em que área o FMD mais
gasta", "qual elemento de despesa domina", "quem são os maiores credores",
"de onde vem a receita, por tipo".

PRÉ-REQUISITOS (rodar nessa ordem):
  1. coletor_sof_fmd.py    -> gera raw/empenhos_fmd_{ano}.csv
  2. consolidar_fmd.py     -> gera consolidado/receita_fmd_detalhada_{ano}.csv
  3. este script

SOBRE "SECRETARIA": o FMD já É o órgão orçamentário (codOrgao=07) -- não
existe "quebra por secretaria" dentro dele, porque ele não é um departamento
de uma secretaria maior, é o órgão em si. A dimensão mais parecida com isso
que a API oferece é "função de governo" (txDescricaoFuncao) -- área de
atuação (Saúde, Assistência Social, Urbanismo, Habitação etc.) -- é o que
esse script usa como proxy.

SOBRE DUPLICAÇÃO NOS EMPENHOS: /empenhos é cumulativo (mesEmpenho = "até o
mês informado"), mas o coletor (antes da correção) salvava até 12 consultas
mensais concatenadas no mesmo arquivo -- um empenho de janeiro podia aparecer
até 12 vezes. Este script deduplica por (codEmpenho, anoEmpenho, codEmpresa),
mantendo a versão com o maior valPagoExercicio (o estado mais avançado
daquele empenho no ano) -- funciona tanto em arquivos antigos (duplicados)
quanto em arquivos novos (já sem duplicata, então o dedup não muda nada).
"""

import csv
import re
from collections import defaultdict
from pathlib import Path

from fmd_sof2 import salvar_csv, normalizar_texto, RAW_DIR
from fmd_consolida_sof import ler_csv, CONSOLIDADO_DIR

CATEGORIAS_DIR = Path("dados_sof_fmd") / "categorias"
CATEGORIAS_DIR.mkdir(parents=True, exist_ok=True)

CAMPOS_VALOR = ["valTotalEmpenhado", "valEmpenhadoLiquido", "valLiquidado", "valPagoExercicio"]


# =====================================================================================
# DESPESA (a partir de empenhos_fmd_{ano}.csv)
# =====================================================================================

def _float_seguro(valor) -> float:
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def deduplicar_empenhos(registros: list) -> list:
    """
    Mantém 1 linha por (codEmpenho, anoEmpenho, codEmpresa) -- a de maior
    valPagoExercicio (empate: maior valLiquidado, depois valEmpenhadoLiquido).
    """
    melhores = {}

    def pontuacao(reg):
        return (_float_seguro(reg.get("valPagoExercicio")),
                _float_seguro(reg.get("valLiquidado")),
                _float_seguro(reg.get("valEmpenhadoLiquido")))

    for r in registros:
        chave = (r.get("codEmpenho"), r.get("anoEmpenho"), r.get("codEmpresa"))
        atual = melhores.get(chave)
        if atual is None or pontuacao(r) > pontuacao(atual):
            melhores[chave] = r
    return list(melhores.values())


def descobrir_anos_empenhos() -> list:
    return sorted(int(p.stem.rsplit("_", 1)[-1]) for p in RAW_DIR.glob("empenhos_fmd_*.csv"))


def carregar_empenhos_deduplicados_por_ano() -> dict:
    por_ano = {}
    for ano in descobrir_anos_empenhos():
        brutos = ler_csv(RAW_DIR / f"empenhos_fmd_{ano}.csv")
        dedup = deduplicar_empenhos(brutos)
        print(f"  [empenhos {ano}] {len(brutos)} linhas brutas -> "
              f"{len(dedup)} empenhos únicos após dedup")
        por_ano[ano] = dedup
    return por_ano


def agregar_despesa_por_campo(empenhos_por_ano: dict, campo: str) -> list:
    """Soma os valores de despesa agrupando por um campo (função, elemento etc), por ano."""
    acumulado = defaultdict(lambda: defaultdict(float))
    contagem = defaultdict(int)
    for ano, registros in empenhos_por_ano.items():
        for r in registros:
            chave = (ano, r.get(campo) or "(vazio)")
            for cv in CAMPOS_VALOR:
                acumulado[chave][cv] += _float_seguro(r.get(cv))
            contagem[chave] += 1

    linhas = []
    for (ano, valor_campo), valores in acumulado.items():
        linha = {"ano": ano, campo: valor_campo, "qtd_empenhos": contagem[(ano, valor_campo)]}
        linha.update({k: round(v, 2) for k, v in valores.items()})
        linhas.append(linha)
    linhas.sort(key=lambda l: (l["ano"], -l["valEmpenhadoLiquido"]))
    return linhas


def agregar_despesa_por_credor(empenhos_por_ano: dict) -> list:
    acumulado = defaultdict(lambda: defaultdict(float))
    contagem = defaultdict(int)
    for ano, registros in empenhos_por_ano.items():
        for r in registros:
            chave = (ano, r.get("numCpfCnpj") or "(vazio)", r.get("txtRazaoSocial") or "(vazio)")
            for cv in CAMPOS_VALOR:
                acumulado[chave][cv] += _float_seguro(r.get(cv))
            contagem[chave] += 1

    linhas = []
    for (ano, cnpj, razao_social), valores in acumulado.items():
        linha = {
            "ano": ano, "numCpfCnpj": cnpj, "txtRazaoSocial": razao_social,
            "qtd_empenhos": contagem[(ano, cnpj, razao_social)],
        }
        linha.update({k: round(v, 2) for k, v in valores.items()})
        linhas.append(linha)
    linhas.sort(key=lambda l: (l["ano"], -l["valEmpenhadoLiquido"]))
    return linhas


# =====================================================================================
# RECEITA (a partir de consolidado/receita_fmd_detalhada_{ano}.csv, já produzido
# pelo consolidar_fmd.py -- reaproveita a lógica de líquido/pai-filho de lá)
# =====================================================================================

_SUFIXOS_RECEITA = [
    r"-\s*fmd\s*$",
    r"-\s*multas?\s*(e\s*juros)?\s*$",
    r"-\s*juros?\s*$",
]


def limpar_categoria_receita(descricao: str) -> str:
    """Remove sufixos tipo '- FMD', '- MULTA', '- MULTAS E JUROS' do fim (podem
    se acumular, ex: '... - FMD - MULTA', então removemos em laço)."""
    d = (descricao or "").strip()
    mudou = True
    while mudou:
        mudou = False
        for padrao in _SUFIXOS_RECEITA:
            novo = re.sub(padrao, "", d, flags=re.IGNORECASE).strip()
            if novo != d:
                d = novo
                mudou = True
    return d.strip(" -") or "(sem descrição)"


def descobrir_anos_receita_detalhada() -> list:
    return sorted(int(p.stem.rsplit("_", 1)[-1])
                  for p in CONSOLIDADO_DIR.glob("receita_fmd_detalhada_*.csv"))


def agregar_receita_por_categoria() -> list:
    acumulado = defaultdict(float)
    rotulo_exibicao = {}

    for ano in descobrir_anos_receita_detalhada():
        linhas = ler_csv(CONSOLIDADO_DIR / f"receita_fmd_detalhada_{ano}.csv")
        for l in linhas:
            # mesma lógica do consolidar_fmd.calcular_total_seguro: só folha,
            # e ignora subtotal explícito
            if str(l.get("tem_descendente_no_conjunto")).strip().lower() == "true":
                continue
            if l.get("tipo_no_receita") == "total_subtotal":
                continue
            categoria = limpar_categoria_receita(l.get("txtDescricaoMovimentoReceita", ""))
            chave_normalizada = normalizar_texto(categoria)
            rotulo_exibicao.setdefault(chave_normalizada, categoria)
            sinal = -1 if l.get("tipo_no_receita") == "deducao" else 1
            acumulado[(ano, chave_normalizada)] += sinal * _float_seguro(l.get("valRealizado"))

    linhas_saida = []
    for (ano, chave_normalizada), valor in acumulado.items():
        linhas_saida.append({
            "ano": ano,
            "categoria_receita": rotulo_exibicao[chave_normalizada],
            "valRealizado": round(valor, 2),
        })
    linhas_saida.sort(key=lambda l: (l["ano"], -l["valRealizado"]))
    return linhas_saida


# =====================================================================================
# ORQUESTRAÇÃO
# =====================================================================================

def main():
    print("Carregando e deduplicando empenhos...")
    empenhos_por_ano = carregar_empenhos_deduplicados_por_ano()
    if not empenhos_por_ano:
        print("  [aviso] nenhum arquivo raw/empenhos_fmd_*.csv encontrado.")

    dimensoes_despesa = [
        ("txDescricaoFuncao", "despesa_por_funcao.csv"),
        ("txDescricaoSubFuncao", "despesa_por_subfuncao.csv"),
        ("txDescricaoCategoriaEconomica", "despesa_por_categoria_economica.csv"),
        ("txDescricaoGrupoDespesa", "despesa_por_grupo_despesa.csv"),
        ("txDescricaoElemento", "despesa_por_elemento_despesa.csv"),
        ("txDescricaoFonteRecurso", "despesa_por_fonte_recurso.csv"),
        ("txDescricaoPrograma", "despesa_por_programa.csv"),
    ]
    for campo, nome_arquivo in dimensoes_despesa:
        linhas = agregar_despesa_por_campo(empenhos_por_ano, campo)
        salvar_csv(linhas, CATEGORIAS_DIR / nome_arquivo)

    linhas_credor = agregar_despesa_por_credor(empenhos_por_ano)
    salvar_csv(linhas_credor, CATEGORIAS_DIR / "despesa_por_credor.csv")

    print("\nAgregando receita por categoria (a partir do consolidado)...")
    linhas_receita = agregar_receita_por_categoria()
    salvar_csv(linhas_receita, CATEGORIAS_DIR / "receita_por_categoria.csv")

    print(f"\nResultados em: {CATEGORIAS_DIR.resolve()}")


if __name__ == "__main__":
    main()