"""Modo demonstração (ELO_DEMO=ligado): um agente simulado no servidor.

Só existe com o modo ligado; desligado, as rotas /demo/* nem são criadas.

O agente tem a própria chave, gerada a cada início e guardada só na memória. A
transportadora precisa, no painel, assinar o certificado e um mandato para ele
("Preparar demonstração"): o servidor nunca assina pela organização.

"Simular pedido de cotação" registra o pedido de um embarcador fictício e faz o
agente emitir três ofertas de lotação (410 km, 5 eixos), calculadas a partir do
mandato e da tabela da ANTT vigente:
- uma dentro do mandato (acima do piso e dentro do limite);
- uma acima do limite do mandato;
- uma abaixo do piso mínimo da ANTT.

Os valores (e o peso da carga) mudam a cada pedido, sempre dentro dessas três
faixas, para a demonstração não repetir os mesmos números.

"Limpar demonstração" descarta as ofertas do agente simulado que ainda estão em
aberto. O registro só aceita acréscimos: nada é apagado; entra um evento
"ofertas_descartadas" com a lista exata das ofertas, que saem das telas e não
podem mais ser aprovadas nem aceitas. O agente e o mandato ficam, para simular
de novo sem assinar outra vez.
"""

from __future__ import annotations

import random
from datetime import date, timedelta, timezone
from typing import Any

from .antt import piso_centavos, tabela_vigente
from .chaves import ParDeChaves
from .cnpj import calcular_digitos, formatar_cnpj
from .modelos import Carga, Condicoes, ler_momento
from .servico import ErroElo, Plataforma

NOME_AGENTE = "Agente de demonstração"
DISTANCIA_KM = 410   # São Paulo/SP → Curitiba/PR, aproximado
EIXOS = 5
BRT = timezone(timedelta(hours=-3))
CEP_POR_UF = {"SP": "01001-000", "PR": "80010-000", "SC": "88010-000", "RJ": "20010-000",
              "MG": "30110-000", "RS": "90010-000"}


def _cnpj(base12: str) -> str:
    return formatar_cnpj(base12 + calcular_digitos(base12))


EMBARCADORES = [(_cnpj("223334440001"), "Comercial Exemplo de Alimentos Ltda (fictícia)"),
                (_cnpj("334445550001"), "Distribuidora Exemplo S.A. (fictícia)")]
LIMITES_PADRAO = {"valor_max_centavos": 600_000, "ufs_origem": ["SP"], "ufs_destino": ["PR"],
                  "tipos_carga": ["carga-geral"]}


