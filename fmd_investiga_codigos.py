"""
Descoberta dos códigos do FMD na API SOF v4.

Objetivo desta etapa:
- consultar apenas cadastros/classificadores;
- localizar registros relacionados ao FMD por palavras-chave;
- salvar respostas brutas e candidatos para inspeção;
- NÃO consultar ainda despesas, empenhos, liquidações ou pagamentos.

Uso:
1. pip install requests pandas
2. Cole o token em TOKEN.
3. Ajuste ANOS (comece com um único exercício encerrado).
4. Execute: python descobrir_codigos_fmd.py
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests

BASE_URL = "https://gateway.apilib.prefeitura.sp.gov.br/sf/sof/v4"
TOKEN = "0e824a75-52fa-31b2-8647-91aa19cdc973"
ANOS = [2025]
PAUSA_ENTRE_REQUISICOES = 0.15
TIMEOUT = 60
MAX_PAGINAS = 500
SAIDA = Path("saida_codigos_fmd")

# Acrescente/remova termos depois de olhar os primeiros resultados.
PALAVRAS_CHAVE = [
    "fmd",
    "fundo municipal de desenvolvimento social",
    "desenvolvimento social",
    "desestatiza",
    "outorga",
    "concess",
    "aliena",
]

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
})


def sem_acentos_aprox(texto: str) -> str:
    tabela = str.maketrans(
        "áàâãäéèêëíìîïóòôõöúùûüç",
        "aaaaaeeeeiiiiooooouuuuc",
    )
    return texto.lower().translate(tabela)


def contem_palavra(obj: Any, palavras: Iterable[str] = PALAVRAS_CHAVE) -> bool:
    texto = sem_acentos_aprox(json.dumps(obj, ensure_ascii=False, default=str))
    return any(sem_acentos_aprox(p) in texto for p in palavras)


def get_json(endpoint: str, params: dict[str, Any]) -> Any:
    if TOKEN == "COLE_SEU_TOKEN_AQUI":
        raise RuntimeError("Cole seu token na variável TOKEN antes de executar.")

    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    resposta = session.get(url, params=params, timeout=TIMEOUT)

    if resposta.status_code == 401:
        raise RuntimeError("HTTP 401: token ausente, inválido ou expirado.")
    if resposta.status_code == 403:
        raise RuntimeError(
            "HTTP 403: o token não tem acesso à API ou o gateway exige credencial adicional."
        )

    resposta.raise_for_status()
    try:
        return resposta.json()
    except ValueError as exc:
        trecho = resposta.text[:500]
        raise RuntimeError(f"Resposta não JSON em {url}: {trecho}") from exc


def achar_listas(obj: Any) -> list[list[Any]]:
    """Localiza listas em qualquer nível da resposta, tolerando envelopes variados."""
    listas: list[list[Any]] = []
    if isinstance(obj, list):
        listas.append(obj)
    elif isinstance(obj, dict):
        for valor in obj.values():
            listas.extend(achar_listas(valor))
    return listas


def extrair_registros(obj: Any) -> list[dict[str, Any]]:
    """Escolhe a maior lista de objetos como provável conjunto de registros."""
    candidatas = []
    for lista in achar_listas(obj):
        registros = [x for x in lista if isinstance(x, dict)]
        if registros:
            candidatas.append(registros)
    return max(candidatas, key=len, default=[])


def assinatura(registros: list[dict[str, Any]]) -> str:
    return json.dumps(registros, ensure_ascii=False, sort_keys=True, default=str)


def paginar(endpoint: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Any]]:
    """Pagina defensivamente até resposta vazia ou página repetida."""
    todos: list[dict[str, Any]] = []
    brutos: list[Any] = []
    anterior = None

    for pagina in range(1, MAX_PAGINAS + 1):
        consulta = {**params, "numPagina": pagina}
        bruto = get_json(endpoint, consulta)
        brutos.append(bruto)
        registros = extrair_registros(bruto)

        if not registros:
            # Algumas rotas sem paginação podem retornar um único objeto.
            if pagina == 1 and isinstance(bruto, dict):
                todos.append(bruto)
            break

        atual = assinatura(registros)
        if atual == anterior:
            print(f"  Aviso: página repetida em {endpoint}; paginação encerrada.")
            break

        todos.extend(registros)
        anterior = atual
        print(f"  {endpoint}: página {pagina}, {len(registros)} registros")
        time.sleep(PAUSA_ENTRE_REQUISICOES)

    return todos, brutos


def salvar_json(caminho: Path, obj: Any) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def salvar_csv(caminho: Path, registros: list[dict[str, Any]]) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    if registros:
        pd.json_normalize(registros, sep=".").to_csv(caminho, index=False, encoding="utf-8-sig")


def candidatos(registros: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in registros if contem_palavra(r)]


def valor_por_chaves(registro: dict[str, Any], padroes: list[str]) -> Any:
    """Procura uma chave mesmo quando o nome exato varia no retorno."""
    for chave, valor in registro.items():
        chave_norm = re.sub(r"[^a-z0-9]", "", sem_acentos_aprox(str(chave)))
        if any(p in chave_norm for p in padroes):
            return valor
    return None


def main() -> None:
    SAIDA.mkdir(exist_ok=True)
    resumo: list[dict[str, Any]] = []

    for ano in ANOS:
        print(f"\n=== Exercício {ano} ===")
        pasta = SAIDA / str(ano)
        pasta.mkdir(parents=True, exist_ok=True)

        catalogos = {
            "empresas": {"anoExercicio": ano},
            "orgaos": {"anoExercicio": ano},
            "fonteRecursos": {"anoExercicio": ano},
            "programas": {"anoExercicio": ano},
            "projetosAtividades": {"anoExercicio": ano},
            
        }

        dados: dict[str, list[dict[str, Any]]] = {}

        for endpoint, params in catalogos.items():
            print(f"Consultando {endpoint}...")
            regs, brutos = paginar(endpoint, params)
            dados[endpoint] = regs
            cand = candidatos(regs)
            salvar_json(pasta / f"{endpoint}_bruto.json", brutos)
            salvar_csv(pasta / f"{endpoint}_todos.csv", regs)
            salvar_csv(pasta / f"{endpoint}_candidatos.csv", cand)
            resumo.append({
                "ano": ano,
                "endpoint": endpoint,
                "total": len(regs),
                "candidatos": len(cand),
            })
            print(f"  total={len(regs)}; candidatos={len(cand)}")

        # Unidades exigem codOrgao. Consultamos todos os órgãos para não perder o FMD
        # caso o nome do órgão-pai seja apenas 'Secretaria Municipal da Fazenda'.
        print("Consultando unidades de todos os órgãos...")
        unidades: list[dict[str, Any]] = []
        unidades_brutas: dict[str, Any] = {}

        for orgao in dados.get("orgaos", []):
            cod_orgao = valor_por_chaves(orgao, ["codorgao"])
            cod_empresa = valor_por_chaves(orgao, ["codempresa"])
            if cod_orgao is None:
                continue

            params = {"anoExercicio": ano, "codOrgao": cod_orgao}
            if cod_empresa is not None:
                params["codEmpresa"] = cod_empresa

            try:
                regs, brutos = paginar("unidades", params)
            except requests.HTTPError as exc:
                print(f"  Falha em unidades do órgão {cod_orgao}: {exc}")
                continue

            for r in regs:
                r.setdefault("_codOrgao_consultado", cod_orgao)
                if cod_empresa is not None:
                    r.setdefault("_codEmpresa_consultada", cod_empresa)
            unidades.extend(regs)
            unidades_brutas[str(cod_orgao)] = brutos
            time.sleep(PAUSA_ENTRE_REQUISICOES)

        cand_unidades = candidatos(unidades)
        salvar_json(pasta / "unidades_bruto_por_orgao.json", unidades_brutas)
        salvar_csv(pasta / "unidades_todas.csv", unidades)
        salvar_csv(pasta / "unidades_candidatos.csv", cand_unidades)
        resumo.append({
            "ano": ano,
            "endpoint": "unidades",
            "total": len(unidades),
            "candidatos": len(cand_unidades),
        })
        print(f"  unidades: total={len(unidades)}; candidatos={len(cand_unidades)}")

        # Consolida somente os achados, preservando a origem.
        consolidados: list[dict[str, Any]] = []
        for endpoint, regs in {**dados, "unidades": unidades}.items():
            for r in candidatos(regs):
                consolidados.append({"endpoint": endpoint, "ano": ano, **r})
        salvar_csv(pasta / "candidatos_fmd_consolidados.csv", consolidados)

    pd.DataFrame(resumo).to_csv(
        SAIDA / "resumo_consultas.csv", index=False, encoding="utf-8-sig"
    )
    print("\nConcluído.")
    print(f"Abra primeiro: {SAIDA / 'resumo_consultas.csv'}")
    print("Depois examine, por ano: candidatos_fmd_consolidados.csv")
    print("Se algo parecer ausente, procure nos arquivos *_todos.csv e *_bruto.json.")


if __name__ == "__main__":
    main()
