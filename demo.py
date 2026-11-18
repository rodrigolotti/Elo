"""Demonstração de ponta a ponta do núcleo do Elo, com dados fictícios.

Rode com:  python3 demo.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from elo.chaves import ParDeChaves
from elo.cnpj import calcular_digitos, formatar_cnpj
from elo.mandato import reais
from elo.modelos import Carga, Condicoes, LimitesMandato
from elo.registro import Registro
from elo.servico import Plataforma

BRT = timezone(timedelta(hours=-3))


class Relogio:
    def __init__(self, inicio: datetime) -> None:
        self.agora = inicio

    def __call__(self) -> datetime:
        return self.agora

    def avancar(self, **kw) -> None:
        self.agora += timedelta(**kw)


def cnpj(base12: str) -> str:
    return formatar_cnpj(base12 + calcular_digitos(base12))


def titulo(texto: str) -> None:
    print(f"\n=== {texto} ===")


def mostrar_relatorio(relatorio) -> None:
    for v in relatorio.verificacoes:
        marca = "OK  " if v.ok else "FALHA"
        print(f"  [{marca}] {v.nome}" + (f" — {v.detalhe}" if v.detalhe else ""))
    print(f"  Resultado: {'COMPROMISSO VÁLIDO' if relatorio.valido else 'COMPROMISSO INVÁLIDO'}")


def main() -> None:
    relogio = Relogio(datetime(2026, 11, 17, 9, 0, tzinfo=BRT))
    plataforma = Plataforma(Registro(), ParDeChaves.gerar("plataforma#k1"), relogio)

    cnpj_transp = cnpj("112223330001")
    cnpj_embarc = cnpj("223334440001")
    chave_org = ParDeChaves.gerar("transp-exemplo#k1")
    chave_agente = ParDeChaves.gerar("agente-cotacao#k1")

    titulo("1. Transportadora se cadastra e cria seu agente")
    plataforma.cadastrar_organizacao(cnpj_transp, "Transportadora Exemplo Ltda", chave_org)
    agente = plataforma.emitir_agente(cnpj_transp, chave_org, chave_agente, "Agente de cotação")
    print(f"  Organização {cnpj_transp} cadastrada")
    print(f"  Agente {agente['agente_id']} emitido e assinado pela organização")

    titulo("2. Transportadora define o mandato do agente")
    limites = LimitesMandato(
        valor_max_centavos=600_000, ufs_origem=["SP"], ufs_destino=["SP", "PR"],
        tipos_carga=["carga geral"], valido_de="2026-11-01T00:00:00-03:00",
        valido_ate="2026-12-31T23:59:59-03:00",
    )
    mandato = plataforma.emitir_mandato(cnpj_transp, chave_org, agente["agente_id"], limites)
    print(f"  Mandato {mandato['id']}: até {reais(limites.valor_max_centavos)}, "
          f"origem SP, destino SP ou PR, carga geral")

    titulo("3. Chega um pedido de cotação por e-mail")
    pedido = plataforma.registrar_pedido(
        cnpj_embarc, "email",
        Carga(origem_cep="00000-000", origem_uf="SP", destino_cep="11111-111",
              destino_uf="PR", peso_kg=3200, volume_litros=14500, tipo="carga geral"),
        "Bom dia, preciso de cotação para 3,2 t de SP para Curitiba, coleta até dia 20.",
    )
    print(f"  Pedido {pedido['id']} de {cnpj_embarc}: SP → PR, 3.200 kg, 14.500 L")

    titulo("4. O agente oferece dentro do mandato e o embarcador aceita")
    cond = Condicoes(valor_centavos=485_000, coleta_ate="2026-11-20",
                     prazo_entrega_dias_uteis=2, inclui=["pedagio", "seguro_rctr_c"])
    oferta, problemas, token = plataforma.emitir_oferta(
        pedido["id"], agente["agente_id"], mandato["id"], chave_agente, cond,
        "2026-11-18T18:00:00-03:00")
    print(f"  Oferta {oferta['id']} de {reais(cond.valor_centavos)}: "
          f"{'enviada direto' if not problemas else 'aguardando aprovação'}")
    relogio.avancar(hours=1, minutes=42)
    cp = plataforma.aceitar_oferta(oferta["id"], cnpj_embarc, "Maria Exemplo",
                                   "maria@exemplo.com", token)
    print(f"  Compromisso {cp['id']} firmado às {cp['aceite']['em']}")

    titulo("5. Qualquer parte verifica o compromisso")
    mostrar_relatorio(plataforma.verificar_compromisso(cp["id"]))

    titulo("6. Um segundo pedido sai do mandato e vai para aprovação humana")
    pedido2 = plataforma.registrar_pedido(
        cnpj_embarc, "formulario",
        Carga(origem_cep="00000-000", origem_uf="SP", destino_cep="22222-222",
              destino_uf="PR", peso_kg=12000, volume_litros=60000, tipo="carga geral"))
    cond2 = Condicoes(valor_centavos=920_000, coleta_ate="2026-11-21",
                      prazo_entrega_dias_uteis=3)
    oferta2, problemas2, token2 = plataforma.emitir_oferta(
        pedido2["id"], agente["agente_id"], mandato["id"], chave_agente, cond2,
        "2026-11-19T18:00:00-03:00")
    print(f"  Oferta bloqueada pelo mandato: {problemas2[0]}")
    plataforma.aprovar_oferta(oferta2["id"], chave_org, "Gerente Comercial")
    print("  Gerente aprovou com a chave da organização")
    relogio.avancar(minutes=30)
    cp2 = plataforma.aceitar_oferta(oferta2["id"], cnpj_embarc, "Maria Exemplo",
                                    "maria@exemplo.com", token2)
    mostrar_relatorio(plataforma.verificar_compromisso(cp2["id"]))

    titulo("7. Compromisso em JSON (o que a página de verificação mostraria)")
    print(json.dumps(cp, ensure_ascii=False, indent=2))

    titulo("8. Alguém com acesso ao banco tenta mudar o valor da primeira oferta")
    conexao = plataforma.registro.conexao
    try:
        conexao.execute("UPDATE eventos SET tipo = tipo WHERE seq = 1")
    except Exception as erro:  # noqa: BLE001
        print(f"  Pela aplicação: recusado ({erro})")
    conexao.execute("DROP TRIGGER eventos_sem_update")
    seq, payload = conexao.execute(
        "SELECT seq, payload FROM eventos WHERE objeto_id = ?", (oferta["id"],)).fetchone()
    conexao.execute("UPDATE eventos SET payload = ? WHERE seq = ?",
                    (payload.replace("485000", "185000"), seq))
    conexao.commit()
    print("  Removeu o gatilho e trocou R$ 4.850,00 por R$ 1.850,00 direto no arquivo")
    mostrar_relatorio(plataforma.verificar_compromisso(cp["id"]))


if __name__ == "__main__":
    main()
