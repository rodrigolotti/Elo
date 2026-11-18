"""Demo do lado do CLIENTE: gera as chaves das empresas aqui e fala com a API.

As chaves privadas nascem neste processo e nunca saem dele: para a API só vão
as chaves públicas e os objetos já assinados. Dados fictícios.

Com a API no ar (uvicorn elo.api:app), rode:
    python demo_api.py
    python demo_api.py --url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from elo.chaves import ParDeChaves
from elo.cnpj import calcular_digitos, formatar_cnpj

BRT = timezone(timedelta(hours=-3))


def cnpj_aleatorio() -> str:
    """CNPJ fictício com dígitos válidos, novo a cada execução (o banco persiste)."""
    base = f"{secrets.randbelow(10**8):08d}0001"
    return formatar_cnpj(base + calcular_digitos(base))


def _ok(resposta: httpx.Response) -> dict[str, Any]:
    if resposta.is_error:
        raise RuntimeError(f"{resposta.request.method} {resposta.request.url.path} -> "
                           f"{resposta.status_code}: {resposta.text}")
    return resposta.json()


def assinar_e_enviar(cliente: httpx.Client, rota_rascunho: str, corpo: dict[str, Any],
                     chave: ParDeChaves, rota_registro: str) -> tuple[dict, dict]:
    """Pede o rascunho, assina AQUI com a chave local e envia o objeto assinado."""
    preparado = _ok(cliente.post(rota_rascunho, json=corpo))
    assinado = chave.assinar(preparado["rascunho"])
    return preparado, _ok(cliente.post(rota_registro, json=assinado))


def executar(cliente: httpx.Client) -> list[str]:
    """Roda o cenário de ponta a ponta. Devolve os ids dos compromissos firmados."""
    agora = datetime.now(BRT)
    cnpj_transp, cnpj_embarc = cnpj_aleatorio(), cnpj_aleatorio()

    # chaves geradas localmente; só a parte pública vai para a API
    chave_org = ParDeChaves.gerar("transp-exemplo#k1")
    chave_agente = ParDeChaves.gerar("agente-cotacao#k1")

    print("1. Cadastrando a transportadora (cadastro autoassinado)")
    assinar_e_enviar(cliente, "/organizacoes/rascunho", {
        "cnpj": cnpj_transp, "razao_social": "Transportadora Exemplo Ltda",
        "chave_id": chave_org.chave_id, "chave_publica": chave_org.publica_b64,
    }, chave_org, "/organizacoes")
    print(f"   {cnpj_transp} cadastrada")

    print("2. Emitindo o certificado do agente (assinado pela organização)")
    _, agente = assinar_e_enviar(cliente, "/agentes/rascunho", {
        "cnpj": cnpj_transp, "nome": "Agente de cotação",
        "chave_id": chave_agente.chave_id, "chave_publica": chave_agente.publica_b64,
    }, chave_org, "/agentes")
    print(f"   agente {agente['agente_id']}")

    print("3. Emitindo o mandato: até R$ 6.000,00, SP -> SP/PR, carga geral")
    _, mandato = assinar_e_enviar(cliente, "/mandatos/rascunho", {
        "cnpj": cnpj_transp, "agente_id": agente["agente_id"],
        "limites": {
            "valor_max_centavos": 600_000, "ufs_origem": ["SP"],
            "ufs_destino": ["SP", "PR"], "tipos_carga": ["carga geral"],
            "valido_de": (agora - timedelta(days=1)).isoformat(timespec="seconds"),
            "valido_ate": (agora + timedelta(days=30)).isoformat(timespec="seconds"),
        },
    }, chave_org, "/mandatos")
    print(f"   mandato {mandato['id']}")

    compromissos = []
    for passo, valor, descricao in [(4, 485_000, "dentro do mandato"),
                                    (5, 920_000, "acima do limite")]:
        print(f"{passo}. Pedido de cotação e oferta de R$ {valor // 100:,} ({descricao})"
              .replace(",", "."))
        pedido = _ok(cliente.post("/pedidos", json={
            "cnpj_solicitante": cnpj_embarc, "canal": "email",
            "carga": {"origem_cep": "00000-000", "origem_uf": "SP",
                      "destino_cep": "11111-111", "destino_uf": "PR",
                      "peso_kg": 3200, "volume_litros": 14500, "tipo": "carga geral"},
            "mensagem_original": "Preciso de cotação de SP para Curitiba.",
        }))
        preparado, emitida = assinar_e_enviar(cliente, "/ofertas/rascunho", {
            "pedido_id": pedido["id"], "agente_id": agente["agente_id"],
            "mandato_id": mandato["id"],
            "condicoes": {"valor_centavos": valor,
                          "coleta_ate": (agora + timedelta(days=3)).date().isoformat(),
                          "prazo_entrega_dias_uteis": 2,
                          "inclui": ["pedagio", "seguro_rctr_c"]},
            "valida_ate": (agora + timedelta(days=1)).isoformat(timespec="seconds"),
        }, chave_agente, "/ofertas")
        oferta_id = emitida["oferta"]["id"]
        print(f"   oferta {oferta_id}: {emitida['status']}")

        if emitida["status"] == "aguardando_aprovacao":
            print(f"   fora do mandato: {'; '.join(preparado['violacoes'])}")
            assinar_e_enviar(cliente, f"/ofertas/{oferta_id}/aprovacao/rascunho",
                             {"aprovado_por": "Gerente Comercial"}, chave_org,
                             f"/ofertas/{oferta_id}/aprovacao")
            print("   gerente aprovou com a chave da organização (assinada localmente)")

        cp = _ok(cliente.post(f"/ofertas/{oferta_id}/aceite", json={
            "cnpj": cnpj_embarc, "nome": "Maria Exemplo", "email": "maria@exemplo.com"}))
        relatorio = _ok(cliente.get(f"/compromissos/{cp['id']}/verificacao"))
        ok = sum(v["ok"] for v in relatorio["verificacoes"])
        print(f"   compromisso {cp['id']}: {ok}/{len(relatorio['verificacoes'])} "
              f"checagens OK -> {'VÁLIDO' if relatorio['valido'] else 'INVÁLIDO'}")
        compromissos.append(cp["id"])
    return compromissos


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000",
                        help="endereço da API (padrão: %(default)s)")
    url = parser.parse_args().url.rstrip("/")
    try:
        with httpx.Client(base_url=url, timeout=10) as cliente:
            compromissos = executar(cliente)
    except httpx.ConnectError:
        print(f"Não consegui falar com a API em {url}. Ela está no ar? "
              "(uvicorn elo.api:app)", file=sys.stderr)
        return 1
    print("\nAbra no navegador:")
    for cid in compromissos:
        print(f"   {url}/verificar/{cid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
