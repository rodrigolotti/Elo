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

from . import VERSAO_PROTOCOLO
from .canonico import ErroCanonico, hash_sha256
from .chaves import ParDeChaves, chave_publica_valida, verificar_assinatura
from .cnpj import formatar_cnpj, validar_cnpj
from .mandato import violacoes
from .modelos import Carga, Condicoes, LimitesMandato, ler_momento
from .registro import Registro


class ErroElo(Exception):
    """Operação recusada por regra do protocolo."""


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
    "Certificado do agente assinado pela organização",
    "Oferta emitida pela mesma organização do agente",
    "Mandato assinado pela organização para este agente",
    "Oferta autorizada",
    "Aceite feito pelo destinatário",
    "Aceite dentro da validade da oferta",
)
"""As 14 checagens do relatório de verificação, na ordem em que são feitas."""


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
        """Garante as 14 checagens: as que não puderam ser feitas contam como falha."""
        feitas = {v.nome for v in self.verificacoes}
        for nome in CHECAGENS:
            if nome not in feitas:
                self.verificacoes.append(Verificacao(
                    nome, False, "não verificada: uma checagem anterior falhou"))
        ordem = {nome: i for i, nome in enumerate(CHECAGENS)}
        self.verificacoes.sort(key=lambda v: ordem.get(v.nome, len(ordem)))


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
        self.aprovacoes: dict[str, dict[str, Any]] = {}
        self.compromissos: dict[str, dict[str, Any]] = {}
        self.token_aceite_hash: dict[str, str | None] = {}
        for ev in self.registro.eventos():
            self._aplicar(ev.tipo, ev.payload)

    def _aplicar(self, tipo: str, p: dict[str, Any]) -> None:
        if tipo == "plataforma_chave":
            self.chaves_plataforma[p["chave_id"]] = p["chave_publica"]
        elif tipo == "organizacao_cadastrada":
            self.organizacoes[p["cnpj"]] = p
        elif tipo == "agente_emitido":
            self.agentes[p["agente_id"]] = p
        elif tipo == "mandato_emitido":
            self.mandatos[p["id"]] = p
        elif tipo == "mandato_revogado":
            self.revogacoes[p["mandato_id"]] = p
        elif tipo == "pedido_recebido":
            self.pedidos[p["id"]] = p
        elif tipo == "oferta_emitida":
            oferta = p["oferta"]
            self.ofertas[oferta["id"]] = oferta
            self.ofertas_por_hash[hash_sha256(oferta)] = oferta["id"]
            self.status_oferta[oferta["id"]] = p["status"]
            self.token_aceite_hash[oferta["id"]] = p.get("token_aceite_hash")
        elif tipo == "oferta_aprovada":
            # .get: num registro adulterado o hash pode não achar a oferta
            oferta_id = self.ofertas_por_hash.get(p["oferta_hash"])
            if oferta_id is not None:
                self.aprovacoes[oferta_id] = p
                self.status_oferta[oferta_id] = "enviada"
        elif tipo == "compromisso_firmado":
            self.compromissos[p["id"]] = p
            oferta_id = self.ofertas_por_hash.get(p["oferta_hash"])
            if oferta_id is not None:
                self.status_oferta[oferta_id] = "aceita"

    def _incluir(self, tipo: str, payload: dict[str, Any], objeto_id: str) -> None:
        self.registro.incluir(tipo, payload, self._agora_iso(), objeto_id)
        self._aplicar(tipo, payload)

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
                                      org["chave_publica"])

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
                                      mandato["id"], org["chave_publica"])

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
                                      mandato_id, org["chave_publica"], revalidar)

    def revogar_mandato(self, mandato_id: str, chave_org: ParDeChaves) -> dict[str, Any]:
        self._exigir_chave_da_org(self._org(self._mandato(mandato_id)["cnpj"]), chave_org)
        rascunho = self.preparar_revogacao(mandato_id)
        return self.registrar_assinado(chave_org.assinar(rascunho), "revogacao")

    # ---------- pedido e oferta ----------

    def registrar_pedido(self, cnpj_solicitante: str, canal: str, carga: Carga,
                         mensagem_original: str = "") -> dict[str, Any]:
        if not validar_cnpj(cnpj_solicitante):
            raise ErroElo(f"CNPJ do solicitante inválido: {cnpj_solicitante}")
        pedido = {
            "tipo": "pedido",
            "id": _novo_id("pc"),
            "cnpj_solicitante": formatar_cnpj(cnpj_solicitante),
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
            self.revogacoes.get(mandato_id, {}).get("em"),
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
            "condicoes": condicoes.model_dump(mode="json"),
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

    def preparar_aprovacao(self, oferta_id: str, aprovado_por: str) -> dict[str, Any]:
        oferta = self._oferta(oferta_id)
        org = self._org(oferta["emissor"]["cnpj"])

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
        }
        return self._guardar_rascunho("aprovacao", "oferta_aprovada", aprovacao,
                                      oferta_id, org["chave_publica"], revalidar)

    def aprovar_oferta(self, oferta_id: str, chave_org: ParDeChaves,
                       aprovado_por: str) -> dict[str, Any]:
        oferta = self._oferta(oferta_id)
        self._exigir_chave_da_org(self._org(oferta["emissor"]["cnpj"]), chave_org)
        rascunho = self.preparar_aprovacao(oferta_id, aprovado_por)
        return self.registrar_assinado(chave_org.assinar(rascunho), "aprovacao")

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
        r.checar("Certificado do agente assinado pela organização",
                 verificar_assinatura(agente, org["chave_publica"]))
        r.checar("Oferta emitida pela mesma organização do agente",
                 oferta["emissor"]["cnpj"] == org["cnpj"])

        mandato = self.mandatos.get(oferta["mandato_id"])
        mandato_ok = (mandato is not None
                      and verificar_assinatura(mandato, org["chave_publica"])
                      and mandato["agente_id"] == agente["agente_id"])
        r.checar("Mandato assinado pela organização para este agente", mandato_ok)

        if mandato_ok:
            problemas = violacoes(
                LimitesMandato(**mandato["limites"]),
                Carga(**oferta["carga"]), Condicoes(**oferta["condicoes"]),
                ler_momento(oferta["emitida_em"]),
                self.revogacoes.get(mandato["id"], {}).get("em"),
            )
            aprovacao = self.aprovacoes.get(oferta_id)
            if not problemas:
                r.checar("Oferta autorizada", True, "dentro do mandato")
            elif aprovacao is not None:
                aprov_ok = (verificar_assinatura(aprovacao, org["chave_publica"])
                            and aprovacao["oferta_hash"] == cp["oferta_hash"]
                            and ler_momento(aprovacao["em"])
                            <= ler_momento(cp["aceite"]["em"]))
                r.checar("Oferta autorizada", aprov_ok,
                         f"fora do mandato ({'; '.join(problemas)}), aprovada por "
                         f"{aprovacao['aprovado_por']}")
            else:
                r.checar("Oferta autorizada", False,
                         f"fora do mandato e sem aprovação: {'; '.join(problemas)}")

        r.checar("Aceite feito pelo destinatário",
                 cp["aceite"]["cnpj"] == oferta["destinatario_cnpj"])
        r.checar("Aceite dentro da validade da oferta",
                 ler_momento(cp["aceite"]["em"]) <= ler_momento(oferta["valida_ate"]),
                 f"aceite {cp['aceite']['em']}, validade {oferta['valida_ate']}")

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

    @staticmethod
    def _exigir_chave_da_org(org: dict[str, Any], chave_org: ParDeChaves) -> None:
        if chave_org.publica_b64 != org["chave_publica"]:
            raise ErroElo("a chave informada não é a chave desta organização")
