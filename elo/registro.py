"""Registro de eventos só de inclusão, com hashes encadeados (SQLite).

Cada evento guarda o hash do anterior. Alterar ou apagar um evento antigo
quebra a corrente a partir dali, e verificar_integridade() acusa.

Duas camadas de proteção:
1. gatilhos no banco recusam UPDATE e DELETE vindos da aplicação;
2. mesmo que alguém com acesso direto ao arquivo remova os gatilhos e
   altere dados, a corrente de hashes denuncia a adulteração.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator

from .canonico import canonicalizar, hash_sha256

HASH_INICIAL = "sha256:" + "0" * 64

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS eventos (
    seq           INTEGER PRIMARY KEY,
    tipo          TEXT NOT NULL,
    objeto_id     TEXT,
    payload       TEXT NOT NULL,
    criado_em     TEXT NOT NULL,
    hash_anterior TEXT NOT NULL,
    hash          TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_eventos_objeto ON eventos (objeto_id);
CREATE TRIGGER IF NOT EXISTS eventos_sem_update
    BEFORE UPDATE ON eventos
    BEGIN SELECT RAISE(ABORT, 'registro é só de inclusão: UPDATE proibido'); END;
CREATE TRIGGER IF NOT EXISTS eventos_sem_delete
    BEFORE DELETE ON eventos
    BEGIN SELECT RAISE(ABORT, 'registro é só de inclusão: DELETE proibido'); END;
"""


@dataclass(frozen=True)
class Evento:
    seq: int
    tipo: str
    objeto_id: str | None
    payload: dict[str, Any]
    criado_em: str
    hash_anterior: str
    hash: str


@dataclass(frozen=True)
class ResultadoIntegridade:
    integro: bool
    total_eventos: int
    primeiro_evento_com_problema: int | None = None
    motivo: str | None = None


def calcular_hash(seq: int, tipo: str, objeto_id: str | None, payload: dict[str, Any],
                  criado_em: str, hash_anterior: str) -> str:
    return hash_sha256({
        "seq": seq,
        "tipo": tipo,
        "objeto_id": objeto_id,
        "payload": payload,
        "criado_em": criado_em,
        "hash_anterior": hash_anterior,
    })


class Registro:
    def __init__(self, caminho: str = ":memory:") -> None:
        # check_same_thread=False: a API usa o registro a partir de várias threads,
        # sempre sob uma trava (ver elo/api.py)
        self.conexao = sqlite3.connect(caminho, check_same_thread=False)
        self.conexao.executescript(_ESQUEMA)

    def ultimo_hash(self) -> tuple[int, str]:
        linha = self.conexao.execute(
            "SELECT seq, hash FROM eventos ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return (0, HASH_INICIAL) if linha is None else (linha[0], linha[1])

    def incluir(self, tipo: str, payload: dict[str, Any], criado_em: str,
                objeto_id: str | None = None) -> Evento:
        # payload_texto canônico: os bytes gravados são exatamente os que entram no hash
        payload_texto = canonicalizar(payload).decode("utf-8")
        with self.conexao:  # transação: lê o último hash e grava atomicamente
            seq_anterior, hash_anterior = self.ultimo_hash()
            seq = seq_anterior + 1
            h = calcular_hash(seq, tipo, objeto_id, payload, criado_em, hash_anterior)
            self.conexao.execute(
                "INSERT INTO eventos (seq, tipo, objeto_id, payload, criado_em, "
                "hash_anterior, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (seq, tipo, objeto_id, payload_texto, criado_em, hash_anterior, h),
            )
        return Evento(seq, tipo, objeto_id, payload, criado_em, hash_anterior, h)

    def eventos(self) -> Iterator[Evento]:
        cursor = self.conexao.execute(
            "SELECT seq, tipo, objeto_id, payload, criado_em, hash_anterior, hash "
            "FROM eventos ORDER BY seq"
        )
        for seq, tipo, objeto_id, payload, criado_em, hash_anterior, h in cursor:
            yield Evento(seq, tipo, objeto_id, json.loads(payload), criado_em,
                         hash_anterior, h)

    def verificar_integridade(self) -> ResultadoIntegridade:
        esperado_anterior = HASH_INICIAL
        esperado_seq = 1
        total = 0
        for ev in self.eventos():
            total += 1
            if ev.seq != esperado_seq:
                return ResultadoIntegridade(False, total, ev.seq,
                                            f"sequência pulou: esperava {esperado_seq}")
            if ev.hash_anterior != esperado_anterior:
                return ResultadoIntegridade(False, total, ev.seq,
                                            "hash_anterior não bate com o evento anterior")
            recalculado = calcular_hash(ev.seq, ev.tipo, ev.objeto_id, ev.payload,
                                        ev.criado_em, ev.hash_anterior)
            if recalculado != ev.hash:
                return ResultadoIntegridade(False, total, ev.seq,
                                            "conteúdo alterado: hash não confere")
            esperado_anterior = ev.hash
            esperado_seq += 1
        return ResultadoIntegridade(True, total)
