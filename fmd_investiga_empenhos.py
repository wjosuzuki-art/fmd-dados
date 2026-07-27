"""Descobre classificadores usados pelo FMD a partir dos empenhos de um exercício."""
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

BASE_URL = "https://gateway.apilib.prefeitura.sp.gov.br/sf/sof/v4"
TOKEN = "e20d722e-258d-3ace-b4ce-73b3c67b76d2"
ANO = 2018
COD_EMPRESA = "01"
COD_ORGAO = "07"
COD_UNIDADE = "10"
TIMEOUT = 90
PAUSA = 0.15
MAX_PAGINAS = 500
SAIDA = Path("saida_empenhos_fmd")

s = requests.Session()
s.headers.update({"Authorization": f"Bearer {TOKEN.strip()}", "Accept": "application/json"})


def get_json(endpoint: str, params: dict[str, Any]) -> Any:
    if TOKEN == "COLE_SEU_TOKEN_AQUI":
        raise RuntimeError("Cole o token em TOKEN.")
    r = s.get(f"{BASE_URL}/{endpoint}", params=params, timeout=TIMEOUT)
    if r.status_code == 401:
        raise RuntimeError("HTTP 401: token inválido ou expirado.")
    r.raise_for_status()
    return r.json()


def extrair_envelope(obj: Any, chave_lista: str):
    envelopes = obj if isinstance(obj, list) else [obj]
    registros, paginas = [], 1
    for env in envelopes:
        if not isinstance(env, dict):
            continue
        meta = env.get("metaDados") or env.get("retorno") or env.get("Retorno") or {}
        status = meta.get("txtStatus")
        erro = meta.get("txtMensagemErro")
        paginas = int(meta.get("qtdPaginas") or meta.get("numPagina") or paginas or 1)
        if status and status != "OK":
            print("Status interno:", status, "Mensagem:", erro)
        lista = env.get(chave_lista, [])
        if isinstance(lista, list):
            registros.extend(x for x in lista if isinstance(x, dict))
    return registros, paginas


def consultar_mes(mes: int):
    todos, brutos = [], []
    pagina = 1
    while pagina <= MAX_PAGINAS:
        params = {
            "anoEmpenho": ANO,
            "mesEmpenho": mes,
            "codEmpresa": COD_EMPRESA,
            "codOrgao": COD_ORGAO,
            "codUnidade": COD_UNIDADE,
            "numPagina": pagina,
        }
        bruto = get_json("empenhos", params)
        brutos.append(bruto)
        regs, qtd_paginas = extrair_envelope(bruto, "lstEmpenhos")
        print(f"  pagina {pagina}/{qtd_paginas}: {len(regs)} empenhos")
        todos.extend(regs)
        if pagina >= qtd_paginas or not regs:
            break
        pagina += 1
        time.sleep(PAUSA)
    return todos, brutos


def achar_coluna(df, nome):
    alvo = ''.join(c.lower() for c in nome if c.isalnum())
    for c in df.columns:
        if ''.join(x.lower() for x in c if x.isalnum()) == alvo:
            return c
    return None


def main():
    SAIDA.mkdir(exist_ok=True)
    todos, brutos_mes = [], {}
    for mes in range(1, 13):
        print(f"Mes {mes:02d}/{ANO}")
        regs, brutos = consultar_mes(mes)
        for r in regs:
            r["_mesConsulta"] = mes
        todos.extend(regs)
        brutos_mes[f"{mes:02d}"] = brutos

    (SAIDA / f"empenhos_fmd_{ANO}_bruto.json").write_text(
        json.dumps(brutos_mes, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    if not todos:
        print("Nenhum empenho encontrado. Examine o JSON bruto e o nome da lista retornada.")
        return

    df = pd.json_normalize(todos, sep=".")
    df.to_csv(SAIDA / f"empenhos_fmd_{ANO}_todos.csv", index=False, encoding="utf-8-sig")

    nomes = [
        "codFonteRecurso", "codReferencia", "codDestinacaoRecurso",
        "codVinculacaoRecurso", "codPrograma", "codProjetoAtividade",
        "codFuncao", "codSubFuncao", "codCategoria", "codGrupo",
        "codModalidade", "codElemento", "codItemDespesa", "codSubElemento",
        "codEmpenho", "numProcesso", "codContrato", "txtRazaoSocial", "numCpfCnpj"
    ]
    cols = [achar_coluna(df, n) for n in nomes]
    cols = [c for c in cols if c]
    print("Colunas classificadoras encontradas:", cols)
    if cols:
        df[cols].drop_duplicates().to_csv(
            SAIDA / f"classificadores_empenhos_fmd_{ANO}.csv", index=False, encoding="utf-8-sig"
        )

    chaves = [achar_coluna(df, n) for n in [
        "codFonteRecurso", "codReferencia", "codDestinacaoRecurso", "codVinculacaoRecurso"
    ]]
    chaves = [c for c in chaves if c]
    if chaves:
        df[chaves].drop_duplicates().to_csv(
            SAIDA / f"combinacoes_fonte_empenhos_fmd_{ANO}.csv", index=False, encoding="utf-8-sig"
        )
    print("Total bruto:", len(df))
    print("Arquivos em:", SAIDA)

if __name__ == "__main__":
    main()