def _arredondar(centavos: int, para: int = 5_000) -> int:
    """Para cima, em múltiplos de R$ 50, para valores de demonstração redondos."""
    return -(-centavos // para) * para


# faixas, em % (do piso ou do limite do mandato), de onde saem os valores de cada pedido
FAIXA_DENTRO = (105, 140)   # do piso, sem passar do limite
FAIXA_ACIMA = (115, 190)    # do limite do mandato
FAIXA_ABAIXO = (35, 80)     # do piso


class AgenteDemo:
    def __init__(self, sorteio: random.Random | None = None) -> None:
        self.chave = ParDeChaves.gerar("agente-demonstracao#k1")
        self.sorteio = sorteio or random.Random()

    def _agente(self, p: Plataforma, cnpj: str) -> dict | None:
        return next((a for a in p.agentes.values()
                     if a["cnpj"] == cnpj and a["chave_publica"] == self.chave.publica_b64), None)

    def limpar(self, p: Plataforma, cnpj: str) -> dict[str, Any]:
        """Descarta as ofertas em aberto do agente simulado (nada é apagado do registro)."""
        cnpj = p._org(cnpj)["cnpj"]
        agente = self._agente(p, cnpj)
        if agente is None:
            return {"descartadas": 0}
        ids = p.descartar_ofertas_do_agente(cnpj, agente["agente_id"],
                                            "limpeza do modo demonstração")
        return {"descartadas": len(ids)}

    def descricao(self) -> dict[str, Any]:
        return {"nome": NOME_AGENTE, "chave_id": self.chave.chave_id,
                "chave_publica": self.chave.publica_b64, "limites_padrao": LIMITES_PADRAO}

    def _agente_e_mandato(self, p: Plataforma, cnpj: str) -> tuple[dict, dict]:
        agente = self._agente(p, cnpj)
        if agente is None:
            raise ErroElo("prepare a demonstração antes: cadastre o agente de demonstração")
        agora = p._agora()
        validos = [m for m in p.mandatos.values()
                   if m["agente_id"] == agente["agente_id"] and m["id"] not in p.revogacoes
                   and ler_momento(m["limites"]["valido_de"]) <= agora
                   <= ler_momento(m["limites"]["valido_ate"])]
        if not validos:
            raise ErroElo("prepare a demonstração antes: o agente de demonstração não tem "
                          "mandato válido")
        return agente, max(validos, key=lambda m: m["emitido_em"])

    def simular(self, p: Plataforma, cnpj: str) -> dict[str, Any]:
        cnpj = p._org(cnpj)["cnpj"]
        agente, mandato = self._agente_e_mandato(p, cnpj)
        limites = mandato["limites"]
        tabela = tabela_vigente(p._agora())
        tipo = limites["tipos_carga"][0]
        piso = piso_centavos(tipo, EIXOS, DISTANCIA_KM, tabela) if tabela else None
        if piso is None:
            raise ErroElo("sem piso da ANTT para a categoria do mandato: escolha carga geral")
        limite = limites["valor_max_centavos"]
        if limite < piso:
            raise ErroElo("o limite do mandato está abaixo do piso da ANTT para "
                          f"{DISTANCIA_KM} km: aumente o limite para simular")
        sortear = lambda base, faixa: _arredondar(base * self.sorteio.randint(*faixa) // 100)
        # dentro: entre o piso e o limite (arredondar para baixo no limite mantém >= piso)
        dentro = max(piso, min(limite, sortear(piso, FAIXA_DENTRO)))
        abaixo = min(sortear(piso, FAIXA_ABAIXO), (piso - 1) // 5_000 * 5_000)
        valores = [("dentro do mandato", dentro),
                   ("acima do limite", sortear(limite, FAIXA_ACIMA)),
                   ("abaixo do piso da ANTT", abaixo)]

        cnpj_embarcador, nome = next((c, n) for c, n in EMBARCADORES if c != cnpj)
        origem, destino = limites["ufs_origem"][0], limites["ufs_destino"][0]
        carga = Carga(origem_cep=CEP_POR_UF.get(origem, "01001-000"), origem_uf=origem,
                      destino_cep=CEP_POR_UF.get(destino, "80010-000"), destino_uf=destino,
                      peso_kg=self.sorteio.randrange(1_800, 9_001, 100),
                      volume_litros=self.sorteio.randrange(8_000, 30_001, 500), tipo=tipo)
        pedido = p.registrar_pedido(cnpj_embarcador, "demonstracao", carga,
                                    "Pedido simulado no modo demonstração", nome)
        valida_ate = (p._agora() + timedelta(days=1)).astimezone(BRT).isoformat(timespec="seconds")
        coleta = (p._agora().astimezone(BRT).date() + timedelta(days=2)).isoformat()
        ofertas = []
        for rotulo, valor in valores:
            condicoes = Condicoes(valor_centavos=valor, coleta_ate=date.fromisoformat(coleta),
                                  prazo_entrega_dias_uteis=2, inclui=["pedágio"],
                                  modalidade="lotacao", distancia_km=DISTANCIA_KM, eixos=EIXOS)
            oferta, problemas, token = p.emitir_oferta(pedido["id"], agente["agente_id"],
                                                       mandato["id"], self.chave, condicoes,
                                                       valida_ate)
            ofertas.append({"rotulo": rotulo, "oferta_id": oferta["id"], "valor_centavos": valor,
                            "status": p.status_oferta[oferta["id"]], "violacoes": problemas,
                            "token_aceite": token})
        return {"pedido_id": pedido["id"], "embarcador": {"cnpj": cnpj_embarcador, "nome": nome},
                "piso_centavos": piso, "ofertas": ofertas}
