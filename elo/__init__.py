"""Elo: camada de confiança entre agentes logísticos (núcleo da V1).

Módulos:
- canonico: serialização canônica de JSON (subconjunto da RFC 8785)
- chaves: geração de chaves, assinatura e verificação Ed25519
- cnpj: validação de CNPJ
- modelos: formatos das mensagens do protocolo (Pydantic)
- mandato: verificação de uma oferta contra um mandato
- registro: registro de eventos só de inclusão, com hashes encadeados
- servico: orquestra o fluxo de ponta a ponta
"""

VERSAO_PROTOCOLO = "0.2"
"""0.2: em carga lotação, a oferta precisa trazer distância e eixos (sem eles, o
piso da ANTT não pode ser conferido e a oferta vai para aprovação); aprovar
oferta abaixo do piso exige justificativa. Ofertas 0.1 seguem as regras de 0.1."""


def versao_ao_menos(versao: str | None, minima: str) -> bool:
    """'0.2' >= '0.1'? Versão ausente conta como '0.1'."""
    def partes(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split("."))
    try:
        return partes(versao or "0.1") >= partes(minima)
    except ValueError:
        return False
