"""Login do painel por desafio assinado com a chave da organização.

Não há senha: quem tem a chave privada da organização entra.

1. ``criar_desafio(cnpj)`` devolve um objeto com um nonce aleatório;
2. o navegador assina o desafio com a chave da organização (que nunca sai dele);
3. ``abrir(assinado)`` confere a assinatura contra a chave pública cadastrada e
   devolve um token de sessão.

O desafio vale por poucos minutos e uma vez só: qualquer tentativa de uso, certa
ou errada, o consome. A sessão é curta e só o hash do token fica guardado.

Sessões não são atos do protocolo: ficam só na memória, fora do registro, e
somem quando a API reinicia (basta entrar de novo).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .canonico import ErroCanonico, hash_sha256
from .chaves import verificar_assinatura
from .servico import ErroElo, Plataforma, hash_token

VALIDADE_DESAFIO = timedelta(minutes=5)
VALIDADE_SESSAO = timedelta(minutes=30)
MAX_DESAFIOS_POR_CNPJ = 5
"""Teto de desafios pendentes por organização. Passou do teto, o mais antigo é
descartado (em vez de recusar o novo): assim quem pede desafios em massa não
consegue impedir a organização de entrar, e a memória fica limitada a
organizações cadastradas × este número."""


class NaoAutenticado(Exception):
    """Sem sessão válida (vira 401 na API)."""


@dataclass
class _Pendente:
    cnpj: str
    expira_em: datetime


class Sessoes:
    def __init__(self, plataforma: Callable[[], Plataforma]) -> None:
        # recebe uma função porque a API só abre a Plataforma ao iniciar
        self._plataforma = plataforma
        self._desafios: dict[str, _Pendente] = {}  # hash do desafio -> cnpj
        self._sessoes: dict[str, _Pendente] = {}   # hash do token -> cnpj

    def _agora(self) -> datetime:
        return self._plataforma().relogio()

    def _limpar(self) -> None:
        agora = self._agora()
        for tabela in (self._desafios, self._sessoes):
            for chave in [c for c, p in tabela.items() if p.expira_em < agora]:
                del tabela[chave]

    def criar_desafio(self, cnpj: str) -> dict[str, Any]:
        org = self._plataforma()._org(cnpj)  # NaoEncontrado se não cadastrada
        self._limpar()
        agora = self._agora()
        expira = agora + VALIDADE_DESAFIO
        desafio = {
            "tipo": "desafio_sessao",
            "finalidade": "entrar no painel do Elo",
            "cnpj": org["cnpj"],
            "nonce": secrets.token_urlsafe(32),
            "emitido_em": agora.isoformat(timespec="seconds"),
            "expira_em": expira.isoformat(timespec="seconds"),
        }
        do_cnpj = [c for c, p in self._desafios.items() if p.cnpj == org["cnpj"]]
        excesso = max(0, len(do_cnpj) - MAX_DESAFIOS_POR_CNPJ + 1)
        for antigo in do_cnpj[:excesso]:
            del self._desafios[antigo]  # dict mantém a ordem de inclusão
        self._desafios[hash_sha256(desafio)] = _Pendente(org["cnpj"], expira)
        return desafio

    def abrir(self, assinado: Any) -> dict[str, Any]:
        """Troca um desafio assinado por um token de sessão."""
        if not isinstance(assinado, dict):
            raise ErroElo("o desafio assinado precisa ser um objeto JSON")
        sem_assinatura = {k: v for k, v in assinado.items() if k != "assinatura"}
        try:
            chave = hash_sha256(sem_assinatura)
        except ErroCanonico as erro:
            raise ErroElo(str(erro)) from erro
        self._limpar()
        pendente = self._desafios.pop(chave, None)  # uso único, mesmo se falhar
        if pendente is None:
            raise NaoAutenticado("desafio desconhecido, já usado ou expirado: peça outro")
        org = self._plataforma()._org(pendente.cnpj)
        if not verificar_assinatura(assinado, org["chave_publica"]):
            raise NaoAutenticado("a assinatura não confere com a chave da organização")
        token = secrets.token_urlsafe(32)
        expira = self._agora() + VALIDADE_SESSAO
        self._sessoes[hash_token(token)] = _Pendente(pendente.cnpj, expira)
        return {"token": token, "cnpj": pendente.cnpj,
                "expira_em": expira.isoformat(timespec="seconds")}

    def cnpj_da_sessao(self, token: str | None) -> str:
        if not token:
            raise NaoAutenticado("entre no painel para ver estes dados")
        self._limpar()
        # busca pelo hash: o tempo da busca não revela nada do token em si
        sessao = self._sessoes.get(hash_token(token))
        if sessao is None:
            raise NaoAutenticado("sessão inválida ou expirada: entre de novo")
        return sessao.cnpj

    def encerrar(self, token: str) -> None:
        self._sessoes.pop(hash_token(token), None)
