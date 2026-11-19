"""API HTTP (FastAPI) sobre elo.servico.

Esta camada só traduz HTTP <-> chamadas da Plataforma: nenhuma regra de
negócio mora aqui. Erros de regra (ErroElo) viram 400, objetos inexistentes
(NaoEncontrado) viram 404 e corpo mal formado vira 422.

A API nunca recebe chave privada. Tudo que precisa da assinatura de uma
empresa segue dois passos:

    POST /<recurso>/rascunho   -> a plataforma devolve o objeto sem assinatura
    (o cliente assina localmente com a própria chave)
    POST /<recurso>            -> o cliente envia o objeto assinado

Consultas (listas de agentes, mandatos, ofertas e compromissos) exigem sessão:
o cliente pede um desafio, assina com a chave da organização e recebe um
token (cabeçalho "Authorization: Bearer <token>"). Cada sessão só enxerga os
dados da própria organização. Fora isso, só a verificação é pública.

O painel web da transportadora (elo/painel) é servido em /painel. Ele gera e
usa a chave da organização só no navegador e fala com esta mesma API.

Subir:  uvicorn elo.api:app --reload
Banco:  variável ELO_DB (padrão: elo.db na pasta atual).
"""

from __future__ import annotations

import html
import mimetypes
import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal
from zoneinfo import ZoneInfo

from fastapi import Body, Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .antt import nome_tipo_carga
from .canonico import hash_sha256
from .chaves import ParDeChaves
from .distancia import EstimadorDistancia
from .mandato import piso_da_oferta, reais
from .modelos import Carga, Condicoes, LimitesMandato, ler_momento
from .registro import Registro
from .servico import ErroElo, NaoEncontrado, Plataforma, Relatorio
from .sessoes import NaoAutenticado, Sessoes

# ---------- corpos das requisições ----------


class _Corpo(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrepararOrganizacao(_Corpo):
    cnpj: str
    razao_social: str = Field(min_length=1, max_length=200)
    chave_id: str = Field(min_length=1, max_length=100)
    chave_publica: str = Field(description="Chave pública Ed25519, 32 bytes em base64")


class PrepararAgente(_Corpo):
    cnpj: str = Field(description="CNPJ da organização dona do agente")
    nome: str = Field(min_length=1, max_length=200)
    chave_id: str = Field(min_length=1, max_length=100)
    chave_publica: str = Field(description="Chave pública Ed25519 do agente, em base64")


class PrepararMandato(_Corpo):
    cnpj: str
    agente_id: str
    limites: LimitesMandato


class RegistrarPedido(_Corpo):
    cnpj_solicitante: str
    canal: str = Field(min_length=1, max_length=40)
    carga: Carga
    mensagem_original: str = Field(default="", max_length=20_000)
    nome_solicitante: str = Field(default="", max_length=200,
                                  description="Nome do embarcador, como informado no pedido")


class PrepararOferta(_Corpo):
    pedido_id: str
    agente_id: str
    mandato_id: str
    condicoes: Condicoes
    valida_ate: str = Field(description="ISO 8601 com fuso, ex.: 2026-11-18T18:00:00-03:00")


class PrepararAprovacao(_Corpo):
    aprovado_por: str = Field(min_length=1, max_length=200)
    justificativa: str = Field(default="", max_length=1000,
                               description="Obrigatória para aprovar frete abaixo do piso da ANTT")


class PrepararRecusa(_Corpo):
    recusado_por: str = Field(min_length=1, max_length=200)
    motivo: str = Field(default="", max_length=1000)


class PrepararRecuperacao(_Corpo):
    cnpj: str
    chave_id: str = Field(min_length=1, max_length=100)
    chave_publica: str = Field(description="Chave pública Ed25519 de recuperação, em base64")


class PrepararRotacao(_Corpo):
    cnpj: str
    chave_id: str = Field(min_length=1, max_length=100)
    chave_publica: str = Field(description="Nova chave pública principal, em base64")
    recuperacao_chave_id: str = Field(min_length=1, max_length=100)
    recuperacao_chave_publica: str = Field(description="Nova chave pública de recuperação")


class PedirDesafio(_Corpo):
    cnpj: str


StatusOferta = Literal["aguardando_aprovacao", "enviada", "aceita", "recusada"]


class Aceite(_Corpo):
    token: str = Field(min_length=1, max_length=200,
                       description="Token de aceite devolvido uma única vez em POST /ofertas")
    cnpj: str = Field(description="CNPJ do embarcador destinatário da oferta")
    nome: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=200)


