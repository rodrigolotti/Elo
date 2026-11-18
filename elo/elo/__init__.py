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

VERSAO_PROTOCOLO = "0.1"
