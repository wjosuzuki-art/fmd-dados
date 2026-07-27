"""
Etapa 2 — descobrir classificadores efetivamente usados pelo FMD no SOF.

Escopo deliberadamente pequeno:
- consulta /despesas para a unidade do FMD nos 12 meses do exercício;
- extrai combinações distintas de fonte/referência/destinação/vinculação;
- consulta /fonteRecursos somente para as combinações encontradas;
- não consulta contasReceita, empenhos, credores ou pagamentos.

Instalação:
    pip install requests pandas

Execução:
    python investigar_classificadores_fmd.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

BASE_URL = "https://gateway.apilib.prefeitura.sp.gov.br/sf/sof/v4"
TOKEN = "e20d722e-258d-3ace-b4ce-73b3c67b76d2"

ANO = 2025
MESES = range(1, 13)
COD_EMPRESA = "01"
COD_ORGAO = "07"
COD_UNIDADE = "10"

TIMEOUT = 60
PAUSA = 0.15
MAX_PAGINAS = 500
SAIDA = Path("saida_classificadores_fmd")

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
})


def get_json(endpoint: str, params: dict[str, Any]) -> Any:
    if TOKEN == "COLE_SEU_TOKEN_AQUI":
        raise RuntimeError("Cole seu token na variável TOKEN.")
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    r = session.get(url, params=params, timeout=TIMEOUT)
    if r.status_code == 401:
        raise RuntimeError("HTTP 401: token inválido ou expirado.")
    if r.status_code == 403:
        raise RuntimeError("HTTP 403: acesso negado pelo gateway.")
    r.raise_for_status()
    try:
        return r.json()
    except ValueError as exc:
        raise RuntimeError(f"Resposta não JSON: {r.text[:500]}") from exc


def achar_listas(obj: Any) -> list[list[Any]]:
    listas = []
    if isinstance(obj, list):
        listas.append(obj)
    elif isinstance(obj, dict):
        for valor in obj.values():
            listas.extend(achar_listas(valor))
    return listas


def extrair_registros_despesas(obj):
    """
    Extrai somente a lista real de despesas, ignorando o envelope da API.
    Também exibe mensagens internas retornadas pelo SOF.
    """

    if isinstance(obj, list):
        for item in obj:
            registros = extrair_registros_despesas(item)
            if registros is not None:
                return registros

    if isinstance(obj, dict):
        # Mostra o status interno, quando existir.
        retorno = obj.get("retorno") or obj.get("Retorno")

        if isinstance(retorno, dict):
            status = (
                retorno.get("txtStatus")
                or retorno.get("status")
            )
            mensagem = (
                retorno.get("txtMensagemErro")
                or retorno.get("mensagem")
            )

            if status or mensagem:
                print(
                    f"Status interno: {status!r}; "
                    f"mensagem: {mensagem!r}"
                )

        # Nomes possíveis da lista de despesas.
        for chave in (
            "lstDespesas",
            "listaDespesas",
            "despesas",
            "LstDespesas",
        ):
            valor = obj.get(chave)

            if isinstance(valor, list):
                return [
                    registro
                    for registro in valor
                    if isinstance(registro, dict)
                ]

        # Procura recursivamente em outros objetos.
        for valor in obj.values():
            if isinstance(valor, (dict, list)):
                registros = extrair_registros_despesas(valor)

                if registros is not None:
                    return registros

    return None

def consultar_despesas(params):
    bruto = get_json("despesas", params)

    registros = extrair_registros_despesas(bruto)

    if registros is None:
        print("Estrutura desconhecida na resposta de /despesas.")
        print(json.dumps(bruto, ensure_ascii=False, indent=2)[:5000])
        return [], bruto

    return registros, bruto


def paginar(endpoint: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Any]]:
    todos, brutos = [], []
    assinatura_anterior = None
    for pagina in range(1, MAX_PAGINAS + 1):
        p = dict(params)
        p["numPagina"] = pagina
        bruto = get_json(endpoint, p)
        brutos.append(bruto)
        regs = extrair_registros_despesas(bruto)

        if regs is None:
            print("Não encontrei a lista real de despesas na resposta.")
            print(json.dumps(bruto, ensure_ascii=False, indent=2)[:3000])
            break

        if not regs:
            print("A API respondeu corretamente, mas a lista de despesas está vazia.")
            break
      
        assinatura = json.dumps(regs, ensure_ascii=False, sort_keys=True, default=str)
        if assinatura == assinatura_anterior:
            print(f"Aviso: página repetida em /{endpoint}; encerrando.")
            break
        todos.extend(regs)
        assinatura_anterior = assinatura
        print(f"/{endpoint}: página {pagina}, {len(regs)} registros")
        time.sleep(PAUSA)
    return todos, brutos


def norm_coluna(nome: str) -> str:
    return "".join(c.lower() for c in nome if c.isalnum())


def localizar_coluna(df: pd.DataFrame, nomes: list[str]) -> str | None:
    mapa = {norm_coluna(c): c for c in df.columns}
    for nome in nomes:
        alvo = norm_coluna(nome)
        if alvo in mapa:
            return mapa[alvo]
    return None


def salvar_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def main() -> None:
    SAIDA.mkdir(exist_ok=True)

    registros = []
    brutos_por_mes = {}

    print(f"Consultando as dotações do FMD em todos os meses de {ANO}...")
    for mes in MESES:
        params = {
            "anoDotacao": str(ANO),
            "mesDotacao": str(mes),
            "codEmpresa": COD_EMPRESA,
            "codOrgao": COD_ORGAO,
            "codUnidade": COD_UNIDADE,
        }
        print(f"\nMês {mes:02d}/{ANO}")
        regs_mes, bruto_mes = consultar_despesas(params)
        brutos_mes = [bruto_mes]
        brutos_por_mes[f"{mes:02d}"] = brutos_mes

        for registro in regs_mes:
            registro["_mesConsulta"] = mes
        registros.extend(regs_mes)
        print(f"Total retornado no mês {mes:02d}: {len(regs_mes)}")

    salvar_json(SAIDA / f"despesas_fmd_{ANO}_bruto_por_mes.json", brutos_por_mes)

    if not registros:
        print(f"Nenhum registro retornado para o exercício de {ANO}.")
        return

    df = pd.json_normalize(registros, sep=".")
    df.to_csv(
        SAIDA / f"despesas_fmd_{ANO}_todos_os_meses.csv",
        index=False,
        encoding="utf-8-sig",
    )

    # Mantém todos os retornos mensais e cria também uma visão sem duplicatas exatas.
    # A coluna _mesConsulta não entra na comparação, pois o mesmo registro pode reaparecer
    # em vários meses caso o endpoint devolva posição acumulada.
    colunas_negocio = [c for c in df.columns if c != "_mesConsulta"]
    df_sem_duplicatas = df.drop_duplicates(subset=colunas_negocio)
    df_sem_duplicatas.to_csv(
        SAIDA / f"despesas_fmd_{ANO}_sem_duplicatas_exatas.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(f"\nRegistros somados dos 12 meses: {len(df)}")
    print(f"Registros sem duplicatas exatas entre meses: {len(df_sem_duplicatas)}")

    aliases = {
        "codFonteRecurso": ["codFonteRecurso"],
        "codReferencia": ["codReferencia"],
        "codDestinacaoRecurso": ["codDestinacaoRecurso"],
        "codVinculacaoRecurso": ["codVinculacaoRecurso"],
        "codPrograma": ["codPrograma"],
        "codProjetoAtividade": ["codProjetoAtividade"],
        "codFuncao": ["codFuncao"],
        "codSubFuncao": ["codSubFuncao"],
        "codCategoria": ["codCategoria"],
        "codGrupo": ["codGrupo"],
        "codModalidade": ["codModalidade"],
        "codElemento": ["codElemento"],
    }
    encontradas = {rotulo: localizar_coluna(df, nomes) for rotulo, nomes in aliases.items()}
    print("\nColunas localizadas:")
    for rotulo, coluna in encontradas.items():
        print(f"  {rotulo}: {coluna}")

    colunas_chave = [
        encontradas[x] for x in (
            "codFonteRecurso", "codReferencia",
            "codDestinacaoRecurso", "codVinculacaoRecurso"
        ) if encontradas[x] is not None
    ]
    if not colunas_chave:
        print("Não localizei os classificadores esperados. Examine o CSV e o JSON bruto.")
        return

    chaves = df_sem_duplicatas[colunas_chave].drop_duplicates().copy()
    chaves.to_csv(SAIDA / f"combinacoes_fonte_fmd_{ANO}.csv", index=False, encoding="utf-8-sig")
    print(f"\nCombinações distintas encontradas: {len(chaves)}")

    colunas_perfil = [c for c in encontradas.values() if c is not None]
    perfil = df_sem_duplicatas[colunas_perfil].drop_duplicates()
    perfil.to_csv(SAIDA / f"perfil_orcamentario_fmd_{ANO}.csv", index=False, encoding="utf-8-sig")

    # Consulta o cadastro de fontes apenas para as combinações efetivamente observadas.
    detalhes = []
    detalhes_brutos = []
    for _, linha in chaves.iterrows():
        p: dict[str, Any] = {"anoExercicio": ANO}
        for rotulo in ("codFonteRecurso", "codReferencia", "codDestinacaoRecurso", "codVinculacaoRecurso"):
            coluna = encontradas.get(rotulo)
            if coluna and pd.notna(linha.get(coluna)):
                valor = linha[coluna]
                if isinstance(valor, float) and valor.is_integer():
                    valor = int(valor)
                p[rotulo] = str(valor)
        print("Consultando /fonteRecursos com", p)
        bruto = get_json("fonteRecursos", p)
        detalhes_brutos.append({"parametros": p, "resposta": bruto})
        detalhes.extend(extrair_registros(bruto))
        time.sleep(PAUSA)

    salvar_json(SAIDA / f"fontes_observadas_fmd_{ANO}_bruto.json", detalhes_brutos)
    if detalhes:
        pd.json_normalize(detalhes, sep=".").drop_duplicates().to_csv(
            SAIDA / f"fontes_observadas_fmd_{ANO}.csv",
            index=False,
            encoding="utf-8-sig",
        )

    print("\nConcluído. Examine primeiro:")
    print(SAIDA / f"combinacoes_fonte_fmd_{ANO}.csv")
    print(SAIDA / f"fontes_observadas_fmd_{ANO}.csv")
    print(SAIDA / f"perfil_orcamentario_fmd_{ANO}.csv")


if __name__ == "__main__":
    main()
