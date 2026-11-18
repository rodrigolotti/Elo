"""Simula um agente de cotação e um embarcador fictício, só pela API do Elo.

Serve para testar o fluxo de fretes no painel sem um agente de verdade.

    python scripts\\simular_agente.py chave
        Gera a chave do agente, guarda a parte privada em chaves_locais\\agente.json
        (pasta ignorada pelo git) e imprime o que colar na tela Agentes do painel.

    python scripts\\simular_agente.py oferta --cnpj 11.222.333/0001-81 --mandato md_... --valor 4850 --agente ag_...
        Registra o pedido de um embarcador fictício e emite uma oferta assinada pelo
        agente. O --agente (coluna ID da tela Agentes) só é preciso na primeira vez:
        depois fica lembrado no arquivo da chave.

    python scripts\\simular_agente.py aceitar --oferta of_... --token ...
        Aceita a oferta como o embarcador fictício e mostra o link de verificação.

Opção comum: --url (padrão http://127.0.0.1:8000).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from elo.chaves import ParDeChaves  # noqa: E402
from elo.cnpj import formatar_cnpj, validar_cnpj  # noqa: E402
from elo.mandato import reais  # noqa: E402

PASTA_PADRAO = RAIZ / "chaves_locais"  # no .gitignore
ARQUIVO_CHAVE = "agente.json"
CHAVE_ID_PADRAO = "agente-simulado#k1"
# CNPJ de exemplo dos testes (não sorteado, para não cair num número de empresa real)
EMBARCADOR_PADRAO = "22.333.444/0001-81"
BRT = timezone(timedelta(hours=-3))


class ErroSimulador(Exception):
    """Erro com mensagem para mostrar a quem está testando."""


# ---------- utilidades ----------

def centavos_de_reais(texto: str) -> int:
    """'4850', '4.850,50', 'R$ 4850,5' -> centavos. Sem float."""
    limpo = re.sub(r"^\s*R\$\s*", "", str(texto)).strip()
    casa = re.fullmatch(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?", limpo)
    if not casa:
        raise ErroSimulador(f'valor em reais não reconhecido: "{texto}" (use, por exemplo, 4850 ou 4.850,00)')
    return int(casa.group(1).replace(".", "")) * 100 + int((casa.group(2) or "0").ljust(2, "0"))


def _resposta(r: Any) -> dict[str, Any]:
    try:
        dados = r.json()
    except ValueError:
        dados = {}
    if r.status_code >= 400:
        detalhe = dados.get("detail", r.text) if isinstance(dados, dict) else r.text
        if isinstance(detalhe, list):  # 422 do FastAPI
            detalhe = "; ".join(f"{'.'.join(map(str, d.get('loc', [])[1:]))}: {d.get('msg')}"
                                for d in detalhe)
        raise ErroSimulador(f"a API recusou ({r.status_code}): {detalhe}")
    return dados


def _arquivo(pasta: Path) -> Path:
    return Path(pasta) / ARQUIVO_CHAVE


def _ler_chave(pasta: Path) -> dict[str, Any]:
    arquivo = _arquivo(pasta)
    if not arquivo.is_file():
        raise ErroSimulador(f"não achei a chave do agente em {arquivo}: rode antes "
                            "'python scripts\\simular_agente.py chave'")
    return json.loads(arquivo.read_text(encoding="utf-8"))


def _par(dados: dict[str, Any]) -> ParDeChaves:
    privada = Ed25519PrivateKey.from_private_bytes(base64.b64decode(dados["privada"]))
    return ParDeChaves(dados["chave_id"], privada)


# ---------- comandos ----------

def criar_chave(pasta: Path = PASTA_PADRAO, chave_id: str = CHAVE_ID_PADRAO,
                substituir: bool = False) -> dict[str, Any]:
    """Gera a chave do agente e guarda a parte privada num arquivo local."""
    arquivo = _arquivo(pasta)
    if arquivo.exists() and not substituir:
        raise ErroSimulador(f"já existe uma chave de agente em {arquivo}. Para gerar outra "
                            "(a anterior deixa de servir), use --substituir")
    par = ParDeChaves.gerar(chave_id)
    bruta = par.privada.private_bytes(serialization.Encoding.Raw,
                                      serialization.PrivateFormat.Raw,
                                      serialization.NoEncryption())
    dados = {"chave_id": chave_id, "chave_publica": par.publica_b64,
             "privada": base64.b64encode(bruta).decode("ascii"), "agente_id": None}
    Path(pasta).mkdir(parents=True, exist_ok=True)
    arquivo.write_text(json.dumps(dados, indent=2) + "\n", encoding="utf-8")
    return {"arquivo": str(arquivo), "chave_id": chave_id, "chave_publica": par.publica_b64}


def emitir_oferta(cliente: Any, *, cnpj: str, mandato: str, valor: str,
                  agente: str | None = None, pasta: Path = PASTA_PADRAO,
                  embarcador: str = EMBARCADOR_PADRAO, origem: str = "SP",
                  destino: str = "PR", tipo: str = "carga geral", peso_kg: int = 3200,
                  volume_litros: int = 14500) -> dict[str, Any]:
    """Registra o pedido do embarcador fictício e emite a oferta assinada pelo agente."""
    if not validar_cnpj(cnpj):
        raise ErroSimulador(f"CNPJ da transportadora inválido: {cnpj}")
    dados = _ler_chave(pasta)
    agente_id = agente or dados.get("agente_id")
    if not agente_id:
        raise ErroSimulador("informe --agente na primeira oferta: é o ID do agente na tela "
                            "Agentes do painel (começa com ag_)")
    centavos = centavos_de_reais(valor)

    pedido = _resposta(cliente.post("/pedidos", json={
        "cnpj_solicitante": embarcador, "canal": "simulador",
        "mensagem_original": "Pedido de cotação gerado por scripts/simular_agente.py",
        "carga": {"origem_cep": "01000-000", "origem_uf": origem.upper(),
                  "destino_cep": "80000-000", "destino_uf": destino.upper(),
                  "peso_kg": peso_kg, "volume_litros": volume_litros, "tipo": tipo},
    }))
    agora = datetime.now(BRT)
    preparado = _resposta(cliente.post("/ofertas/rascunho", json={
        "pedido_id": pedido["id"], "agente_id": agente_id, "mandato_id": mandato,
        "condicoes": {"valor_centavos": centavos,
                      "coleta_ate": (date.today() + timedelta(days=2)).isoformat(),
                      "prazo_entrega_dias_uteis": 2, "inclui": ["pedágio"]},
        "valida_ate": (agora + timedelta(days=1)).isoformat(timespec="seconds"),
    }))
    rascunho = preparado["rascunho"]
    if rascunho["emissor"]["cnpj"] != formatar_cnpj(cnpj):
        raise ErroSimulador(f"este agente é da transportadora {rascunho['emissor']['cnpj']}, "
                            f"não de {formatar_cnpj(cnpj)}: confira --cnpj e --agente")
    if rascunho["emissor"]["agente_id"] != agente_id or rascunho["mandato_id"] != mandato \
            or rascunho["condicoes"]["valor_centavos"] != centavos:
        raise ErroSimulador("a API devolveu um rascunho diferente do pedido; nada foi assinado")
    emitida = _resposta(cliente.post("/ofertas", json=_par(dados).assinar(rascunho)))

    if dados.get("agente_id") != agente_id:  # lembra o agente para as próximas ofertas
        dados["agente_id"] = agente_id
        _arquivo(pasta).write_text(json.dumps(dados, indent=2) + "\n", encoding="utf-8")
    return {"oferta": emitida["oferta"], "status": emitida["status"],
            "violacoes": emitida["violacoes"], "token": emitida["token_aceite"],
            "embarcador": embarcador}


def aceitar(cliente: Any, *, oferta: str, token: str,
            embarcador: str = EMBARCADOR_PADRAO) -> dict[str, Any]:
    """Aceita a oferta como o embarcador fictício."""
    try:
        return _resposta(cliente.post(f"/ofertas/{oferta}/aceite", json={
            "token": token, "cnpj": embarcador, "nome": "Compras (embarcador fictício)",
            "email": "compras@embarcador.exemplo"}))
    except ErroSimulador as erro:
        if "aguardando_aprovacao" in str(erro):
            raise ErroSimulador("a oferta ainda está aguardando aprovação: aprove no painel "
                                "(tela Aprovações) e tente de novo") from None
        if "recusada" in str(erro):
            raise ErroSimulador("a oferta foi recusada no painel e não pode ser aceita") from None
        raise


# ---------- linha de comando ----------

def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    raiz = argparse.ArgumentParser(
        prog="simular_agente.py",
        description="Simula um agente de cotação e um embarcador fictício pela API do Elo.")
    raiz.add_argument("--url", default="http://127.0.0.1:8000", help="endereço da API")
    raiz.add_argument("--pasta", type=Path, default=PASTA_PADRAO,
                      help="onde guardar a chave do agente (padrão: chaves_locais)")
    comandos = raiz.add_subparsers(dest="comando", required=True)

    chave = comandos.add_parser("chave", help="gera a chave do agente")
    chave.add_argument("--id", default=CHAVE_ID_PADRAO, help="identificador da chave")
    chave.add_argument("--substituir", action="store_true", help="troca a chave existente")

    oferta = comandos.add_parser("oferta", help="registra um pedido e emite uma oferta")
    oferta.add_argument("--cnpj", required=True, help="CNPJ da transportadora")
    oferta.add_argument("--mandato", required=True, help="ID do mandato (tela Mandatos)")
    oferta.add_argument("--valor", required=True, help="valor em reais, ex.: 4850 ou 4.850,00")
    oferta.add_argument("--agente", help="ID do agente (tela Agentes); só na primeira vez")
    oferta.add_argument("--embarcador", default=EMBARCADOR_PADRAO, help="CNPJ do embarcador")
    oferta.add_argument("--origem", default="SP", help="UF de origem")
    oferta.add_argument("--destino", default="PR", help="UF de destino")
    oferta.add_argument("--tipo", default="carga geral", help="tipo de carga")

    aceite = comandos.add_parser("aceitar", help="aceita a oferta como o embarcador")
    aceite.add_argument("--oferta", required=True, help="ID da oferta (of_...)")
    aceite.add_argument("--token", required=True, help="token de aceite mostrado na oferta")
    aceite.add_argument("--embarcador", default=EMBARCADOR_PADRAO, help="CNPJ do embarcador")
    return raiz.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8")  # console cp1252 do Windows
    args = _argumentos(argv)
    try:
        if args.comando == "chave":
            r = criar_chave(args.pasta, args.id, args.substituir)
            ignorada = Path(args.pasta).resolve() == PASTA_PADRAO.resolve()
            print(f"Chave do agente criada e guardada em {r['arquivo']}"
                  + (" (o git ignora esta pasta)." if ignorada else
                     ". Atenção: esta pasta não é a padrão; não a coloque no git."))
            print("\nNa tela Agentes do painel, preencha:")
            print(f"  Identificador da chave: {r['chave_id']}")
            print(f"  Chave pública do agente: {r['chave_publica']}")
            return 0

        import httpx2 as httpx
        with httpx.Client(base_url=args.url, timeout=15) as cliente:
            if args.comando == "oferta":
                r = emitir_oferta(cliente, cnpj=args.cnpj, mandato=args.mandato,
                                  valor=args.valor, agente=args.agente, pasta=args.pasta,
                                  embarcador=args.embarcador, origem=args.origem,
                                  destino=args.destino, tipo=args.tipo)
                o = r["oferta"]
                print(f"Oferta {o['id']}: {reais(o['condicoes']['valor_centavos'])}, "
                      f"{o['carga']['origem_uf']} → {o['carga']['destino_uf']}, "
                      f"para o embarcador {r['embarcador']}.")
                if r["status"] == "enviada":
                    print("Dentro do mandato: já está com o embarcador.")
                else:
                    print("Fora do mandato: está na tela Aprovações do painel. Motivo:")
                    for v in r["violacoes"]:
                        print(f"  - {v}")
                print(f"\nToken de aceite (aparece só agora): {r['token']}")
                print("Para aceitar como o embarcador"
                      + (" (depois de aprovar no painel)" if r["status"] != "enviada" else "") + ":")
                print(f"  python scripts\\simular_agente.py aceitar --oferta {o['id']} "
                      f"--token {r['token']}")
            else:
                cp = aceitar(cliente, oferta=args.oferta, token=args.token,
                             embarcador=args.embarcador)
                print(f"Oferta aceita. Compromisso {cp['id']}.")
                print(f"Verifique em: {args.url.rstrip('/')}/verificar/{cp['id']}")
        return 0
    except ErroSimulador as erro:
        print(f"Erro: {erro}", file=sys.stderr)
        return 1
    except Exception as erro:  # API fora do ar, por exemplo
        if type(erro).__name__ in ("ConnectError", "ConnectTimeout"):
            print(f"Erro: não consegui falar com a API em {args.url}. Ela está no ar? "
                  "(uvicorn elo.api:app)", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
