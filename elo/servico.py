"""Orquestra o fluxo da V1: organização → agente → mandato → pedido → oferta →
(aprovação humana, se preciso) → aceite do embarcador → verificação.

O estado é sempre reconstruído a partir do registro (o registro é a fonte da
verdade). As chaves privadas das empresas NÃO ficam aqui.

Toda operação que precisa da assinatura de uma empresa tem dois passos:

1. ``preparar_*`` aplica as regras e devolve o objeto ainda sem assinatura
   (o "rascunho"), que fica pendente por alguns minutos;
2. quem tem a chave assina o rascunho do seu lado e o devolve em
   ``registrar_assinado``, que confere a assinatura contra a chave pública
   esperada, revalida o estado e grava no registro.

Assim a API HTTP nunca recebe chave privada. Os métodos de um passo só
(``cadastrar_organizacao``, ``emitir_mandato`` etc.) fazem os dois passos em
sequência, para uso local, nos testes e na demo. A plataforma só guarda a
chave dela, usada para atestar aceites recebidos por link.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import VERSAO_PROTOCOLO, versao_ao_menos
from .canonico import ErroCanonico, hash_sha256
from .chaves import ParDeChaves, chave_publica_valida, verificar_assinatura
from .cnpj import formatar_cnpj, validar_cnpj
from .antt import TIPOS_CARGA, nome_tipo_carga, normalizar_tipo_carga


def _descrever_tipo(tipo: str) -> dict[str, Any]:
    """Para exibir: nome da categoria da ANTT, ou o texto antigo fora da lista."""
    return {"tipo": tipo, "nome": nome_tipo_carga(tipo),
            "na_lista": normalizar_tipo_carga(tipo) is not None}
from .mandato import abaixo_do_piso, violacoes

MINIMO_JUSTIFICATIVA = 10
"""Caracteres mínimos da justificativa para aprovar frete abaixo do piso."""
from .modelos import Carga, Condicoes, LimitesMandato, ler_momento
from .registro import Registro


class ErroElo(Exception):
    """Operação recusada por regra do protocolo."""


def _exigir_categoria(tipo: str) -> str:
    """Categoria da Tabela A da ANTT para o tipo informado, ou erro com a lista."""
    categoria = normalizar_tipo_carga(tipo)
    if categoria is None:
        raise ErroElo(f"tipo de carga fora da lista da ANTT: {tipo!r}. Use um de: "
                      + ", ".join(TIPOS_CARGA))
    return categoria


class NaoEncontrado(ErroElo):
    """O objeto pedido (organização, agente, oferta...) não existe."""


VALIDADE_RASCUNHO = timedelta(minutes=10)
"""Por quanto tempo um rascunho espera a assinatura antes de ser descartado."""

CHECAGENS = (
    "Registro íntegro",
    "Compromisso existe",
    "Aceite atestado pela plataforma",
    "Oferta aceita encontrada pelo hash",
    "Agente emissor conhecido",
    "Oferta assinada pelo agente",
    "Organização do agente cadastrada",
    "Cadastro da organização autoassinado",
    "Histórico de chaves da organização íntegro",
    "Certificado do agente assinado pela organização",
    "Oferta emitida pela mesma organização do agente",
    "Mandato assinado pela organização para este agente",
    "Oferta autorizada",
    "Aceite feito pelo destinatário",
    "Aceite dentro da validade da oferta",
)
"""As 15 checagens do relatório de verificação, na ordem em que são feitas."""


def _novo_id(prefixo: str) -> str:
    return f"{prefixo}_{uuid.uuid4().hex[:12]}"


def _agora_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Verificacao:
    nome: str
    ok: bool
    detalhe: str = ""


@dataclass
class Relatorio:
    compromisso_id: str
    verificacoes: list[Verificacao] = field(default_factory=list)

    @property
    def valido(self) -> bool:
        return bool(self.verificacoes) and all(v.ok for v in self.verificacoes)

    def checar(self, nome: str, ok: bool, detalhe: str = "") -> bool:
        self.verificacoes.append(Verificacao(nome, ok, detalhe))
        return ok

    def completar(self) -> None:
        """Garante todas as checagens: as que não puderam ser feitas contam como falha."""
        feitas = {v.nome for v in self.verificacoes}
        for nome in CHECAGENS:
            if nome not in feitas:
                self.verificacoes.append(Verificacao(
                    nome, False, "não verificada: uma checagem anterior falhou"))
        ordem = {nome: i for i, nome in enumerate(CHECAGENS)}
        self.verificacoes.sort(key=lambda v: ordem.get(v.nome, len(ordem)))


@dataclass
class ChaveOrg:
    """Uma chave principal da organização e a faixa do registro em que valeu."""

    chave_id: str
    chave_publica: str
    desde_seq: int
    ate_seq: int | None = None  # None = vigente
    desde_em: str | None = None  # quando passou a valer (ISO 8601), para exibir

    def valia_em(self, seq: int) -> bool:
        return self.desde_seq <= seq and (self.ate_seq is None or seq < self.ate_seq)


@dataclass
class EventoChave:
    """Definição de chave de recuperação ou rotação, com o resultado da conferência."""

    seq: int
    tipo: str
    valido: bool
    motivo: str = ""
    em: str | None = None


@dataclass
class _Rascunho:
    """Objeto preparado pela plataforma, esperando a assinatura de quem tem a chave."""

    tipo: str
    evento: str
    objeto: dict[str, Any]
    objeto_id: str
    chave_publica: str
    expira_em: datetime
    revalidar: Callable[[], None]
    # recebe o objeto assinado e devolve (payload gravado no registro,
    # extras que vão só na resposta e nunca para o registro)
    montar_payload: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]]


def hash_token(token: str) -> str:
    """Hash do token de aceite. O token tem 256 bits aleatórios, então SHA-256 basta."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