# ---------- painel web ----------

PASTA_PAINEL = Path(__file__).parent / "painel"

# No Windows o registro pode associar .js a text/plain, e o navegador recusa
# módulos JavaScript servidos assim.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")

# O painel usa a chave da organização: um script estranho na página conseguiria
# usá-la (não copiá-la). Por isso só roda script servido daqui, sem inline.
CABECALHOS_PAINEL = {
    "Content-Security-Policy": "; ".join([
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",  # Inter, guardada no projeto (sem CDN)
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


# ---------- sessão ----------

_portador = HTTPBearer(auto_error=False)


def _token(credenciais: Annotated[HTTPAuthorizationCredentials | None,
                                  Depends(_portador)]) -> str | None:
    return credenciais.credentials if credenciais else None


Token = Annotated[str | None, Depends(_token)]


def _cnpj_logado(request: Request, token: Token) -> str:
    """CNPJ da sessão; 401 sem token válido."""
    with request.app.state.trava:
        return request.app.state.sessoes.cnpj_da_sessao(token)


Logado = Annotated[str, Depends(_cnpj_logado)]


ObjetoAssinado = Body(description="O rascunho devolvido pela plataforma, com o campo "
                                  "'assinatura' preenchido pelo cliente")


# ---------- aplicação ----------


def _plataforma_padrao() -> Plataforma:
    # A chave da plataforma (não a das empresas) é gerada a cada início e só
    # vive na memória. A chave pública vai para o registro, então aceites
    # atestados por chaves de execuções anteriores continuam verificáveis.
    chave = ParDeChaves.gerar(f"plataforma#{uuid.uuid4().hex[:8]}")
    return Plataforma(Registro(os.environ.get("ELO_DB", "elo.db")), chave)


class _EstimativaDesligada:
    """Para testes e roteiros sem rede: ELO_ESTIMATIVA_CEP=desligada."""

    def estimar(self, cep_origem: str, cep_destino: str) -> dict[str, Any]:
        return {"km": None, "motivo": "estimativa por CEP desligada neste servidor"}


def _estimador_padrao() -> Any:
    if os.environ.get("ELO_ESTIMATIVA_CEP", "").lower() == "desligada":
        return _EstimativaDesligada()
    return EstimadorDistancia()


def _frete(item: dict[str, Any], estimador: Any) -> dict[str, Any]:
    """Se o frete paga o piso: modalidade, distância (do agente ou estimada),
    valor por km e piso da ANTT. Só para exibir; a regra do mandato usa só o que
    o agente assinou (elo.mandato.violacoes)."""
    oferta = item["oferta"]
    condicoes = Condicoes(**oferta["condicoes"])
    carga = Carga(**oferta["carga"])
    valor = condicoes.valor_centavos
    if condicoes.distancia_km:
        distancia = {"km": condicoes.distancia_km, "origem": "agente"}
    else:
        estimada = estimador.estimar(carga.origem_cep, carga.destino_cep)
        distancia = {"km": estimada.get("km"), "origem": "estimada",
                     "detalhe": (f"{estimada['origem']} → {estimada['destino']}"
                                 if estimada.get("km") else estimada.get("motivo"))}
    km = distancia["km"]
    frete: dict[str, Any] = {
        "modalidade": condicoes.modalidade, "eixos": condicoes.eixos, "distancia": distancia,
        # arredondado meio para cima, em centavos por km
        "valor_por_km_centavos": (2 * valor + km) // (2 * km) if km else None,
        "piso": None, "sem_piso": None,
    }
    if condicoes.modalidade == "fracionada":
        frete["sem_piso"] = "carga fracionada: o piso da ANTT (Tabela A) não se aplica"
    elif condicoes.modalidade is None:
        frete["sem_piso"] = "o agente não informou se é lotação ou fracionada"
    elif condicoes.eixos is None:
        frete["sem_piso"] = "o agente não informou o número de eixos"
    elif not km:
        frete["sem_piso"] = f"sem distância: {distancia.get('detalhe')}"
    else:
        piso = piso_da_oferta(carga, condicoes.model_copy(update={"distancia_km": km}),
                              ler_momento(oferta["emitida_em"]))
        if piso is None:
            frete["sem_piso"] = ("a Tabela A da ANTT não publica piso para esta combinação "
                                 "de categoria, eixos e data")
        else:
            frete["piso"] = {**piso, "estimado": distancia["origem"] == "estimada",
                             "folga_centavos": valor - piso["piso_centavos"]}
    return frete


def criar_app(plataforma: Plataforma | None = None, estimador: Any = None,
              demo: bool | None = None) -> FastAPI:
    """Cria a API. Sem plataforma, abre o banco de ELO_DB ao iniciar."""

    @asynccontextmanager
    async def ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
        propria = getattr(app.state, "plataforma", None) is None
        if propria:
            app.state.plataforma = _plataforma_padrao()
        yield
        if propria:  # fecha o banco que esta app abriu
            app.state.plataforma.registro.conexao.close()
            app.state.plataforma = None

    app = FastAPI(
        title="Elo",
        description="Camada de confiança entre agentes logísticos (V1). "
                    "Chaves privadas nunca passam pela API.",
        version="0.1",
        lifespan=ciclo_de_vida,
    )
    app.state.plataforma = plataforma
    app.state.estimador = estimador if estimador is not None else _estimador_padrao()
    if demo is None:
        demo = os.environ.get("ELO_DEMO", "").lower() == "ligado"
    sessoes = app.state.sessoes = Sessoes(lambda: app.state.plataforma)
    # a Plataforma mantém estado em memória e uma conexão SQLite: uma
    # requisição por vez
    trava = app.state.trava = threading.Lock()

    @app.exception_handler(NaoEncontrado)
    async def _nao_encontrado(_: Request, erro: NaoEncontrado) -> JSONResponse:
        return JSONResponse({"detail": str(erro)}, status_code=404)

    @app.exception_handler(ErroElo)
    async def _erro_elo(_: Request, erro: ErroElo) -> JSONResponse:
        return JSONResponse({"detail": str(erro)}, status_code=400)

    @app.exception_handler(NaoAutenticado)
    async def _nao_autenticado(_: Request, erro: NaoAutenticado) -> JSONResponse:
        return JSONResponse({"detail": str(erro)}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})

    @app.middleware("http")
    async def _cabecalhos_do_painel(request: Request, call_next):  # type: ignore[no-untyped-def]
        resposta = await call_next(request)
        caminho = request.url.path
        if caminho == "/painel" or caminho.startswith("/painel/"):
            resposta.headers.update(CABECALHOS_PAINEL)
        return resposta

    app.mount("/painel", StaticFiles(directory=PASTA_PAINEL, html=True), name="painel")

    @app.get("/", include_in_schema=False)
    def inicio() -> RedirectResponse:
        return RedirectResponse("/painel/")

    def plat() -> Plataforma:
        return app.state.plataforma

    # ----- modo demonstração (só existe com ELO_DEMO=ligado) -----

    if demo:
        from .demo import AgenteDemo
        agente_demo = app.state.agente_demo = AgenteDemo()

        @app.get("/demo/agente", tags=["demonstração"])
        def agente_de_demonstracao() -> dict[str, Any]:
            """Chave pública do agente simulado, para o painel assinar o certificado."""
            return agente_demo.descricao()

        @app.post("/demo/simular", tags=["demonstração"])
        def simular_pedido(cnpj: Logado) -> dict[str, Any]:
            """Pedido de um embarcador fictício e três ofertas do agente simulado."""
            with trava:
                return agente_demo.simular(plat(), cnpj)

    # ----- sessão do painel -----

    @app.post("/sessoes/desafio", tags=["sessão"])
    def pedir_desafio(corpo: PedirDesafio) -> dict[str, Any]:
        """Desafio para assinar com a chave da organização. Vale 5 minutos e uma vez só."""
        with trava:
            return {"desafio": sessoes.criar_desafio(corpo.cnpj)}

    @app.post("/sessoes", status_code=201, tags=["sessão"])
    def entrar(assinado: dict[str, Any] = Body(
            description="O desafio, com o campo 'assinatura' preenchido")) -> dict[str, Any]:
        with trava:
            return sessoes.abrir(assinado)

    @app.delete("/sessoes", status_code=204, tags=["sessão"])
    def sair(cnpj: Logado, token: Token) -> Response:
        with trava:
            sessoes.encerrar(token)
        return Response(status_code=204)

    # ----- consultas (exigem sessão; só dados da própria organização) -----

    @app.get("/organizacao", tags=["consulta"])
    def minha_organizacao(cnpj: Logado) -> dict[str, Any]:
        """Cadastro, chave vigente, chave de recuperação e histórico de chaves."""
        with trava:
            return plat().resumo_organizacao(cnpj)

    @app.get("/agentes", tags=["consulta"])
    def listar_agentes(cnpj: Logado) -> list[dict[str, Any]]:
        with trava:
            return plat().listar_agentes(cnpj)

    @app.get("/mandatos", tags=["consulta"])
    def listar_mandatos(cnpj: Logado) -> list[dict[str, Any]]:
        with trava:
            return plat().listar_mandatos(cnpj)

    @app.get("/ofertas", tags=["consulta"])
    def listar_ofertas(cnpj: Logado, status: StatusOferta | None = None) -> list[dict[str, Any]]:
        """Ofertas da organização, cada uma com "frete": distância, valor por km e
        piso da ANTT (a estimativa por CEP roda fora da trava)."""
        with trava:
            itens = plat().listar_ofertas(cnpj, status)
        return [{**item, "frete": _frete(item, app.state.estimador)} for item in itens]

    @app.get("/visao-geral", tags=["consulta"])
    def visao_geral(cnpj: Logado) -> dict[str, Any]:
        """Resumo do mês e as ofertas aguardando decisão (com o bloco "frete")."""
        with trava:
            resumo = plat().visao_geral(cnpj)
            itens = plat().listar_ofertas(cnpj, "aguardando_aprovacao")
        resumo["aguardando"]["itens"] = [{**i, "frete": _frete(i, app.state.estimador)}
                                         for i in itens]
        return resumo

    @app.get("/compromissos", tags=["consulta"])
    def listar_compromissos(cnpj: Logado) -> list[dict[str, Any]]:
        with trava:
            return plat().listar_compromissos(cnpj)

    # ----- organização -----

    @app.post("/organizacoes/rascunho", tags=["organização"])
    def preparar_organizacao(corpo: PrepararOrganizacao) -> dict[str, Any]:
        with trava:
            return {"rascunho": plat().preparar_organizacao(
                corpo.cnpj, corpo.razao_social, corpo.chave_id, corpo.chave_publica)}

    @app.post("/organizacoes", status_code=201, tags=["organização"])
    def cadastrar_organizacao(assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        with trava:
            return plat().registrar_assinado(assinado, "organizacao")

    @app.post("/organizacoes/recuperacao/rascunho", tags=["organização"])
    def preparar_recuperacao(corpo: PrepararRecuperacao) -> dict[str, Any]:
        """Define ou troca a chave de recuperação. Assinado pela chave principal."""
        with trava:
            return {"rascunho": plat().preparar_recuperacao(
                corpo.cnpj, corpo.chave_id, corpo.chave_publica)}

    @app.post("/organizacoes/recuperacao", status_code=201, tags=["organização"])
    def definir_recuperacao(assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        with trava:
            return plat().registrar_assinado(assinado, "recuperacao_chave")

    @app.post("/organizacoes/rotacao/rascunho", tags=["organização"])
    def preparar_rotacao(corpo: PrepararRotacao) -> dict[str, Any]:
        """Troca a chave principal. Assinado pela chave de RECUPERAÇÃO vigente.

        Não exige sessão: é o caminho de quem perdeu a chave principal.
        """
        with trava:
            return {"rascunho": plat().preparar_rotacao(
                corpo.cnpj, corpo.chave_id, corpo.chave_publica,
                corpo.recuperacao_chave_id, corpo.recuperacao_chave_publica)}

    @app.post("/organizacoes/rotacao", status_code=201, tags=["organização"])
    def rotacionar_chave(assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        """Grava a rotação e encerra as sessões e desafios abertos da organização."""
        with trava:
            gravado = plat().registrar_assinado(assinado, "rotacao_chave")
            sessoes.encerrar_da_organizacao(gravado["cnpj"])
            return gravado

    # ----- agente -----

    @app.post("/agentes/rascunho", tags=["agente"])
    def preparar_agente(corpo: PrepararAgente) -> dict[str, Any]:
        with trava:
            return {"rascunho": plat().preparar_agente(
                corpo.cnpj, corpo.nome, corpo.chave_id, corpo.chave_publica)}

    @app.post("/agentes", status_code=201, tags=["agente"])
    def emitir_agente(assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        with trava:
            return plat().registrar_assinado(assinado, "certificado_agente")

    # ----- mandato -----

    @app.post("/mandatos/rascunho", tags=["mandato"])
    def preparar_mandato(corpo: PrepararMandato) -> dict[str, Any]:
        with trava:
            return {"rascunho": plat().preparar_mandato(
                corpo.cnpj, corpo.agente_id, corpo.limites)}

    @app.post("/mandatos", status_code=201, tags=["mandato"])
    def emitir_mandato(assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        with trava:
            return plat().registrar_assinado(assinado, "mandato")

    @app.post("/mandatos/{mandato_id}/revogacao/rascunho", tags=["mandato"])
    def preparar_revogacao(mandato_id: str) -> dict[str, Any]:
        with trava:
            return {"rascunho": plat().preparar_revogacao(mandato_id)}

    @app.post("/mandatos/{mandato_id}/revogacao", status_code=201, tags=["mandato"])
    def revogar_mandato(mandato_id: str,
                        assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        """Revoga o mandato: ofertas emitidas a partir daí saem do mandato."""
        with trava:
            plat()._mandato(mandato_id)  # 404 se não existir
            if assinado.get("mandato_id") != mandato_id:
                raise ErroElo("a revogação enviada é de outro mandato")
            return plat().registrar_assinado(assinado, "revogacao")

    # ----- pedido (não precisa de assinatura) -----

    @app.post("/pedidos", status_code=201, tags=["pedido"])
    def registrar_pedido(corpo: RegistrarPedido) -> dict[str, Any]:
        with trava:
            return plat().registrar_pedido(corpo.cnpj_solicitante, corpo.canal,
                                           corpo.carga, corpo.mensagem_original,
                                           corpo.nome_solicitante)

    # ----- oferta -----

    @app.post("/ofertas/rascunho", tags=["oferta"])
    def preparar_oferta(corpo: PrepararOferta) -> dict[str, Any]:
        with trava:
            rascunho, problemas = plat().preparar_oferta(
                corpo.pedido_id, corpo.agente_id, corpo.mandato_id,
                corpo.condicoes, corpo.valida_ate)
        return {"rascunho": rascunho, "violacoes": problemas,
                "status_previsto": "aguardando_aprovacao" if problemas else "enviada"}

    @app.post("/ofertas", status_code=201, tags=["oferta"])
    def emitir_oferta(assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        """Devolve {"oferta", "status", "violacoes", "token_aceite_hash", "token_aceite"}.

        O token_aceite só aparece nesta resposta (o registro guarda só o hash):
        a transportadora o envia ao embarcador junto com o link de aceite.
        """
        with trava:
            return plat().registrar_assinado(assinado, "oferta")

    @app.post("/ofertas/{oferta_id}/aprovacao/rascunho", tags=["oferta"])
    def preparar_aprovacao(oferta_id: str, corpo: PrepararAprovacao) -> dict[str, Any]:
        with trava:
            return {"rascunho": plat().preparar_aprovacao(oferta_id, corpo.aprovado_por,
                                                          corpo.justificativa)}

    @app.post("/ofertas/{oferta_id}/aprovacao", status_code=201, tags=["oferta"])
    def aprovar_oferta(oferta_id: str,
                       assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        with trava:
            oferta = plat()._oferta(oferta_id)  # 404 se não existir
            if assinado.get("oferta_hash") != hash_sha256(oferta):
                raise ErroElo("a aprovação enviada é de outra oferta")
            return plat().registrar_assinado(assinado, "aprovacao")

    @app.post("/ofertas/{oferta_id}/recusa/rascunho", tags=["oferta"])
    def preparar_recusa(oferta_id: str, corpo: PrepararRecusa) -> dict[str, Any]:
        with trava:
            return {"rascunho": plat().preparar_recusa(oferta_id, corpo.recusado_por,
                                                       corpo.motivo)}

    @app.post("/ofertas/{oferta_id}/recusa", status_code=201, tags=["oferta"])
    def recusar_oferta(oferta_id: str,
                       assinado: dict[str, Any] = ObjetoAssinado) -> dict[str, Any]:
        """Recusa definitiva, assinada pela organização e gravada no registro."""
        with trava:
            oferta = plat()._oferta(oferta_id)  # 404 se não existir
            if assinado.get("oferta_hash") != hash_sha256(oferta):
                raise ErroElo("a recusa enviada é de outra oferta")
            return plat().registrar_assinado(assinado, "recusa")

    @app.post("/ofertas/{oferta_id}/aceite", status_code=201, tags=["oferta"])
    def aceitar_oferta(oferta_id: str, corpo: Aceite) -> dict[str, Any]:
        """Aceite humano pelo link, com o token de uso único. A plataforma atesta."""
        with trava:
            return plat().aceitar_oferta(oferta_id, corpo.cnpj, corpo.nome, corpo.email,
                                         corpo.token)

    # ----- verificação -----

    @app.get("/compromissos/{compromisso_id}/verificacao", tags=["verificação"])
    def verificar_compromisso(compromisso_id: str) -> JSONResponse:
        with trava:
            relatorio = plat().verificar_compromisso(compromisso_id)
        return JSONResponse(_relatorio_json(relatorio),
                            status_code=200 if _existe(relatorio) else 404)

    @app.get("/verificar/{compromisso_id}", response_class=HTMLResponse,
             tags=["verificação"])
    def pagina_verificacao(compromisso_id: str) -> HTMLResponse:
        """Página pública, em português, com as 15 checagens e o resultado."""
        with trava:
            relatorio = plat().verificar_compromisso(compromisso_id)
            topo = _resumo_topo(plat(), compromisso_id)
            resumo = _resumo(plat(), compromisso_id)
        return HTMLResponse(_pagina(relatorio, topo, resumo),
                            status_code=200 if _existe(relatorio) else 404)

    return app


# ---------- apresentação ----------


def _existe(relatorio: Relatorio) -> bool:
    return any(v.nome == "Compromisso existe" and v.ok for v in relatorio.verificacoes)


def _relatorio_json(relatorio: Relatorio) -> dict[str, Any]:
    return {
        "compromisso_id": relatorio.compromisso_id,
        "valido": relatorio.valido,
        "verificacoes": [{"nome": v.nome, "ok": v.ok, "detalhe": v.detalhe}
                         for v in relatorio.verificacoes],
    }


_BRASILIA = ZoneInfo("America/Sao_Paulo")
# data e hora ISO 8601 com fuso, como gravadas no registro (ex.: 2026-11-18T00:48:00+00:00)
_MOMENTO_ISO = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})")


def _em_brasilia(momento_iso: str) -> str:
    """Só para exibir: '17/11/2026 às 21:48 (horário de Brasília)'.

    O valor gravado e assinado continua em ISO 8601, no fuso em que foi emitido.
    """
    momento = ler_momento(momento_iso).astimezone(_BRASILIA)
    return f"{momento:%d/%m/%Y} às {momento:%H:%M} (horário de Brasília)"


def _datas_em_brasilia(texto: str) -> str:
    """Troca, num texto de exibição, cada data e hora ISO pela forma de Brasília."""
    return _MOMENTO_ISO.sub(lambda m: _em_brasilia(m.group()), texto)


def _data_br(data_iso: str) -> str:
    """Data sem hora (ex.: coleta_ate): '20/11/2026'. Sem hora, não há fuso a converter."""
    ano, mes, dia = data_iso.split("-")
    return f"{dia}/{mes}/{ano}"


def _milhares(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _m3(litros: int) -> str:
    """14500 -> '14,5 m³' (inteiros, sem float)."""
    inteiro, fracao = divmod(litros, 1000)
    decimais = f"{fracao:03d}".rstrip("0")
    return _milhares(inteiro) + (f",{decimais}" if decimais else "") + " m³"


def _resumo_topo(plataforma: Plataforma, compromisso_id: str) -> dict[str, str] | None:
    """O que o embarcador quer ver primeiro: valor, rota, transportadora e quando
    o frete foi fechado. None se o compromisso ou a oferta não forem encontrados."""
    cp = plataforma.compromissos.get(compromisso_id)
    if cp is None:
        return None
    oferta = plataforma.ofertas.get(plataforma.ofertas_por_hash.get(cp["oferta_hash"], ""))
    if oferta is None:
        return None
    cnpj = oferta["emissor"]["cnpj"]
    org = plataforma.organizacoes.get(cnpj)
    carga = oferta["carga"]
    return {
        "valor": reais(oferta["condicoes"]["valor_centavos"]),
        "rota": f"{carga['origem_uf']} ({carga['origem_cep']}) → "
                f"{carga['destino_uf']} ({carga['destino_cep']})",
        "transportadora": f"{org['razao_social']} ({cnpj})" if org else cnpj,
        "fechado_em": _em_brasilia(cp["aceite"]["em"]),
    }


def _resumo(plataforma: Plataforma, compromisso_id: str) -> list[tuple[str, str]]:
    """Dados do acordo para a página. Só leitura do estado; sem regra de negócio.

    Nome e e-mail de quem aceitou ficam fora da página pública.
    """
    cp = plataforma.compromissos.get(compromisso_id)
    if cp is None:
        return []
    linhas = [("Aceite em", _em_brasilia(cp["aceite"]["em"])), ("Embarcador", cp["aceite"]["cnpj"])]
    oferta_id = plataforma.ofertas_por_hash.get(cp["oferta_hash"])
    oferta = plataforma.ofertas.get(oferta_id) if oferta_id else None
    if oferta is not None:
        cnpj = oferta["emissor"]["cnpj"]
        org = plataforma.organizacoes.get(cnpj)
        carga, cond = oferta["carga"], oferta["condicoes"]
        linhas = [
            ("Transportadora", f"{org['razao_social']} ({cnpj})" if org else cnpj),
            *linhas,
            ("Oferta", oferta["id"]),
            ("Valor", reais(cond["valor_centavos"])),
            ("Rota", f"{carga['origem_uf']} ({carga['origem_cep']}) → "
                     f"{carga['destino_uf']} ({carga['destino_cep']})"),
            ("Carga", f"{nome_tipo_carga(carga['tipo'])}, {_milhares(carga['peso_kg'])} kg, "
                      f"{_m3(carga['volume_litros'])}"),
            ("Coleta até", _data_br(cond["coleta_ate"])),
            ("Prazo de entrega", f"{cond['prazo_entrega_dias_uteis']} dias úteis"),
        ]
    linhas.append(("Hash da oferta", cp["oferta_hash"]))
    return linhas


_ESTILO = """
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;
background:#f6f7f9;color:#1d2330}
main{max-width:760px;margin:0 auto;padding:32px 20px 48px}
h1{font-size:1.4rem;margin:0 0 4px}
h2{font-size:1.1rem;margin:28px 0 10px}
.id{font-family:ui-monospace,Consolas,monospace;color:#5b6475;margin:0 0 20px;
word-break:break-all}
.topo{border-radius:12px;padding:20px 22px;margin-bottom:8px}
.valido{background:#e3f4e8;color:#14532d;border:1px solid #9fd5b0}
.invalido{background:#fbe4e4;color:#7f1d1d;border:1px solid #eba9a9}
.selo{font-weight:700;font-size:1rem;letter-spacing:.02em}
.valor{font-size:2rem;font-weight:800;color:#1d2330;margin:10px 0 2px}
.rota{font-size:1.1rem;color:#1d2330;margin-bottom:12px}
.fatos{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0 0 12px;
color:#1d2330}
.fatos dt{color:#5b6475}.fatos dd{margin:0}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;
overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.06);margin-bottom:24px}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid #eceef2;
vertical-align:top}
th{background:#f0f2f5;font-size:.85rem;color:#5b6475}
tr:last-child td{border-bottom:0}
.n{color:#8a93a3;width:2em}
.st{font-weight:700;white-space:nowrap;width:5em}
.ok{color:#15803d}.falha{color:#b91c1c}
.det{color:#5b6475;font-size:.9rem}
.mono{font-family:ui-monospace,Consolas,monospace;word-break:break-all}
footer{color:#5b6475;font-size:.85rem;line-height:1.5}
"""


def _pagina(relatorio: Relatorio, topo: dict[str, str] | None,
            resumo: list[tuple[str, str]]) -> str:
    e = html.escape
    valido = relatorio.valido
    linhas = "".join(
        f'<tr><td class="n">{i}</td><td>{e(v.nome)}'
        + (f'<div class="det">{e(_datas_em_brasilia(v.detalhe))}</div>'
           if v.detalhe else "")
        + f'</td><td class="st {"ok" if v.ok else "falha"}">'
          f'{"OK" if v.ok else "FALHA"}</td></tr>'
        for i, v in enumerate(relatorio.verificacoes, 1)
    )
    tabela_resumo = ""
    if resumo:
        tabela_resumo = (
            '<h2>Detalhes do que foi combinado</h2><table>'
            + "".join(f'<tr><th>{e(k)}</th><td class="{"mono" if k.startswith("Hash") else ""}">'
                      f'{e(v)}</td></tr>' for k, v in resumo)
            + "</table>")
    ok_total = sum(v.ok for v in relatorio.verificacoes)
    total = len(relatorio.verificacoes)
    cid = e(relatorio.compromisso_id)
    selo = "✔ COMPROMISSO VÁLIDO" if valido else "✘ COMPROMISSO INVÁLIDO"
    if valido:
        prova = f"{ok_total} de {total} checagens OK. A prova está logo abaixo."
    elif topo is None:
        prova = "Compromisso não encontrado no registro."
    else:
        prova = (f"{ok_total} de {total} checagens OK. Não confie nestes dados até "
                 "entender a falha mostrada abaixo.")
    dados_topo = "" if topo is None else (
        f'<div class="valor">{e(topo["valor"])}</div>'
        f'<div class="rota">{e(topo["rota"])}</div>'
        '<dl class="fatos">'
        f'<dt>Transportadora</dt><dd>{e(topo["transportadora"])}</dd>'
        f'<dt>Fechado em</dt><dd>{e(topo["fechado_em"])}</dd>'
        "</dl>")
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<link rel="icon" href="data:,">
<title>Verificação do compromisso {cid} · Elo</title>
<style>{_ESTILO}</style>
</head>
<body>
<main>
<h1>Verificação do compromisso</h1>
<p class="id">{cid}</p>
<section class="topo {"valido" if valido else "invalido"}">
<div class="selo">{selo}</div>
{dados_topo}
<div class="det">{e(prova)}</div>
</section>
<h2>A prova: {total} checagens independentes</h2>
<table id="checagens">
<tr><th class="n">#</th><th>Checagem</th><th class="st">Resultado</th></tr>
{linhas}
</table>
{tabela_resumo}
<footer>
<p>Todas as checagens são refeitas agora, a partir do registro de eventos com
hashes encadeados. As assinaturas são Ed25519. O aceite do embarcador foi
feito por link e é atestado pela plataforma, não assinado pelo embarcador.</p>
<p><a href="/compromissos/{cid}/verificacao">Ver relatório em JSON</a></p>
</footer>
</main>
</body>
</html>"""


app = criar_app()
