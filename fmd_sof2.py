#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coletor de dados do SOF (Sistema de Execução Orçamentária) da Prefeitura de São Paulo
para o FMD - Fundo Municipal de Desenvolvimento Social.

RESUMO DA ESTRATÉGIA (ver conversa que originou este script para o raciocínio completo):

  RECEITA (não tem filtro por órgão/unidade na API -> filtramos por TEXTO)
    - /contasReceita   : árvore completa de classificação de receita (por ano).
                         Guardamos tudo em raw/ e filtramos localmente por texto
                         ("FMD" e "fundo municipal de desenvolvimento social")
                         -> filtered/fmd_receita.csv
    - /movimentosReceita : valores (orçado/realizado) por código de receita.
                         Em vez de baixar a árvore inteira mês a mês (caríssimo:
                         ~87 páginas x 12 meses x 8 anos), consultamos direto pelos
                         codReceita já identificados como FMD em /contasReceita.

  DESPESA (tem filtro nativo por codOrgao=07 / codUnidade=10 / codEmpresa=01)
    - /despesas        : dotação orçamentária (resumo, 1 chamada por ano/mês)
    - /empenhos        : núcleo da base - já vem autodescrito (função, programa,
                         elemento, fonte, credor, anexos com objeto da compra)
    - /despesasCredor  : ranking de credores (mesEmpenho AQUI é o mês exato, não
                         acumulado - diferente de /empenhos!)
    - /contratos       : contexto contratual
    - /credoresDeContrato : drill-down por contrato (só quando há mais de 1 credor)
    - /fonteRecursos   : dicionário para decodificar codFonteRecurso encontrado
                         nos empenhos do FMD

  OPCIONAIS / SOB DEMANDA (desligados por padrão - custo alto ou baixo retorno):
    - /liquidacoes      : drill-down por empenho (1 chamada por empenho -> caro
                         em volume; /empenhos já traz valLiquidado agregado)
    - /CompromissosPagar: não filtra por órgão (9126 páginas/ano na cidade toda);
                         só compensa se você quiser rastrear Nota de Liquidação e
                         Pagamento (NLP) individual. Ativar via RODAR_COMPROMISSOS_PAGAR.

Tudo é salvo em CSV (utf-8-sig) com checkpoint por requisição em disco, então o
script pode ser interrompido e retomado sem perder trabalho já feito.

COMO USAR:
  1. Preencha API_TOKEN abaixo.
  2. Rode primeiro com MODO_TESTE = True para validar token/paginação com poucas
     chamadas antes de soltar a coleta completa de 2019-2026.
  3. Depois rode com MODO_TESTE = False.
