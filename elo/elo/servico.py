"""Orquestra o fluxo da V1: organização → agente → mandato → pedido → oferta →
(aprovação humana, se preciso) → aceite do embarcador → verificação.

O estado é sempre reconstruído a partir do registro (o registro é a fonte da
verdade). As chaves privadas das empresas NÃO ficam aqui: quem chama passa a
chave na hora de assinar. A plataforma só guarda a chave dela, usada para
atestar aceites recebidos por link.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import VERSAO_PROTOCOLO
from .canonico import hash_sha256
from .chaves import ParDeChaves, verificar_assinatura
from .cnpj import formatar_cnpj, validar_cnpj
from .mandato import violacoes
from .modelos import Carga, Condicoes, LimitesMandato, ler_momento
from .registro import Registro


class ErroElo(Exception):
    """Operação recusada por regra do protocolo."""


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


class Plataforma:
    def __init__(self, registro: Registro, chave_plataforma: ParDeChaves,
                 relogio: Callable[[], datetime] = _agora_utc) -> None:
        self.registro = registro
        self.chave = chave_plataforma
        self.relogio = relogio
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

    # ---------- organizações e agentes ----------

    def cadastrar_organizacao(self, cnpj: str, razao_social: str,
                              chave_org: ParDeChaves) -> dict[str, Any]:
        if not validar_cnpj(cnpj):
            raise ErroElo(f"CNPJ inválido: {cnpj}")
        cnpj_fmt = formatar_cnpj(cnpj)
        if cnpj_fmt in self.organizacoes:
            raise ErroElo(f"organização já cadastrada: {cnpj_fmt}")
        # autoassinado: prova que quem cadastra tem a chave privada da organização
        org = chave_org.assinar({
            "tipo": "organizacao",
            "versao_protocolo": VERSAO_PROTOCOLO,
            "cnpj": cnpj_fmt,
            "razao_social": razao_social,
            "chave_id": chave_org.chave_id,
            "chave_publica": chave_org.publica_b64,
            "cadastrada_em": self._agora_iso(),
            "cnpj_conferido_na_receita": False,  # V1: conferência manual
        })
        self._incluir("organizacao_cadastrada", org, cnpj_fmt)
        return org

    def emitir_agente(self, cnpj: str, chave_org: ParDeChaves,
                      chave_agente: ParDeChaves, nome: str) -> dict[str, Any]:
        org = self._org(cnpj)
        self._exigir_chave_da_org(org, chave_org)
        certificado = chave_org.assinar({
            "tipo": "certificado_agente",
            "versao_protocolo": VERSAO_PROTOCOLO,
            "agente_id": _novo_id("ag"),
            "nome": nome,
            "cnpj": org["cnpj"],
            "chave_id": chave_agente.chave_id,
            "chave_publica": chave_agente.publica_b64,
            "emitido_em": self._agora_iso(),
        })
        self._incluir("agente_emitido", certificado, certificado["agente_id"])
        return certificado

    def emitir_mandato(self, cnpj: str, chave_org: ParDeChaves, agente_id: str,
                       limites: LimitesMandato) -> dict[str, Any]:
        org = self._org(cnpj)
        self._exigir_chave_da_org(org, chave_org)
        agente = self._agente(agente_id)
        if agente["cnpj"] != org["cnpj"]:
            raise ErroElo("o agente não pertence a esta organização")
        mandato = chave_org.assinar({
            "tipo": "mandato",
            "versao_protocolo": VERSAO_PROTOCOLO,
            "id": _novo_id("md"),
            "cnpj": org["cnpj"],
            "agente_id": agente_id,
            "limites": limites.model_dump(mode="json"),
            "emitido_em": self._agora_iso(),
        })
        self._incluir("mandato_emitido", mandato, mandato["id"])
        return mandato

    def revogar_mandato(self, mandato_id: str, chave_org: ParDeChaves) -> dict[str, Any]:
        mandato = self._mandato(mandato_id)
        self._exigir_chave_da_org(self._org(mandato["cnpj"]), chave_org)
        if mandato_id in self.revogacoes:
            raise ErroElo("mandato já revogado")
        revogacao = chave_org.assinar({
            "tipo": "revogacao",
            "mandato_id": mandato_id,
            "em": self._agora_iso(),
        })
        self._incluir("mandato_revogado", revogacao, mandato_id)
        return revogacao

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

    def emitir_oferta(self, pedido_id: str, agente_id: str, mandato_id: str,
                      chave_agente: ParDeChaves, condicoes: Condicoes,
                      valida_ate: str) -> tuple[dict[str, Any], list[str]]:
        """Assina a oferta. Se sair do mandato, fica aguardando aprovação humana.

        Retorna (oferta, lista_de_violacoes). Lista vazia = oferta enviada.
        """
        pedido = self._pedido(pedido_id)
        agente = self._agente(agente_id)
        mandato = self._mandato(mandato_id)
        if chave_agente.publica_b64 != agente["chave_publica"]:
            raise ErroElo("a chave informada não é a chave deste agente")
        if mandato["agente_id"] != agente_id:
            raise ErroElo("o mandato não foi emitido para este agente")
        agora = self._agora()
        if ler_momento(valida_ate) <= agora:
            raise ErroElo("valida_ate precisa estar no futuro")

        carga = Carga(**pedido["carga"])
        problemas = violacoes(
            LimitesMandato(**mandato["limites"]), carga, condicoes, agora,
            self.revogacoes.get(mandato_id, {}).get("em"),
        )
        oferta = chave_agente.assinar({
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
        })
        status = "aguardando_aprovacao" if problemas else "enviada"
        self._incluir("oferta_emitida",
                      {"oferta": oferta, "status": status, "violacoes": problemas},
                      oferta["id"])
        return oferta, problemas

    def aprovar_oferta(self, oferta_id: str, chave_org: ParDeChaves,
                       aprovado_por: str) -> dict[str, Any]:
        oferta = self._oferta(oferta_id)
        self._exigir_chave_da_org(self._org(oferta["emissor"]["cnpj"]), chave_org)
        if self.status_oferta[oferta_id] != "aguardando_aprovacao":
            raise ErroElo(f"oferta não está aguardando aprovação "
                          f"({self.status_oferta[oferta_id]})")
        aprovacao = chave_org.assinar({
            "tipo": "aprovacao",
            "oferta_hash": hash_sha256(oferta),
            "aprovado_por": aprovado_por,
            "em": self._agora_iso(),
        })
        self._incluir("oferta_aprovada", aprovacao, oferta_id)
        return aprovacao

    # ---------- aceite do embarcador (via link) ----------

    def aceitar_oferta(self, oferta_id: str, cnpj_embarcador: str, nome: str,
                       email: str) -> dict[str, Any]:
        """Registra o aceite feito por uma pessoa pelo link.

        O embarcador não tem chave na V1: a plataforma ATESTA o aceite com a
        chave dela. Isso é mais fraco que uma assinatura do próprio embarcador,
        e está documentado como limitação.
        """
        oferta = self._oferta(oferta_id)
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

        integridade = self.registro.verificar_integridade()
        r.checar("Registro íntegro", integridade.integro,
                 f"{integridade.total_eventos} eventos" if integridade.integro
                 else f"evento {integridade.primeiro_evento_com_problema}: "
                      f"{integridade.motivo}")

        cp = self.compromissos.get(compromisso_id)
        if not r.checar("Compromisso existe", cp is not None):
            return r
        chave_plat = self.chaves_plataforma.get(cp["assinatura"]["chave_id"])
        r.checar("Aceite atestado pela plataforma",
                 chave_plat is not None and verificar_assinatura(cp, chave_plat))

        oferta_id = self.ofertas_por_hash.get(cp["oferta_hash"])
        if not r.checar("Oferta aceita encontrada pelo hash", oferta_id is not None):
            return r
        oferta = self.ofertas[oferta_id]

        agente = self.agentes.get(oferta["emissor"]["agente_id"])
        if not r.checar("Agente emissor conhecido", agente is not None):
            return r
        r.checar("Oferta assinada pelo agente",
                 verificar_assinatura(oferta, agente["chave_publica"]))

        org = self.organizacoes.get(agente["cnpj"])
        if not r.checar("Organização do agente cadastrada", org is not None,
                        agente["cnpj"]):
            return r
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
        return r

    # ---------- auxiliares ----------

    def _org(self, cnpj: str) -> dict[str, Any]:
        org = self.organizacoes.get(formatar_cnpj(cnpj))
        if org is None:
            raise ErroElo(f"organização não cadastrada: {cnpj}")
        return org

    def _agente(self, agente_id: str) -> dict[str, Any]:
        if agente_id not in self.agentes:
            raise ErroElo(f"agente desconhecido: {agente_id}")
        return self.agentes[agente_id]

    def _mandato(self, mandato_id: str) -> dict[str, Any]:
        if mandato_id not in self.mandatos:
            raise ErroElo(f"mandato desconhecido: {mandato_id}")
        return self.mandatos[mandato_id]

    def _pedido(self, pedido_id: str) -> dict[str, Any]:
        if pedido_id not in self.pedidos:
            raise ErroElo(f"pedido desconhecido: {pedido_id}")
        return self.pedidos[pedido_id]

    def _oferta(self, oferta_id: str) -> dict[str, Any]:
        if oferta_id not in self.ofertas:
            raise ErroElo(f"oferta desconhecida: {oferta_id}")
        return self.ofertas[oferta_id]

    @staticmethod
    def _exigir_chave_da_org(org: dict[str, Any], chave_org: ParDeChaves) -> None:
        if chave_org.publica_b64 != org["chave_publica"]:
            raise ErroElo("a chave informada não é a chave desta organização")