class Plataforma:
    def __init__(self, registro: Registro, chave_plataforma: ParDeChaves,
                 relogio: Callable[[], datetime] = _agora_utc) -> None:
        self.registro = registro
        self.chave = chave_plataforma
        self.relogio = relogio
        self._rascunhos: dict[str, _Rascunho] = {}
        self._reconstruir()
        if self.chave.chave_id not in self.chaves_plataforma:
            self._incluir("plataforma_chave", {
                "tipo": "chave_plataforma",
                "chave_id": self.chave.chave_id,
                "chave_publica": self.chave.publica_b64,
            }, self.chave.chave_id)

    # ---------- estado reconstruído a partir do registro ----------

    def _reconstruir(self) -> None:
        self.chaves_plataforma: dict[str, str] = {}
        self.organizacoes: dict[str, dict[str, Any]] = {}
        self.agentes: dict[str, dict[str, Any]] = {}
        self.mandatos: dict[str, dict[str, Any]] = {}
        self.revogacoes: dict[str, dict[str, Any]] = {}
        self.pedidos: dict[str, dict[str, Any]] = {}
        self.ofertas: dict[str, dict[str, Any]] = {}
        self.ofertas_por_hash: dict[str, str] = {}
        self.status_oferta: dict[str, str] = {}
        self.violacoes_oferta: dict[str, list[str]] = {}
        self.aprovacoes: dict[str, dict[str, Any]] = {}
        self.recusas: dict[str, dict[str, Any]] = {}
        self.compromissos: dict[str, dict[str, Any]] = {}
        self.token_aceite_hash: dict[str, str | None] = {}
        # o mandato já estava revogado quando a oferta entrou no registro?
        # (ordem do registro, protegida pela corrente de hashes; os horários
        # têm resolução de segundos e não bastam para desempatar)
        self.revogado_na_emissao: dict[str, bool] = {}
        # chaves da organização ao longo do tempo (pela ordem do registro)
        self.chaves_org: dict[str, list[ChaveOrg]] = {}
        self.recuperacao_org: dict[str, dict[str, Any]] = {}
        self.eventos_chave: dict[str, list[EventoChave]] = {}
        # em que posição do registro entrou cada objeto assinado pela organização:
        # a assinatura é conferida contra a chave que valia ali
        self.seq_objeto: dict[tuple[str, str], int] = {}
        for ev in self.registro.eventos():
            self._aplicar(ev.tipo, ev.payload, ev.seq)

    def _aplicar(self, tipo: str, p: dict[str, Any], seq: int) -> None:
        if tipo == "plataforma_chave":
            self.chaves_plataforma[p["chave_id"]] = p["chave_publica"]
        elif tipo == "organizacao_cadastrada":
            self.organizacoes[p["cnpj"]] = p
            self.chaves_org[p["cnpj"]] = [ChaveOrg(p["chave_id"], p["chave_publica"], seq,
                                                   desde_em=p.get("cadastrada_em"))]
            self.eventos_chave[p["cnpj"]] = []
        elif tipo in ("chave_recuperacao_definida", "chave_rotacionada"):
            self._aplicar_evento_de_chave(tipo, p, seq)
        elif tipo == "agente_emitido":
            self.agentes[p["agente_id"]] = p
            self.seq_objeto[("agente", p["agente_id"])] = seq
        elif tipo == "mandato_emitido":
            self.mandatos[p["id"]] = p
            self.seq_objeto[("mandato", p["id"])] = seq
        elif tipo == "mandato_revogado":
            self.revogacoes[p["mandato_id"]] = p
        elif tipo == "pedido_recebido":
            self.pedidos[p["id"]] = p
        elif tipo == "oferta_emitida":
            oferta = p["oferta"]
            self.ofertas[oferta["id"]] = oferta
            self.ofertas_por_hash[hash_sha256(oferta)] = oferta["id"]
            self.status_oferta[oferta["id"]] = p["status"]
            self.violacoes_oferta[oferta["id"]] = list(p.get("violacoes", []))
            self.revogado_na_emissao[oferta["id"]] = oferta.get("mandato_id") in self.revogacoes
            self.token_aceite_hash[oferta["id"]] = p.get("token_aceite_hash")
        elif tipo == "oferta_aprovada":
            # .get: num registro adulterado o hash pode não achar a oferta
            oferta_id = self.ofertas_por_hash.get(p["oferta_hash"])
            if oferta_id is not None:
                self.aprovacoes[oferta_id] = p
                self.seq_objeto[("aprovacao", oferta_id)] = seq
                # uma aprovação gravada depois de uma recusa não reabre a oferta
                if oferta_id not in self.recusas:
                    self.status_oferta[oferta_id] = "enviada"
        elif tipo == "oferta_recusada":
            oferta_id = self.ofertas_por_hash.get(p["oferta_hash"])
            if oferta_id is not None:
                self.recusas[oferta_id] = p
                self.seq_objeto[("recusa", oferta_id)] = seq
                self.status_oferta[oferta_id] = "recusada"
        elif tipo == "compromisso_firmado":
            self.compromissos[p["id"]] = p
            oferta_id = self.ofertas_por_hash.get(p["oferta_hash"])
            if oferta_id is not None:
                self.status_oferta[oferta_id] = "aceita"

    def _aplicar_evento_de_chave(self, tipo: str, p: dict[str, Any], seq: int) -> None:
        """Aplica definição de recuperação ou rotação SÓ se a assinatura conferir.

        A plataforma nunca grava um evento inválido; se aparecer um (banco
        adulterado), ele não muda as chaves vigentes e fica no histórico como
        falha, com o motivo, para a checagem "Histórico de chaves" mostrar.
        """
        cnpj = p.get("cnpj") if isinstance(p, dict) else None
        if cnpj not in self.chaves_org:
            return  # organização desconhecida: nada a que se ligar
        atual = self.chaves_org[cnpj][-1]
        recuperacao = self.recuperacao_org.get(cnpj)
        nome = "definição de chave de recuperação" if tipo == "chave_recuperacao_definida" \
            else "rotação de chave"

        em = p.get("em") or p.get("definida_em")
        em = em if isinstance(em, str) else None

        def anotar(valido: bool, motivo: str = "") -> None:
            self.eventos_chave[cnpj].append(EventoChave(seq, nome, valido, motivo, em))
            return None

        try:
            if tipo == "chave_recuperacao_definida":
                if p["chave_principal_id"] != atual.chave_id:
                    return anotar(False, f"{nome} feita sobre outra chave principal")
                if not verificar_assinatura(p, atual.chave_publica):
                    return anotar(False, f"{nome}: assinatura não confere com a chave "
                                         f"principal vigente ({atual.chave_id})")
                anotar(True)
                self.recuperacao_org[cnpj] = {"chave_id": p["chave_id"],
                                              "chave_publica": p["chave_publica"], "seq": seq,
                                              "em": em}
                return
            # rotação: assinada pela chave de RECUPERAÇÃO vigente
            if recuperacao is None:
                return anotar(False, f"{nome} sem chave de recuperação definida")
            if p["chave_anterior"]["chave_publica"] != atual.chave_publica:
                return anotar(False, f"{nome} feita sobre outra chave principal")
            if p["recuperacao_usada"]["chave_publica"] != recuperacao["chave_publica"]:
                return anotar(False, f"{nome} com uma chave de recuperação que não é a "
                                     "vigente")
            if not verificar_assinatura(p, recuperacao["chave_publica"]):
                return anotar(False, f"{nome}: assinatura não confere com a chave de "
                                     f"recuperação vigente ({recuperacao['chave_id']})")
            anotar(True)
            atual.ate_seq = seq
            nova = p["chave_nova"]
            self.chaves_org[cnpj].append(ChaveOrg(nova["chave_id"], nova["chave_publica"], seq,
                                                  desde_em=em))
            rec = p["recuperacao_nova"]
            self.recuperacao_org[cnpj] = {"chave_id": rec["chave_id"],
                                          "chave_publica": rec["chave_publica"], "seq": seq,
                                          "em": em}
            # rascunhos à espera das chaves antigas não podem mais ser assinados
            antigas = {atual.chave_publica, recuperacao["chave_publica"]}
            for h in [h for h, r in self._rascunhos.items() if r.chave_publica in antigas]:
                del self._rascunhos[h]
        except (KeyError, TypeError):
            anotar(False, f"{nome} mal formada")

    def _incluir(self, tipo: str, payload: dict[str, Any], objeto_id: str) -> None:
        evento = self.registro.incluir(tipo, payload, self._agora_iso(), objeto_id)
        self._aplicar(tipo, payload, evento.seq)

    def _chave_atual(self, cnpj: str) -> ChaveOrg:
        return self.chaves_org[self._org(cnpj)["cnpj"]][-1]

    def _assinado_pela_org(self, objeto: dict[str, Any], cnpj: str,
                           tipo: str, objeto_id: str) -> bool:
        """A assinatura confere com a chave da organização que valia quando o
        objeto entrou no registro?"""
        seq = self.seq_objeto.get((tipo, objeto_id))
        chave = next((c for c in self.chaves_org.get(cnpj, []) if seq is not None
                      and c.valia_em(seq)), None)
        return chave is not None and verificar_assinatura(objeto, chave.chave_publica)

    def _agora(self) -> datetime:
        return self.relogio()

    def _agora_iso(self) -> str:
        return self._agora().isoformat(timespec="seconds")

    # ---------- rascunhos assinados do lado de quem tem a chave ----------

    def _guardar_rascunho(self, tipo: str, evento: str, objeto: dict[str, Any],
                          objeto_id: str, chave_publica: str,
                          revalidar: Callable[[], None] = lambda: None,
                          montar_payload: Callable[[dict[str, Any]],
                                                   tuple[dict[str, Any], dict[str, Any]]]
                          = lambda assinado: (assinado, {})) -> dict[str, Any]:
        agora = self._agora()
        for h in [h for h, r in self._rascunhos.items() if r.expira_em < agora]:
            del self._rascunhos[h]
        self._rascunhos[hash_sha256(objeto)] = _Rascunho(
            tipo, evento, objeto, objeto_id, chave_publica,
            agora + VALIDADE_RASCUNHO, revalidar, montar_payload)
        return dict(objeto)

    def registrar_assinado(self, assinado: dict[str, Any],
                           tipo: str | None = None) -> dict[str, Any]:
        """Recebe um rascunho assinado por quem tem a chave e grava no registro.

        Retorna o payload gravado (para ofertas: {"oferta", "status", "violacoes",
        "token_aceite_hash"}, mais "token_aceite", que só aparece aqui; para os
        demais: o próprio objeto assinado).
        """
        if not isinstance(assinado, dict):
            raise ErroElo("o objeto assinado precisa ser um objeto JSON")
        sem_assinatura = {k: v for k, v in assinado.items() if k != "assinatura"}
        try:
            chave = hash_sha256(sem_assinatura)
        except ErroCanonico as erro:
            raise ErroElo(str(erro)) from erro
        rascunho = self._rascunhos.get(chave)
        if rascunho is None or rascunho.expira_em < self._agora():
            self._rascunhos.pop(chave, None)
            raise ErroElo("rascunho desconhecido, alterado ou expirado: prepare de novo")
        if tipo is not None and rascunho.tipo != tipo:
            raise ErroElo(f"esperava um rascunho de {tipo}, veio um de {rascunho.tipo}")
        assinatura = assinado.get("assinatura")
        if not (isinstance(assinatura, dict)
                and set(assinatura) == {"alg", "chave_id", "valor"}
                and all(isinstance(v, str) for v in assinatura.values())):
            raise ErroElo("assinatura ausente ou mal formada")
        if not verificar_assinatura(assinado, rascunho.chave_publica):
            raise ErroElo("a assinatura não confere com a chave pública esperada")
        del self._rascunhos[chave]
        rascunho.revalidar()  # o estado pode ter mudado desde o rascunho
        objeto = {**rascunho.objeto, "assinatura": assinatura}
        payload, extras = rascunho.montar_payload(objeto)
        self._incluir(rascunho.evento, payload, rascunho.objeto_id)
        return {**payload, **extras}

    # ---------- organizações e agentes ----------

    def preparar_organizacao(self, cnpj: str, razao_social: str, chave_id: str,
                             chave_publica: str) -> dict[str, Any]:
        if not validar_cnpj(cnpj):
            raise ErroElo(f"CNPJ inválido: {cnpj}")
        if not chave_publica_valida(chave_publica):
            raise ErroElo("chave pública Ed25519 inválida")
        cnpj_fmt = formatar_cnpj(cnpj)

        def revalidar() -> None:
            if cnpj_fmt in self.organizacoes:
                raise ErroElo(f"organização já cadastrada: {cnpj_fmt}")

        revalidar()
        # autoassinado: prova que quem cadastra tem a chave privada da organização
        org = {
            "tipo": "organizacao",
            "versao_protocolo": VERSAO_PROTOCOLO,
            "cnpj": cnpj_fmt,
            "razao_social": razao_social,
            "chave_id": chave_id,
            "chave_publica": chave_publica,
            "cadastrada_em": self._agora_iso(),
            "cnpj_conferido_na_receita": False,  # V1: conferência manual
        }
        return self._guardar_rascunho("organizacao", "organizacao_cadastrada", org,
                                      cnpj_fmt, chave_publica, revalidar)

    def cadastrar_organizacao(self, cnpj: str, razao_social: str,
                              chave_org: ParDeChaves) -> dict[str, Any]:
        rascunho = self.preparar_organizacao(cnpj, razao_social, chave_org.chave_id,
                                             chave_org.publica_b64)
        return self.registrar_assinado(chave_org.assinar(rascunho), "organizacao")

    def preparar_agente(self, cnpj: str, nome: str, chave_id: str,
                        chave_publica: str) -> dict[str, Any]:
        """O certificado leva só a chave PÚBLICA do agente e é assinado pela organização."""
        org = self._org(cnpj)
        if not chave_publica_valida(chave_publica):
            raise ErroElo("chave pública Ed25519 do agente inválida")
        certificado = {
            "tipo": "certificado_agente",
            "versao_protocolo": VERSAO_PROTOCOLO,
            "agente_id": _novo_id("ag"),
            "nome": nome,
            "cnpj": org["cnpj"],
            "chave_id": chave_id,
            "chave_publica": chave_publica,
            "emitido_em": self._agora_iso(),
        }
        return self._guardar_rascunho("certificado_agente", "agente_emitido",
                                      certificado, certificado["agente_id"],
                                      self._chave_atual(cnpj).chave_publica)

    def emitir_agente(self, cnpj: str, chave_org: ParDeChaves,
                      chave_agente: ParDeChaves, nome: str) -> dict[str, Any]:
        self._exigir_chave_da_org(self._org(cnpj), chave_org)
        rascunho = self.preparar_agente(cnpj, nome, chave_agente.chave_id,
                                        chave_agente.publica_b64)
        return self.registrar_assinado(chave_org.assinar(rascunho), "certificado_agente")

    def preparar_mandato(self, cnpj: str, agente_id: str,
                         limites: LimitesMandato) -> dict[str, Any]:
        org = self._org(cnpj)
        agente = self._agente(agente_id)
        if agente["cnpj"] != org["cnpj"]:
            raise ErroElo("o agente não pertence a esta organização")
        categorias = list(dict.fromkeys(_exigir_categoria(t) for t in limites.tipos_carga))
        limites = limites.model_copy(update={"tipos_carga": categorias})
        mandato = {
            "tipo": "mandato",
            "versao_protocolo": VERSAO_PROTOCOLO,
            "id": _novo_id("md"),
            "cnpj": org["cnpj"],
            "agente_id": agente_id,
            "limites": limites.model_dump(mode="json"),
            "emitido_em": self._agora_iso(),
        }
        return self._guardar_rascunho("mandato", "mandato_emitido", mandato,
                                      mandato["id"], self._chave_atual(cnpj).chave_publica)

    def emitir_mandato(self, cnpj: str, chave_org: ParDeChaves, agente_id: str,
                       limites: LimitesMandato) -> dict[str, Any]:
        self._exigir_chave_da_org(self._org(cnpj), chave_org)
        rascunho = self.preparar_mandato(cnpj, agente_id, limites)
        return self.registrar_assinado(chave_org.assinar(rascunho), "mandato")

    def preparar_revogacao(self, mandato_id: str) -> dict[str, Any]:
        mandato = self._mandato(mandato_id)
        org = self._org(mandato["cnpj"])

        def revalidar() -> None:
            if mandato_id in self.revogacoes:
                raise ErroElo("mandato já revogado")

        revalidar()
        revogacao = {"tipo": "revogacao", "mandato_id": mandato_id,
                     "em": self._agora_iso()}
        return self._guardar_rascunho("revogacao", "mandato_revogado", revogacao,
                                      mandato_id, self._chave_atual(org["cnpj"]).chave_publica,
                                      revalidar)

    def revogar_mandato(self, mandato_id: str, chave_org: ParDeChaves) -> dict[str, Any]:
        self._exigir_chave_da_org(self._org(self._mandato(mandato_id)["cnpj"]), chave_org)
        rascunho = self.preparar_revogacao(mandato_id)
        return self.registrar_assinado(chave_org.assinar(rascunho), "revogacao")

    # ---------- chave de recuperação e rotação da chave principal ----------

    def preparar_recuperacao(self, cnpj: str, chave_id: str,
                             chave_publica: str) -> dict[str, Any]:
        """Define (ou troca) a chave de recuperação. Assinado pela chave principal."""
        org = self._org(cnpj)
        atual = self._chave_atual(cnpj)
        if not chave_publica_valida(chave_publica):
            raise ErroElo("chave pública Ed25519 de recuperação inválida")
        if chave_publica == atual.chave_publica:
            raise ErroElo("a chave de recuperação precisa ser diferente da chave principal")

        def revalidar() -> None:
            if self._chave_atual(cnpj).chave_publica != atual.chave_publica:
                raise ErroElo("a chave principal mudou depois do rascunho: prepare de novo")

        definicao = {
            "tipo": "recuperacao_chave",
            "versao_protocolo": VERSAO_PROTOCOLO,
            "cnpj": org["cnpj"],
            "chave_principal_id": atual.chave_id,
            "chave_id": chave_id,
            "chave_publica": chave_publica,
            "definida_em": self._agora_iso(),
        }
        return self._guardar_rascunho("recuperacao_chave", "chave_recuperacao_definida",
                                      definicao, org["cnpj"], atual.chave_publica, revalidar)

    def definir_recuperacao(self, cnpj: str, chave_org: ParDeChaves,
                            chave_recuperacao: ParDeChaves) -> dict[str, Any]:
        self._exigir_chave_da_org(self._org(cnpj), chave_org)
        rascunho = self.preparar_recuperacao(cnpj, chave_recuperacao.chave_id,
                                             chave_recuperacao.publica_b64)
        return self.registrar_assinado(chave_org.assinar(rascunho), "recuperacao_chave")

    def preparar_rotacao(self, cnpj: str, chave_id: str, chave_publica: str,
                         recuperacao_chave_id: str,
                         recuperacao_chave_publica: str) -> dict[str, Any]:
        """Troca a chave principal. Assinado pela chave de RECUPERAÇÃO vigente,
        que também é trocada: o arquivo de recuperação usado deixa de valer."""
        org = self._org(cnpj)
        atual = self._chave_atual(cnpj)
        recuperacao = self.recuperacao_org.get(org["cnpj"])
        if recuperacao is None:
            raise ErroElo("a organização não tem chave de recuperação: não há como trocar "
                          "a chave principal")
        for rotulo, chave in (("principal nova", chave_publica),
                              ("de recuperação nova", recuperacao_chave_publica)):
            if not chave_publica_valida(chave):
                raise ErroElo(f"chave {rotulo} inválida")
        usadas = {atual.chave_publica, recuperacao["chave_publica"]}
        if chave_publica in usadas or recuperacao_chave_publica in usadas \
                or chave_publica == recuperacao_chave_publica:
            raise ErroElo("a chave principal nova e a de recuperação nova precisam ser "
                          "novas e diferentes entre si")

        def revalidar() -> None:
            if (self._chave_atual(cnpj).chave_publica != atual.chave_publica
                    or self.recuperacao_org.get(org["cnpj"]) != recuperacao):
                raise ErroElo("as chaves da organização mudaram depois do rascunho: "
                              "prepare de novo")

        rotacao = {
            "tipo": "rotacao_chave",
            "versao_protocolo": VERSAO_PROTOCOLO,
            "cnpj": org["cnpj"],
            "chave_anterior": {"chave_id": atual.chave_id,
                               "chave_publica": atual.chave_publica},
            "chave_nova": {"chave_id": chave_id, "chave_publica": chave_publica},
            "recuperacao_usada": {"chave_id": recuperacao["chave_id"],
                                  "chave_publica": recuperacao["chave_publica"]},
            "recuperacao_nova": {"chave_id": recuperacao_chave_id,
                                 "chave_publica": recuperacao_chave_publica},
            "em": self._agora_iso(),
        }
        return self._guardar_rascunho("rotacao_chave", "chave_rotacionada", rotacao,
                                      org["cnpj"], recuperacao["chave_publica"], revalidar)

    def rotacionar_chave(self, cnpj: str, chave_recuperacao: ParDeChaves,
                         nova_chave: ParDeChaves,
                         nova_recuperacao: ParDeChaves) -> dict[str, Any]:
        rascunho = self.preparar_rotacao(cnpj, nova_chave.chave_id, nova_chave.publica_b64,
                                         nova_recuperacao.chave_id,
                                         nova_recuperacao.publica_b64)
        return self.registrar_assinado(chave_recuperacao.assinar(rascunho), "rotacao_chave")

    def resumo_organizacao(self, cnpj: str) -> dict[str, Any]:
        """Cadastro, chave vigente, chave de recuperação e histórico de chaves."""
        org = self._org(cnpj)
        atual = self._chave_atual(cnpj)
        recuperacao = self.recuperacao_org.get(org["cnpj"])
        return {
            "cadastro": org,
            "chave_atual": {"chave_id": atual.chave_id, "chave_publica": atual.chave_publica,
                            "desde": atual.desde_em},
            "recuperacao": None if recuperacao is None else {
                "chave_id": recuperacao["chave_id"],
                "chave_publica": recuperacao["chave_publica"],
                "definida_em": recuperacao["em"]},
            "historico_chaves": [{"seq": e.seq, "evento": e.tipo, "valido": e.valido,
                                  "motivo": e.motivo, "em": e.em}
                                 for e in self.eventos_chave.get(org["cnpj"], [])],
        }

    # ---------- pedido e oferta ----------

    def registrar_pedido(self, cnpj_solicitante: str, canal: str, carga: Carga,
                         mensagem_original: str = "",
                         nome_solicitante: str = "") -> dict[str, Any]:
        """Registra o pedido de cotação. O nome do embarcador é o informado no
        pedido (o CNPJ não é consultado na Receita)."""
        if not validar_cnpj(cnpj_solicitante):
            raise ErroElo(f"CNPJ do solicitante inválido: {cnpj_solicitante}")
        carga = carga.model_copy(update={"tipo": _exigir_categoria(carga.tipo)})
        pedido = {
            "tipo": "pedido",
            "id": _novo_id("pc"),
            "cnpj_solicitante": formatar_cnpj(cnpj_solicitante),
            "nome_solicitante": nome_solicitante.strip(),
            "canal": canal,
            "carga": carga.model_dump(mode="json"),
            "mensagem_original": mensagem_original,
            "recebido_em": self._agora_iso(),
        }
        self._incluir("pedido_recebido", pedido, pedido["id"])
        return pedido

    def preparar_oferta(self, pedido_id: str, agente_id: str, mandato_id: str,
                        condicoes: Condicoes,
                        valida_ate: str) -> tuple[dict[str, Any], list[str]]:
        """Monta a oferta para o agente assinar e diz se ela sai do mandato.

        Retorna (rascunho, lista_de_violacoes). Lista vazia = será enviada direto;
        senão, ao ser registrada fica aguardando aprovação humana.
        """
        pedido = self._pedido(pedido_id)
        agente = self._agente(agente_id)
        mandato = self._mandato(mandato_id)
        if mandato["agente_id"] != agente_id:
            raise ErroElo("o mandato não foi emitido para este agente")
        if pedido["cnpj_solicitante"] == agente["cnpj"]:
            raise ErroElo("a oferta não pode ter como destinatário a própria transportadora "
                          f"que a emite ({agente['cnpj']})")
        agora = self._agora()
        try:
            vence = ler_momento(valida_ate)
        except ValueError as erro:
            raise ErroElo(f"valida_ate inválida: {erro}") from erro
        if vence <= agora:
            raise ErroElo("valida_ate precisa estar no futuro")

        carga = Carga(**pedido["carga"])
        problemas = violacoes(
            LimitesMandato(**mandato["limites"]), carga, condicoes, agora,
            self.revogacoes.get(mandato_id, {}).get("em"), VERSAO_PROTOCOLO,
        )
        oferta = {
            "tipo": "oferta",
            "versao_protocolo": VERSAO_PROTOCOLO,
            "id": _novo_id("of"),
            "emissor": {"cnpj": agente["cnpj"], "agente_id": agente_id},
            "mandato_id": mandato_id,
            "referencia": {"pedido_id": pedido_id, "oferta_anterior_hash": None},
            "destinatario_cnpj": pedido["cnpj_solicitante"],
            "carga": pedido["carga"],
            # campos opcionais vazios ficam de fora: oferta sem modalidade fica
            # igual ao formato de antes
            "condicoes": condicoes.model_dump(mode="json", exclude_none=True),
            "emitida_em": agora.isoformat(timespec="seconds"),
            "valida_ate": valida_ate,
        }
        status = "aguardando_aprovacao" if problemas else "enviada"

        def revalidar() -> None:
            if not problemas and mandato_id in self.revogacoes:
                raise ErroElo("o mandato foi revogado depois do rascunho: prepare de novo")

        def montar_payload(assinada: dict[str, Any]) -> tuple[dict, dict]:
            # token de aceite de uso único: o registro guarda só o hash; o token
            # puro vai uma única vez na resposta, para a transportadora mandar
            # ao embarcador junto com o link
            token = secrets.token_urlsafe(32)
            return ({"oferta": assinada, "status": status, "violacoes": list(problemas),
                     "token_aceite_hash": hash_token(token)},
                    {"token_aceite": token})

        rascunho = self._guardar_rascunho(
            "oferta", "oferta_emitida", oferta, oferta["id"], agente["chave_publica"],
            revalidar, montar_payload,
        )
        return rascunho, problemas

    def emitir_oferta(self, pedido_id: str, agente_id: str, mandato_id: str,
                      chave_agente: ParDeChaves, condicoes: Condicoes,
                      valida_ate: str) -> tuple[dict[str, Any], list[str], str]:
        """Assina a oferta. Se sair do mandato, fica aguardando aprovação humana.

        Retorna (oferta, lista_de_violacoes, token_aceite). Lista vazia = oferta
        enviada. O token é exigido no aceite e não é guardado em lugar nenhum.
        """
        if chave_agente.publica_b64 != self._agente(agente_id)["chave_publica"]:
            raise ErroElo("a chave informada não é a chave deste agente")
        rascunho, _ = self.preparar_oferta(pedido_id, agente_id, mandato_id,
                                           condicoes, valida_ate)
        payload = self.registrar_assinado(chave_agente.assinar(rascunho), "oferta")
        return payload["oferta"], payload["violacoes"], payload["token_aceite"]

    def preparar_aprovacao(self, oferta_id: str, aprovado_por: str,
                           justificativa: str = "") -> dict[str, Any]:
        """Aprovação humana. Oferta abaixo do piso da ANTT exige justificativa
        escrita, que vai assinada junto com a aprovação."""
        oferta = self._oferta(oferta_id)
        org = self._org(oferta["emissor"]["cnpj"])
        justificativa = justificativa.strip()
        if (abaixo_do_piso(self.violacoes_oferta.get(oferta_id, []))
                and len(justificativa) < MINIMO_JUSTIFICATIVA):
            raise ErroElo("frete abaixo do piso mínimo da ANTT: para aprovar, escreva a "
                          f"justificativa (pelo menos {MINIMO_JUSTIFICATIVA} caracteres)")

        def revalidar() -> None:
            if self.status_oferta[oferta_id] != "aguardando_aprovacao":
                raise ErroElo(f"oferta não está aguardando aprovação "
                              f"({self.status_oferta[oferta_id]})")

        revalidar()
        aprovacao = {
            "tipo": "aprovacao",
            "oferta_hash": hash_sha256(oferta),
            "aprovado_por": aprovado_por,
            "em": self._agora_iso(),
            # só quando há: aprovação sem justificativa fica no formato de antes
            **({"justificativa": justificativa} if justificativa else {}),
        }
        return self._guardar_rascunho("aprovacao", "oferta_aprovada", aprovacao,
                                      oferta_id, self._chave_atual(org["cnpj"]).chave_publica,
                                      revalidar)

    def aprovar_oferta(self, oferta_id: str, chave_org: ParDeChaves,
                       aprovado_por: str, justificativa: str = "") -> dict[str, Any]:
        oferta = self._oferta(oferta_id)
        self._exigir_chave_da_org(self._org(oferta["emissor"]["cnpj"]), chave_org)
        rascunho = self.preparar_aprovacao(oferta_id, aprovado_por, justificativa)
        return self.registrar_assinado(chave_org.assinar(rascunho), "aprovacao")

    def preparar_recusa(self, oferta_id: str, recusado_por: str,
                        motivo: str = "") -> dict[str, Any]:
        """A organização recusa uma oferta que aguardava aprovação. É definitivo."""
        oferta = self._oferta(oferta_id)
        org = self._org(oferta["emissor"]["cnpj"])

        def revalidar() -> None:
            if self.status_oferta[oferta_id] != "aguardando_aprovacao":
                raise ErroElo(f"oferta não está aguardando aprovação "
                              f"({self.status_oferta[oferta_id]})")

        revalidar()
        recusa = {
            "tipo": "recusa",
            "oferta_hash": hash_sha256(oferta),
            "recusado_por": recusado_por,
            "motivo": motivo,
            "em": self._agora_iso(),
        }
        return self._guardar_rascunho("recusa", "oferta_recusada", recusa,
                                      oferta_id, self._chave_atual(org["cnpj"]).chave_publica,
                                      revalidar)

    def recusar_oferta(self, oferta_id: str, chave_org: ParDeChaves,
                       recusado_por: str, motivo: str = "") -> dict[str, Any]:
        oferta = self._oferta(oferta_id)
        self._exigir_chave_da_org(self._org(oferta["emissor"]["cnpj"]), chave_org)
        rascunho = self.preparar_recusa(oferta_id, recusado_por, motivo)
        return self.registrar_assinado(chave_org.assinar(rascunho), "recusa")

    # ---------- aceite do embarcador (via link) ----------

    def aceitar_oferta(self, oferta_id: str, cnpj_embarcador: str, nome: str,
                       email: str, token: str) -> dict[str, Any]:
        """Registra o aceite feito por uma pessoa pelo link.

        Exige o token de aceite gerado na emissão da oferta. Como cada oferta
        só pode ser aceita uma vez, o token é de uso único.

        O embarcador não tem chave na V1: a plataforma ATESTA o aceite com a
        chave dela. Isso é mais fraco que uma assinatura do próprio embarcador,
        e está documentado como limitação.
        """
        oferta = self._oferta(oferta_id)
        # o token é conferido antes de tudo: sem ele, nada se revela sobre a oferta
        esperado = self.token_aceite_hash.get(oferta_id)
        if (not isinstance(token, str) or not token or esperado is None
                or not hmac.compare_digest(hash_token(token), esperado)):
            raise ErroElo("token de aceite inválido")
        status = self.status_oferta[oferta_id]
        if status != "enviada":
            raise ErroElo(f"oferta não pode ser aceita no status '{status}'")
        if formatar_cnpj(cnpj_embarcador) != oferta["destinatario_cnpj"]:
            raise ErroElo("só o destinatário da oferta pode aceitá-la")
        agora = self._agora()
        if agora > ler_momento(oferta["valida_ate"]):
            raise ErroElo(f"oferta vencida em {oferta['valida_ate']}")
        compromisso = self.chave.assinar({
            "tipo": "compromisso",
            "versao_protocolo": VERSAO_PROTOCOLO,
            "id": _novo_id("cp"),
            "oferta_hash": hash_sha256(oferta),
            "aceite": {
                "cnpj": oferta["destinatario_cnpj"],
                "nome": nome,
                "email": email,
                "modo": "humano_via_link",
                "em": agora.isoformat(timespec="seconds"),
            },
        })
        self._incluir("compromisso_firmado", compromisso, compromisso["id"])
        return compromisso

    # ---------- verificação independente ----------

    def verificar_compromisso(self, compromisso_id: str) -> Relatorio:
        """Verifica tudo a partir do que está gravado, sem confiar na memória."""
        self._reconstruir()
        r = Relatorio(compromisso_id)
        self._verificar(r)
        r.completar()
        return r

    def _verificar(self, r: Relatorio) -> None:
        compromisso_id = r.compromisso_id
        integridade = self.registro.verificar_integridade()
        r.checar("Registro íntegro", integridade.integro,
                 f"{integridade.total_eventos} eventos" if integridade.integro
                 else f"evento {integridade.primeiro_evento_com_problema}: "
                      f"{integridade.motivo}")

        cp = self.compromissos.get(compromisso_id)
        if not r.checar("Compromisso existe", cp is not None):
            return
        chave_plat = self.chaves_plataforma.get(cp["assinatura"]["chave_id"])
        r.checar("Aceite atestado pela plataforma",
                 chave_plat is not None and verificar_assinatura(cp, chave_plat))

        oferta_id = self.ofertas_por_hash.get(cp["oferta_hash"])
        if not r.checar("Oferta aceita encontrada pelo hash", oferta_id is not None):
            return
        oferta = self.ofertas[oferta_id]

        agente = self.agentes.get(oferta["emissor"]["agente_id"])
        if not r.checar("Agente emissor conhecido", agente is not None):
            return
        r.checar("Oferta assinada pelo agente",
                 verificar_assinatura(oferta, agente["chave_publica"]))

        org = self.organizacoes.get(agente["cnpj"])
        if not r.checar("Organização do agente cadastrada", org is not None,
                        agente["cnpj"]):
            return
        r.checar("Cadastro da organização autoassinado",
                 verificar_assinatura(org, org["chave_publica"]))
        eventos = self.eventos_chave.get(org["cnpj"], [])
        falhas = [e for e in eventos if not e.valido]
        trocas = len(self.chaves_org[org["cnpj"]]) - 1
        r.checar("Histórico de chaves da organização íntegro", not falhas,
                 f"evento {falhas[0].seq}: {falhas[0].motivo}" if falhas
                 else "chave original, sem rotação" if not trocas
                 else f"{trocas} rotação(ões) de chave, todas assinadas pela chave "
                      "de recuperação vigente")
        r.checar("Certificado do agente assinado pela organização",
                 self._assinado_pela_org(agente, org["cnpj"], "agente", agente["agente_id"]))
        r.checar("Oferta emitida pela mesma organização do agente",
                 oferta["emissor"]["cnpj"] == org["cnpj"])

        mandato = self.mandatos.get(oferta["mandato_id"])
        mandato_ok = (mandato is not None
                      and self._assinado_pela_org(mandato, org["cnpj"], "mandato", mandato["id"])
                      and mandato["agente_id"] == agente["agente_id"])
        r.checar("Mandato assinado pela organização para este agente", mandato_ok)

        if mandato_ok:
            problemas = violacoes(
                LimitesMandato(**mandato["limites"]),
                Carga(**oferta["carga"]), Condicoes(**oferta["condicoes"]),
                ler_momento(oferta["emitida_em"]),
                # revogação registrada depois da oferta não a torna irregular
                self.revogacoes.get(mandato["id"], {}).get("em")
                if self.revogado_na_emissao.get(oferta_id) else None,
                oferta.get("versao_protocolo"),
            )
            aprovacao = self.aprovacoes.get(oferta_id)
            recusa = self.recusas.get(oferta_id)
            if recusa is not None and self._assinado_pela_org(recusa, org["cnpj"], "recusa",
                                                              oferta_id):
                r.checar("Oferta autorizada", False,
                         f"oferta recusada pela organização em {recusa['em']} "
                         f"por {recusa['recusado_por']}")
            elif not problemas:
                r.checar("Oferta autorizada", True, "dentro do mandato")
            elif aprovacao is not None:
                justificativa = str(aprovacao.get("justificativa", "")).strip()
                # protocolo 0.2: aprovar abaixo do piso exige justificativa assinada
                sem_justificativa = (abaixo_do_piso(problemas)
                                     and versao_ao_menos(oferta.get("versao_protocolo"), "0.2")
                                     and len(justificativa) < MINIMO_JUSTIFICATIVA)
                aprov_ok = (self._assinado_pela_org(aprovacao, org["cnpj"], "aprovacao",
                                                    oferta_id)
                            and aprovacao["oferta_hash"] == cp["oferta_hash"]
                            and ler_momento(aprovacao["em"])
                            <= ler_momento(cp["aceite"]["em"])
                            and not sem_justificativa)
                r.checar("Oferta autorizada", aprov_ok,
                         f"fora do mandato ({'; '.join(problemas)}), aprovada por "
                         f"{aprovacao['aprovado_por']}"
                         + (f"; justificativa: {justificativa}" if justificativa else "")
                         + ("; abaixo do piso da ANTT e sem justificativa"
                            if sem_justificativa else ""))
            else:
                r.checar("Oferta autorizada", False,
                         f"fora do mandato e sem aprovação: {'; '.join(problemas)}")

        r.checar("Aceite feito pelo destinatário",
                 cp["aceite"]["cnpj"] == oferta["destinatario_cnpj"])
        r.checar("Aceite dentro da validade da oferta",
                 ler_momento(cp["aceite"]["em"]) <= ler_momento(oferta["valida_ate"]),
                 f"aceite {cp['aceite']['em']}, validade {oferta['valida_ate']}")

    # ---------- consultas do painel (só leitura, sempre de uma organização) ----------

    def listar_agentes(self, cnpj: str) -> list[dict[str, Any]]:
        cnpj = self._org(cnpj)["cnpj"]
        return [a for a in self.agentes.values() if a["cnpj"] == cnpj]

    def listar_mandatos(self, cnpj: str) -> list[dict[str, Any]]:
        cnpj = self._org(cnpj)["cnpj"]
        return [{"mandato": m,
                 "agente_nome": self.agentes.get(m["agente_id"], {}).get("nome"),
                 "tipos_carga": [_descrever_tipo(t) for t in m["limites"]["tipos_carga"]],
                 "revogado_em": self.revogacoes.get(m["id"], {}).get("em")}
                for m in self.mandatos.values() if m["cnpj"] == cnpj]

    def _embarcador(self, cnpj_org: str, oferta: dict[str, Any]) -> dict[str, Any]:
        """Quem pediu a cotação: CNPJ, nome informado no pedido e quantos fretes a
        transportadora já fechou com ele (compromissos firmados)."""
        destinatario = oferta["destinatario_cnpj"]
        pedido = self.pedidos.get(oferta.get("referencia", {}).get("pedido_id"), {})
        nome = pedido.get("nome_solicitante") or next(
            (p.get("nome_solicitante") for p in reversed(list(self.pedidos.values()))
             if p["cnpj_solicitante"] == destinatario and p.get("nome_solicitante")), None)
        fechados = 0
        for cp in self.compromissos.values():
            o = self.ofertas.get(self.ofertas_por_hash.get(cp["oferta_hash"], ""))
            if o and o["emissor"]["cnpj"] == cnpj_org and o["destinatario_cnpj"] == destinatario:
                fechados += 1
        return {"cnpj": destinatario, "nome": nome, "fretes_fechados": fechados}

    def listar_ofertas(self, cnpj: str, status: str | None = None) -> list[dict[str, Any]]:
        cnpj = self._org(cnpj)["cnpj"]
        return [{"oferta": o,
                 "embarcador": self._embarcador(cnpj, o),
                 "tipo_carga": _descrever_tipo(o["carga"]["tipo"]),
                 "status": self.status_oferta[o["id"]],
                 "violacoes": self.violacoes_oferta.get(o["id"], []),
                 "aprovacao": self.aprovacoes.get(o["id"]),
                 "recusa": self.recusas.get(o["id"])}
                for o in self.ofertas.values()
                if o["emissor"]["cnpj"] == cnpj
                and (status is None or self.status_oferta[o["id"]] == status)]

    def listar_compromissos(self, cnpj: str) -> list[dict[str, Any]]:
        cnpj = self._org(cnpj)["cnpj"]
        saida = []
        for cp in self.compromissos.values():
            oferta = self.ofertas.get(self.ofertas_por_hash.get(cp["oferta_hash"], ""))
            if oferta is not None and oferta["emissor"]["cnpj"] == cnpj:
                saida.append({"compromisso": cp, "oferta": oferta,
                              "embarcador": self._embarcador(cnpj, oferta),
                              "verificacao": f"/verificar/{cp['id']}"})
        return saida

    # ---------- auxiliares ----------

    def _org(self, cnpj: str) -> dict[str, Any]:
        org = self.organizacoes.get(formatar_cnpj(cnpj))
        if org is None:
            raise NaoEncontrado(f"organização não cadastrada: {cnpj}")
        return org

    def _agente(self, agente_id: str) -> dict[str, Any]:
        if agente_id not in self.agentes:
            raise NaoEncontrado(f"agente desconhecido: {agente_id}")
        return self.agentes[agente_id]

    def _mandato(self, mandato_id: str) -> dict[str, Any]:
        if mandato_id not in self.mandatos:
            raise NaoEncontrado(f"mandato desconhecido: {mandato_id}")
        return self.mandatos[mandato_id]

    def _pedido(self, pedido_id: str) -> dict[str, Any]:
        if pedido_id not in self.pedidos:
            raise NaoEncontrado(f"pedido desconhecido: {pedido_id}")
        return self.pedidos[pedido_id]

    def _oferta(self, oferta_id: str) -> dict[str, Any]:
        if oferta_id not in self.ofertas:
            raise NaoEncontrado(f"oferta desconhecida: {oferta_id}")
        return self.ofertas[oferta_id]

    def _exigir_chave_da_org(self, org: dict[str, Any], chave_org: ParDeChaves) -> None:
        if chave_org.publica_b64 != self._chave_atual(org["cnpj"]).chave_publica:
            raise ErroElo("a chave informada não é a chave desta organização")