"""

import csv
import json
import re
import time
import unicodedata
from pathlib import Path
from datetime import datetime

import requests

# =====================================================================================
# CONFIGURAÇÃO
# =====================================================================================

API_TOKEN = "e20d722e-258d-3ace-b4ce-73b3c67b76d2"

BASE_URL = "https://gateway.apilib.prefeitura.sp.gov.br/sf/sof/v4"

# Esquema de autenticação do swagger é OAuth2 (gateway estilo WSO2 APIM), que
# tipicamente espera o token como Bearer. Se o seu token for de outro tipo
# (ex: header "apikey"), ajuste a função `montar_headers` abaixo.
def montar_headers():
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Accept": "application/json",
    }

ANO_INICIO = 2019
ANO_FIM = 2026  # inclusive
ANOS = list(range(ANO_INICIO, ANO_FIM + 1))
MESES = list(range(1, 13))

COD_EMPRESA_PREFEITURA = "01"
COD_ORGAO_FMD = "07"
COD_UNIDADE_FMD = "10"

# Camadas opcionais (caras em número de chamadas) - deixe False até decidir que
# realmente precisa desse nível de detalhe.
RODAR_LIQUIDACOES = True    
RODAR_COMPROMISSOS_PAGAR = True

# As duas frentes (receita e despesa) são independentes -- despesa não precisa
# de nada que saia da receita. Se a receita estiver lenta (movimentosReceita é
# código x mês, caro), rode um processo só com RODAR_FRENTE_RECEITA=False e
# RODAR_FRENTE_DESPESA=True em paralelo, num segundo terminal, enquanto o
# primeiro processo (com a config original) continua a receita. Os dois
# processos escrevem em arquivos diferentes dentro de dados_sof_fmd/, então
# rodar simultaneamente é seguro.
RODAR_FRENTE_RECEITA = True
RODAR_FRENTE_DESPESA = True

# Modo teste: restringe o universo de coleta para validar rapidamente o token,
# a paginação e o parsing antes de rodar os 8 anos completos.
MODO_TESTE = False
if MODO_TESTE:
    ANOS = [2024]
    MESES = [12]

# Diretórios de saída
OUTPUT_DIR = Path("dados_sof_fmd")
RAW_DIR = OUTPUT_DIR / "raw"
CHECKPOINT_DIR = OUTPUT_DIR / "_checkpoints"
FILTERED_DIR = OUTPUT_DIR / "filtered"
REFERENCE_DIR = OUTPUT_DIR / "reference"
DETALHE_DIR = OUTPUT_DIR / "detalhe"

for d in (RAW_DIR, CHECKPOINT_DIR, FILTERED_DIR, REFERENCE_DIR, DETALHE_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Rede
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_DELAY_SECONDS = 0.25  # pausa entre chamadas, gentileza com a API
MAX_RETRIES = 5
RETRY_BACKOFF_BASE_SECONDS = 2

# Regras de filtro textual para receita do FMD (ver discussão: evitamos usar
# "fundo" E "municipal" E "desenvolvimento" soltos porque isso pegaria fundos
# irmãos como "Fundo Municipal de Desenvolvimento Urbano/Rural/Cultura" etc.)
FILTRO_RECEITA_TAG = "fmd"
FILTRO_RECEITA_FRASE = "fundo municipal de desenvolvimento social"


# =====================================================================================
# UTILITÁRIOS
# =====================================================================================

def normalizar_texto(txt):
    """minúsculo, sem acento, espaços colapsados - para comparação robusta de texto."""
    if txt is None:
        return ""
    txt = str(txt).lower()
    txt = unicodedata.normalize("NFKD", txt)
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return " ".join(txt.split())


def achatar_registro(registro):
    """
    Prepara um registro (dict vindo da API) para virar linha de CSV.
    Campos que são lista/dict (ex: 'anexos', 'retencoes') viram JSON em texto,
    para não perder informação nem quebrar o CSV.
    """
    achatado = {}
    for chave, valor in registro.items():
        if isinstance(valor, (list, dict)):
            achatado[chave] = json.dumps(valor, ensure_ascii=False)
        else:
            achatado[chave] = valor
    return achatado


def salvar_csv(registros, caminho: Path):
    """Salva lista de dicts em CSV, com união de todas as chaves como cabeçalho."""
    if not registros:
        print(f"  [aviso] nenhum registro para salvar em {caminho.name}, pulando.")
        return
    achatados = [achatar_registro(r) for r in registros]
    fieldnames = []
    vistos = set()
    for reg in achatados:
        for k in reg.keys():
            if k not in vistos:
                vistos.add(k)
                fieldnames.append(k)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(achatados)
    print(f"  [ok] {len(achatados)} linhas -> {caminho}")


def _checkpoint_path(endpoint: str, params: dict, pagina: int) -> Path:
    nome_endpoint = endpoint.strip("/").replace("/", "_")
    partes_params = "_".join(f"{k}-{v}" for k, v in sorted(params.items()))
    nome = f"{nome_endpoint}__{partes_params}__pag{pagina}.json"
    # nomes de arquivo não podem ficar gigantes; se ficar grande, é sinal de
    # que os params têm muita coisa - ainda assim mantemos como está pois na
    # prática os params usados aqui são curtos.
    return CHECKPOINT_DIR / nome


def _requisitar_pagina(endpoint: str, params: dict, pagina: int) -> dict:
    """Faz 1 requisição GET com retry/backoff, usando checkpoint em disco."""
    caminho_cp = _checkpoint_path(endpoint, params, pagina)
    if caminho_cp.exists():
        with open(caminho_cp, "r", encoding="utf-8") as f:
            return json.load(f)

    params_completos = dict(params)
    params_completos["numPagina"] = pagina
    url = f"{BASE_URL}{endpoint}"

    ultimo_erro = None
    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            resposta = requests.get(
                url,
                headers=montar_headers(),
                params=params_completos,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if resposta.status_code == 401 or resposta.status_code == 403:
                raise RuntimeError(
                    f"Erro de autenticação ({resposta.status_code}) em {url}. "
                    f"Confira o API_TOKEN. Corpo: {resposta.text[:300]}"
                )
            if resposta.status_code == 429 or resposta.status_code >= 500:
                raise requests.exceptions.RequestException(
                    f"status {resposta.status_code}: {resposta.text[:200]}"
                )
            resposta.raise_for_status()
            dados = resposta.json()
            with open(caminho_cp, "w", encoding="utf-8") as f:
                json.dump(dados, f, ensure_ascii=False)
            time.sleep(REQUEST_DELAY_SECONDS)
            return dados
        except RuntimeError:
            raise  # erro de auth não adianta tentar de novo
        except requests.exceptions.RequestException as e:
            ultimo_erro = e
            espera = RETRY_BACKOFF_BASE_SECONDS ** tentativa
            print(f"  [retry {tentativa}/{MAX_RETRIES}] {endpoint} pag {pagina}: {e} "
                  f"-- aguardando {espera}s")
            time.sleep(espera)
    raise RuntimeError(f"Falhou após {MAX_RETRIES} tentativas em {endpoint} "
                        f"pag {pagina}: {ultimo_erro}")


def coletar_paginado(endpoint: str, params: dict, chave_lista: str,
                      max_paginas_teste=2) -> list:
    """
    Pagina um endpoint até acabar (metaDados.qtdPaginas, ou lista vazia).
    Retorna a lista combinada de todos os registros.
    """
    # Status que a API devolve legitimamente para "sem dados nesse filtro/mês"
    # -- não é erro, é comum em /movimentosReceita quando consultamos código a
    # código, mês a mês (um código pode só ter tido movimento em alguns meses).
    status_vazio_esperado = {"SEM REGISTROS", "SEM REGISTRO", "NAO ENCONTRADO",
                              "NÃO ENCONTRADO"}
    todos = []
    pagina = 1
    while True:
        dados = _requisitar_pagina(endpoint, params, pagina)
        meta = dados.get("metaDados", {})
        status = (meta.get("txtStatus") or "").strip()
        if status not in ("", "OK") and status.upper() not in status_vazio_esperado:
            print(f"  [aviso] status inesperado em {endpoint} pag {pagina}: "
                  f"{meta.get('txtStatus')} / {meta.get('txtMensagemErro')}")
        registros = dados.get(chave_lista, [])
        if not registros:
            break
        todos.extend(registros)

        qtd_paginas = meta.get("qtdPaginas")
        if qtd_paginas is not None and pagina >= qtd_paginas:
            break
        if MODO_TESTE and pagina >= max_paginas_teste:
            print(f"  [modo teste] parando em {pagina} páginas (de {qtd_paginas} totais)")
            break
        pagina += 1
    return todos


# =====================================================================================
# COLETORES POR ENDPOINT
# =====================================================================================

def coletar_contas_receita(ano: int) -> list:
    print(f"[contasReceita] ano={ano}")
    params = {"anoExercicio": ano}
    return coletar_paginado("/contasReceita", params, "lstReceita")


def coletar_movimento_receita_por_codigo(ano: int, mes: int, cod_receita: str) -> list:
    """Consulta direta por código já identificado como FMD (evita baixar tudo)."""
    params = {
        "anoExercicio": ano,
        "codEmpresa": COD_EMPRESA_PREFEITURA,
        "mesAteMovimento": mes,
        "codReceita": cod_receita,
    }
    return coletar_paginado("/movimentosReceita", params, "lstMovimentosReceita")


def coletar_despesas(ano: int, mes: int) -> list:
    print(f"[despesas] ano={ano} mes={mes}")
    params = {
        "anoDotacao": ano,
        "mesDotacao": mes,
        "codEmpresa": COD_EMPRESA_PREFEITURA,
        "codOrgao": COD_ORGAO_FMD,
        "codUnidade": COD_UNIDADE_FMD,
    }
    return coletar_paginado("/despesas", params, "lstDespesas")


def coletar_empenhos(ano: int, mes: int) -> list:
    print(f"[empenhos] ano={ano} mes={mes}")
    params = {
        "anoEmpenho": ano,
        "mesEmpenho": mes,  # acumulado até o mês, conforme swagger
        "codEmpresa": COD_EMPRESA_PREFEITURA,
        "codOrgao": COD_ORGAO_FMD,
        "codUnidade": COD_UNIDADE_FMD,
    }
    return coletar_paginado("/empenhos", params, "lstEmpenhos")


def coletar_despesas_credor(ano: int, mes: int) -> list:
    print(f"[despesasCredor] ano={ano} mes={mes}")
    params = {
        "anoExercicio": ano,
        "mesEmpenho": mes,  # AQUI é o mês exato, não acumulado
        "codEmpresa": COD_EMPRESA_PREFEITURA,
        "codOrgao": COD_ORGAO_FMD,
        "codUnidade": COD_UNIDADE_FMD,
    }
    return coletar_paginado("/despesasCredor", params, "lstDespesaCredores")


def coletar_contratos(ano: int) -> list:
    print(f"[contratos] ano={ano}")
    params = {
        "anoContrato": ano,
        "codEmpresa": COD_EMPRESA_PREFEITURA,
        "codOrgao": COD_ORGAO_FMD,
    }
    return coletar_paginado("/contratos", params, "lstContratos")


def coletar_credores_de_contrato(cod_contrato, cod_empresa, ano_exercicio) -> list:
    params = {
        "codContrato": cod_contrato,
        "codEmpresa": cod_empresa,
        "anoExercicio": ano_exercicio,
    }
    return coletar_paginado("/credoresDeContrato", params, "lstCredoresContrado")


def coletar_fonte_recursos(ano: int) -> list:
    print(f"[fonteRecursos] ano={ano}")
    params = {"anoExercicio": ano}
    return coletar_paginado("/fonteRecursos", params, "lstFonteRecurso")


def coletar_liquidacoes(cod_empenho, ano_empenho, cod_empresa) -> list:
    params = {
        "codEmpenho": cod_empenho,
        "anoEmpenho": ano_empenho,
        "codEmpresa": cod_empresa,
    }
    return coletar_paginado("/liquidacoes", params, "lstLiquidacoes")


def coletar_compromissos_pagar(ano_empenho: int) -> list:
    # ATENÇÃO: não filtra por órgão -- traz a cidade inteira. Só usar sob demanda.
    params = {"anoEmpenho": ano_empenho}
    return coletar_paginado("/CompromissosPagar", params, "lstCompromisso")


# =====================================================================================
# FILTRO DE RECEITA DO FMD
# =====================================================================================

def classificar_tipo_no_receita(descricao: str) -> str:
    """
    Heurística para não somar bruto + multas + dedução + total como se fossem
    receitas independentes na hora de agregar depois.
    """
    d = normalizar_texto(descricao)
    if "total" in d:
        return "total_subtotal"
    if "dedu" in d:
        return "deducao"
    if "multa" in d or "juro" in d:
        return "multas_e_juros"
    return "bruto_ou_outro"


def filtrar_receita_fmd(registros_contas_receita: list) -> list:
    """
    Aplica as duas regras de texto (tag 'FMD' e frase completa) e devolve só
    os registros batidos, com colunas extras: matched_by e tipo_no_receita.
    """
    filtrados = []
    for registro in registros_contas_receita:
        descricao = registro.get("txtDescricaoReceita", "")
        d_norm = normalizar_texto(descricao)
        matches = []
        # tag "fmd" com fronteira de palavra (\b): pega tanto " - FMD" quanto
        # "CONCESSÕES-FMD" colado no hífen (ambos os formatos existem nos dados
        # reais), mas não casa "fmd" no meio de uma palavra maior.
        if re.search(r"\bfmd\b", d_norm):
            matches.append("tag_FMD")
        if FILTRO_RECEITA_FRASE in d_norm:
            matches.append("frase_completa")
        if matches:
            novo = dict(registro)
            novo["matched_by"] = ";".join(matches)
            novo["tipo_no_receita"] = classificar_tipo_no_receita(descricao)
            filtrados.append(novo)
    return filtrados


# =====================================================================================
# ORQUESTRAÇÃO
# =====================================================================================

def rodar_frente_receita():
    print("\n" + "=" * 80)
    print("FRENTE 1: RECEITA (contasReceita + movimentosReceita filtrados por texto)")
    print("=" * 80)

    todas_filtradas_por_ano = {}

    for ano in ANOS:
        contas = coletar_contas_receita(ano)
        salvar_csv(contas, RAW_DIR / f"contasReceita_{ano}.csv")

        filtradas = filtrar_receita_fmd(contas)
        print(f"  -> {len(filtradas)} códigos de receita batidos como FMD em {ano}")
        todas_filtradas_por_ano[ano] = filtradas

    # base filtrada consolidada (todos os anos, para inspeção/auditoria)
    todas_filtradas_flat = []
    for ano, lst in todas_filtradas_por_ano.items():
        for row in lst:
            row_com_ano = dict(row)
            row_com_ano["anoExercicio_consulta"] = ano
            todas_filtradas_flat.append(row_com_ano)
    salvar_csv(todas_filtradas_flat, FILTERED_DIR / "fmd_receita_codigos.csv")

    # movimentos (valores) só para os códigos batidos, mês a mês
    for ano in ANOS:
        codigos_fmd = sorted({row["codReceita"] for row in todas_filtradas_por_ano[ano]})
        if not codigos_fmd:
            print(f"  [aviso] nenhum código FMD encontrado em {ano}, pulando movimentos.")
            continue
        movimentos_ano = []
        for mes in MESES:
            for cod in codigos_fmd:
                registros = coletar_movimento_receita_por_codigo(ano, mes, cod)
                for r in registros:
                    r = dict(r)
                    r["mesAteMovimento_consulta"] = mes
                    movimentos_ano.append(r)
        salvar_csv(movimentos_ano, FILTERED_DIR / f"fmd_movimentosReceita_{ano}.csv")


def rodar_frente_despesa():
    print("\n" + "=" * 80)
    print("FRENTE 2: DESPESA (filtro nativo codOrgao=07 / codUnidade=10)")
    print("=" * 80)

    todos_empenhos = []
    todos_contratos = []

    for ano in ANOS:
        # despesas: resumo por mês
        despesas_ano = []
        for mes in MESES:
            despesas_ano.extend(coletar_despesas(ano, mes))
        salvar_csv(despesas_ano, RAW_DIR / f"despesas_fmd_{ano}.csv")

        # empenhos: núcleo da base
        # /empenhos é cumulativo ("até o mês informado", confirmado no swagger)
        # -- consultar mês a mês e concatenar geraria o MESMO empenho repetido
        # várias vezes (uma por mês em que já estava incluso no acumulado).
        # Uma única consulta no último mês do período já traz o ano inteiro.
        mes_final_empenhos = max(MESES)
        empenhos_ano = coletar_empenhos(ano, mes_final_empenhos)
        salvar_csv(empenhos_ano, RAW_DIR / f"empenhos_fmd_{ano}.csv")
        todos_empenhos.extend(empenhos_ano)

        # despesasCredor: mês a mês (não é acumulado aqui)
        credor_ano = []
        for mes in MESES:
            credor_ano.extend(coletar_despesas_credor(ano, mes))
        salvar_csv(credor_ano, RAW_DIR / f"despesasCredor_fmd_{ano}.csv")

        # contratos: por ano
        contratos_ano = coletar_contratos(ano)
        salvar_csv(contratos_ano, RAW_DIR / f"contratos_fmd_{ano}.csv")
        todos_contratos.extend(contratos_ano)

        # fonteRecursos: dicionário do ano, filtrado aos códigos usados nos empenhos
        fontes_ano = coletar_fonte_recursos(ano)
        codigos_fonte_usados = {
            e.get("codFonteRecurso") for e in empenhos_ano if e.get("codFonteRecurso")
        }
        fontes_relevantes = [
            f for f in fontes_ano if f.get("codFonteRecurso") in codigos_fonte_usados
        ]
        salvar_csv(fontes_relevantes, REFERENCE_DIR / f"fonteRecursos_fmd_{ano}.csv")

    # credoresDeContrato: drill-down por contrato (só os do FMD já coletados)
    credores_contrato_todos = []
    for c in todos_contratos:
        cod_contrato = c.get("codContrato")
        cod_empresa = c.get("codEmpresa")
        ano_contrato = c.get("anoContrato")
        if cod_contrato is None or cod_empresa is None or ano_contrato is None:
            continue
        registros = coletar_credores_de_contrato(cod_contrato, cod_empresa, ano_contrato)
        credores_contrato_todos.extend(registros)
    salvar_csv(credores_contrato_todos, REFERENCE_DIR / "credoresDeContrato_fmd.csv")

    # camadas opcionais
    if RODAR_LIQUIDACOES:
        print("\n[opcional] coletando liquidacoes por empenho (pode ser lento)...")
        liquidacoes_todas = []
        for e in todos_empenhos:
            cod_empenho = e.get("codEmpenho")
            ano_empenho = e.get("anoEmpenho")
            cod_empresa = e.get("codEmpresa")
            if cod_empenho is None or ano_empenho is None or cod_empresa is None:
                continue
            registros = coletar_liquidacoes(cod_empenho, ano_empenho, cod_empresa)
            liquidacoes_todas.extend(registros)
        salvar_csv(liquidacoes_todas, DETALHE_DIR / "liquidacoes_fmd.csv")

    if RODAR_COMPROMISSOS_PAGAR:
        print("\n[opcional] coletando CompromissosPagar (cidade inteira, filtrando "
              "localmente pelos empenhos do FMD -- pode ser MUITO lento)...")
        numeros_empenho_fmd = {
            (e.get("numReserva") or e.get("codEmpenho"), e.get("anoEmpenho"))
            for e in todos_empenhos
        }
        compromissos_fmd = []
        for ano in ANOS:
            todos_do_ano = coletar_compromissos_pagar(ano)
            for c in todos_do_ano:
                chave = (c.get("numeroEmpenho"), c.get("anoEmpenho"))
                if chave in numeros_empenho_fmd:
                    compromissos_fmd.append(c)
        salvar_csv(compromissos_fmd, DETALHE_DIR / "compromissosPagar_fmd.csv")


def main():
    if API_TOKEN == "COLOQUE_SEU_TOKEN_AQUI":
        print("!! Preencha API_TOKEN antes de rodar. Abortando. !!")
        return

    inicio = datetime.now()
    print(f"Iniciando coleta em {inicio:%Y-%m-%d %H:%M:%S} "
          f"(MODO_TESTE={MODO_TESTE}, anos={ANOS}, meses={MESES})")

    rodou_algo = False
    if RODAR_FRENTE_RECEITA:
        rodar_frente_receita()
        rodou_algo = True
    if RODAR_FRENTE_DESPESA:
        rodar_frente_despesa()
        rodou_algo = True
    if not rodou_algo:
        print("RODAR_FRENTE_RECEITA e RODAR_FRENTE_DESPESA estão False -- nada a fazer.")

    fim = datetime.now()
    print(f"\nConcluído em {fim:%Y-%m-%d %H:%M:%S} (duração: {fim - inicio})")
    print(f"Resultados em: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()